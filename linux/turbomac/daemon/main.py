"""turbomacd entry point: wires sampler, governor, logger, GPU control and the socket."""
from __future__ import annotations

import argparse
import os
import signal
import socket
import threading
import time
from typing import Any

from turbomac import __version__, config
from turbomac.config import HARD_MAX_W
from turbomac.daemon import governor, incidents
from turbomac.daemon.cpu import CpuReader
from turbomac.daemon.gpu import Gpu
from turbomac.daemon.logger import EventLog, SampleLogger
from turbomac.daemon.msr import Msr
from turbomac.daemon.paths import Paths
from turbomac.daemon.rapl import Rapl, apply_and_verify, load_or_capture_firmware, valid_firmware
from turbomac.daemon.sampler import FAST_HZ, SLOW_EVERY, Sampler
from turbomac.daemon.server import ControlServer
from turbomac.daemon.smc import SMC, SysfsSmcIO
from turbomac.daemon.sysfs import ensure_group_dir, read_text

WATCHDOG_EVERY_S = 2.0
WATCHDOG_MAX_STEP_AGE_S = 5.0   # ping only if a governor step succeeded this recently
GPU_RETRY_EVERY_S = 15.0
GPU_RETRIES = 12
GPU_RETRYABLE = ("amdgpu device not found", "amdgpu hwmon not found", "vga_switcheroo not available")


def sd_notify(msg: str) -> bool:
    """Send a datagram to $NOTIFY_SOCKET (abstract namespace if it starts with '@')."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    if addr[0] == "@":
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(msg.encode())
        return True
    except OSError:
        return False


class Daemon:
    def __init__(self, paths: Paths, events: EventLog | None = None,
                 journal: incidents.JournalRunner | None = None) -> None:
        self.paths = paths
        self.events = events or EventLog()
        self.journal = journal or incidents.run_journal
        self.cfg_lock = threading.Lock()
        self.config, problems = config.load(paths.config)
        for key, reason in problems.items():
            self.events.emit("config_problem", key=key, reason=reason)
        self.boot_id = read_text(paths.boot_id, quiet=True)
        self.stop_event = threading.Event()
        self.reload_flag = threading.Event()
        self.readback: dict[str, Any] | None = None
        self.last_write: dict[str, Any] | None = None
        self.gpu_result: dict[str, Any] | None = None
        self.incident_check: dict[str, Any] = {}
        self.status_snapshot: dict[str, Any] = {}
        self.started_wall = time.time()
        self.server: ControlServer | None = None
        self.logger: SampleLogger | None = None
        self.log_failed = False
        self.notify = sd_notify
        self.last_step_ok: float | None = None
        self.loop_start: float | None = None
        self.ready = False
        self.last_wd = -1e18
        self._errors: dict[str, tuple[str, float]] = {}
        self.gpu_thread: threading.Thread | None = None
        self.gpu_retry_left = 0               # > 0 while the startup GPU apply is being retried
        self.gpu_retry_next: float | None = None

    # setup -------------------------------------------------------------------
    def setup(self, start_server: bool = True) -> None:
        p = self.paths
        smc_dir = p.smc_dir()
        self.smc = SMC(SysfsSmcIO(smc_dir), p.smc_keys_cache) if smc_dir else None
        if self.smc is None or not self.smc.open():
            self.events.emit("smc_unavailable", dir=smc_dir)
        self.rapl = Rapl(p.powercap)
        self.msr = Msr(p.msr_dev)
        self.gpu = Gpu(p)
        self.cpu = CpuReader(p)
        self.sampler = Sampler(self.smc if self.smc and self.smc.ok else None,
                               self.rapl if self.rapl.available() else None, self.msr, self.gpu, self.cpu, smc_dir)
        self.firmware, warn = load_or_capture_firmware(p.firmware_limits, self.rapl, self.msr, self.boot_id)
        if warn:
            self.events.emit("firmware_limits_warning", message=warn)
        ensure_group_dir(p.incidents_dir)
        self.incident_check = incidents.check_previous_boot(p, self.boot_id, self.journal)
        self.events.emit("boot_check", **self.incident_check)
        lg = self.config["logging"]
        self.logger = SampleLogger(p.samples_dir, lg["retention_days"], lg["max_mib"], self.boot_id,
                                   on_event=self.events.emit)
        self.gov = governor.initial(time.monotonic(), self.firmware if valid_firmware(self.firmware) else None)
        for msg in self.cpu.apply_settings(self.config["cpu"]):
            self.events.emit("cpu_setting", message=msg)
        self.gpu_thread = threading.Thread(target=self._apply_gpu_from_config, name="gpu-start", daemon=True)
        self.gpu_thread.start()
        if start_server:
            self.server = ControlServer(p.socket, self.dispatch)
            self.server.start()
        self.events.emit("started", version=__version__, mode=self.config["governor"]["mode"],
                         smc_keys=len(self.smc.table) if self.smc else 0, firmware=self.firmware)

    def _apply_gpu_from_config(self, attempt: int = 0) -> None:
        """Apply gpu.mode/cap_w. attempt 0 is the startup apply; > 0 are control-loop retries."""
        g = self.config["gpu"]
        res = self.gpu.apply_mode(g["mode"], g["cap_w"])
        self.gpu_result = res
        self.events.emit("gpu_mode", requested=g["mode"], attempt=attempt, **res)
        retryable = not res["ok"] and any(r in res["reason"] for r in GPU_RETRYABLE)
        if attempt == 0:
            if retryable:  # amdgpu may still be probing at boot
                self.gpu_retry_left, self.gpu_retry_next = GPU_RETRIES, None
        elif res["ok"]:
            self.gpu_retry_left = 0
            self.events.emit("gpu_retry_succeeded", attempt=attempt, mode=res["mode"])
        elif not retryable or self.gpu_retry_left <= 0:
            self.gpu_retry_left = 0
            self.events.emit("gpu_retry_gave_up", attempt=attempt, reason=res["reason"])

    def _maybe_retry_gpu(self, now: float) -> None:
        """Called every tick: schedules startup GPU retries every 15 s, at most 12."""
        if self.gpu_retry_left <= 0 or self.gpu.busy or (self.gpu_thread and self.gpu_thread.is_alive()):
            return
        if self.gpu_retry_next is None:
            self.gpu_retry_next = now + GPU_RETRY_EVERY_S
            return
        if now < self.gpu_retry_next:
            return
        attempt = GPU_RETRIES - self.gpu_retry_left + 1
        self.gpu_retry_left -= 1
        self.gpu_retry_next = now + GPU_RETRY_EVERY_S
        self.events.emit("gpu_retry", attempt=attempt, of=GPU_RETRIES,
                         reason=(self.gpu_result or {}).get("reason"))
        self.gpu_thread = threading.Thread(target=self._apply_gpu_from_config, args=(attempt,),
                                           name="gpu-retry", daemon=True)
        self.gpu_thread.start()

    # control loop --------------------------------------------------------------
    def _error(self, typ: str, exc: BaseException, now: float) -> None:
        """Emit an error event, at most once a minute per distinct message."""
        msg, last = self._errors.get(typ, ("", -1e18))
        if repr(exc) != msg or now - last > 60:
            self.events.emit(typ, error=repr(exc))
            self._errors[typ] = (repr(exc), now)

    def tick(self, now: float, slow: bool) -> None:
        """One 250 ms step. Telemetry, logging and status errors never skip governor.step."""
        wall = time.time()
        try:
            self.sampler.fast(now, wall)
        except Exception as exc:  # noqa: BLE001 - an unreadable sample is invalid, never stale-valid
            self.sampler.latest = {"ts": wall, "mono": now, "valid": False}
            self._error("sample_error", exc, now)
        if slow:
            try:
                self.sampler.slow(now, wall)
            except Exception as exc:  # noqa: BLE001
                self._error("slow_sample_error", exc, now)
        cfg = self.config
        sample = self.sampler.gov_input(now)
        sample["readback"], self.readback = self.readback, None
        self.gov, actions = governor.step(self.gov, sample, cfg, now)
        for a in actions:
            self.apply(a)
        self.last_step_ok = now
        if slow and self.logger is not None:
            try:
                rec = self.sampler.aggregate(now, wall, cfg["governor"]["ceiling_w"], governor.summary(self.gov))
                self.logger.write(rec)
            except Exception as exc:  # noqa: BLE001
                self._error("log_error", exc, now)
        self._maybe_retry_gpu(now)
        try:
            self.status_snapshot = self.build_status(now)
        except Exception as exc:  # noqa: BLE001
            self._error("status_error", exc, now)

    def apply(self, action: dict[str, Any]) -> None:
        if action["op"] == "event":
            fields = {k: v for k, v in action.items() if k not in ("op", "type")}
            self.events.emit(action["type"], **fields)
            return
        if action["op"] != "set_limits":
            return
        rb: dict[str, Any] = {"ok": False, "pl1_w": action["pl1_w"], "pl2_w": action["pl2_w"]}
        if not self.rapl.available():
            rb["reason"] = "RAPL powercap unavailable"
        else:
            try:
                rb = apply_and_verify(self.rapl, self.msr, action["pl1_w"], action["pl2_w"], HARD_MAX_W, self.firmware)
            except Exception as exc:  # noqa: BLE001 - never assume a write landed
                rb["reason"] = f"exception: {exc!r}"
        self.readback = {"ok": rb["ok"], "reason": rb["reason"]}
        self.last_write = {"ts": round(time.time(), 3), "why": action["reason"],
                           **{k: rb.get(k) for k in ("ok", "reason", "pl1_w", "pl2_w")}}
        self.events.emit("write", **self.last_write)

    def loop_once(self, now: float, tick: int) -> None:
        """One control-loop iteration: tick, then READY / WATCHDOG to systemd."""
        if self.loop_start is None:
            self.loop_start = now
        if self.reload_flag.is_set():
            self.reload_flag.clear()
            self.reload_config()
        try:
            self.tick(now, slow=tick % SLOW_EVERY == 0)
        except Exception as exc:  # noqa: BLE001 - keep looping; the watchdog decides
            self._error("tick_error", exc, now)
        ok = self.last_step_ok is not None and now - self.last_step_ok <= WATCHDOG_MAX_STEP_AGE_S
        if ok and not self.ready and (self.sampler.latest.get("valid")
                                      or now - self.loop_start > self.config["governor"]["stale_timeout_s"] + 1):
            self.notify("READY=1")
            self.ready = True
        if ok and now - self.last_wd >= WATCHDOG_EVERY_S:   # a wedged or failing governor stops the pings
            self.notify("WATCHDOG=1")
            self.last_wd = now

    def run(self) -> None:
        period = 1.0 / FAST_HZ
        next_t = time.monotonic()
        tick = 0
        while not self.stop_event.is_set():
            now = time.monotonic()
            if now < next_t:
                self.stop_event.wait(next_t - now)
                continue
            next_t = max(next_t + period, now + period / 2)
            self.loop_once(now, tick)
            tick += 1

    def shutdown(self) -> None:
        self.notify("STOPPING=1")
        if self.server is not None:
            self.server.stop()
        if self.logger is not None:
            self.logger.close()
        if getattr(self, "smc", None) is not None:
            self.smc.restore()
        try:
            incidents.mark_clean(self.paths)
        except OSError as exc:
            self.events.emit("mark_clean_failed", error=str(exc))
        self.events.emit("stopped")

    # status and requests ---------------------------------------------------------
    def build_status(self, now: float) -> dict[str, Any]:
        cfg = self.config
        sd = self.sampler.slow_data
        der = self.sampler.derived(now, cfg["governor"]["ceiling_w"])
        return {
            "ts": round(time.time(), 3), "host": socket.gethostname(), "version": __version__,
            "boot_id": self.boot_id, "daemon_uptime_s": round(time.time() - self.started_wall, 1),
            "latest": self.sampler.latest, "derived": der,
            "thresholds": governor.thresholds(der["acpw_median_10s"], cfg["governor"]),
            "governor": {"mode": cfg["governor"]["mode"], **governor.summary(self.gov),
                         "in_state_s": round(now - self.gov.since, 1),
                         "ceiling_w": cfg["governor"]["ceiling_w"], "static_cap_w": cfg["governor"]["static_cap_w"],
                         "floor_w": cfg["governor"]["floor_w"], "last_write": self.last_write},
            "limits": {"sysfs": sd.get("limits"), "msr": sd.get("msr_limits"), "firmware": self.firmware,
                       "hard_max_w": HARD_MAX_W},
            "msr": sd.get("msr"), "smc": sd.get("smc"), "smc_temps": sd.get("smc_temps"), "fans": sd.get("fans"),
            "gpu": {**(sd.get("gpu") or {}), "config_mode": cfg["gpu"]["mode"], "cap_cfg_w": cfg["gpu"]["cap_w"],
                    "busy": self.gpu.busy, "last_result": self.gpu_result},
            "cpu": sd.get("cpu"), "peaks": dict(self.sampler.peaks),
            "smc_ok": bool(self.smc and self.smc.ok), "config_digest": config.digest(cfg),
            "incident_check": self.incident_check,
        }

    def dispatch(self, cmd: str, args: dict[str, Any]) -> Any:
        p = self.paths
        if cmd == "status":
            return self.status_snapshot
        if cmd == "history":
            return self.sampler.history_slice(float(args.get("seconds", 600)), bool(args.get("full")))
        if cmd == "config.get":
            return {"config": self.config, "defaults": config.DEFAULTS, "digest": config.digest(self.config),
                    "path": p.config, "hard_max_w": HARD_MAX_W}
        if cmd == "config.set":
            return self.set_config(args)
        if cmd == "config.reset":
            upd = {k: v for k, v in config.flatten(config.DEFAULTS).items() if k != "schema"}
            return self.set_config(upd)
        if cmd == "gpu.set":
            return self.set_gpu(args.get("mode"))
        if cmd == "incidents":
            return incidents.list_incidents(p.incidents_dir)
        if cmd == "incident":
            inc = incidents.load_incident(p.incidents_dir, str(args.get("id", "")))
            if inc is None:
                raise KeyError(f"no incident {args.get('id')!r}")
            return inc
        if cmd == "events":
            return self.events.recent(int(args.get("n", 100)))
        if cmd == "smc.keys":
            return self.smc.keys_info() if self.smc else []
        raise ValueError(f"unknown command {cmd!r}")

    def set_config(self, updates: dict[str, Any]) -> dict[str, Any]:
        gpu_mode = updates.pop("gpu.mode", None)
        with self.cfg_lock:
            accepted, rejected = config.validate(updates, self.config)
            new = config.apply_updates(self.config, accepted)
            persisted, err = True, None
            if accepted:
                try:
                    config.save(self.paths.config, new)
                except OSError as exc:
                    persisted, err = False, str(exc)
                self.config = new
        if accepted:
            self.events.emit("config_change", accepted=accepted, rejected=rejected, persisted=persisted)
            self._side_effects(accepted)
        out: dict[str, Any] = {"accepted": accepted, "rejected": rejected, "persisted": persisted,
                               "error": err, "digest": config.digest(self.config)}
        if gpu_mode is not None and gpu_mode == self.config["gpu"]["mode"]:
            out["accepted"]["gpu.mode"] = gpu_mode
        elif gpu_mode is not None:
            try:
                out["gpu"] = self.set_gpu(gpu_mode)
                out["accepted"]["gpu.mode"] = out["gpu"]["mode"]
            except ValueError as exc:
                out["rejected"]["gpu.mode"] = str(exc)
        return out

    def _side_effects(self, accepted: dict[str, Any]) -> None:
        if any(k.startswith("cpu.") for k in accepted):
            for msg in self.cpu.apply_settings(self.config["cpu"]):
                self.events.emit("cpu_setting", message=msg)
        if any(k.startswith("logging.") for k in accepted) and self.logger is not None:
            self.logger.retention_days = self.config["logging"]["retention_days"]
            self.logger.max_mib = self.config["logging"]["max_mib"]
        if "gpu.cap_w" in accepted and self.config["gpu"]["mode"] == "auto" and not self.gpu.busy:
            self.gpu_thread = threading.Thread(target=self._apply_gpu_from_config, daemon=True)
            self.gpu_thread.start()

    def set_gpu(self, mode: Any) -> dict[str, Any]:
        """Gated mode change; persists the mode actually reached (PLAN 4.4)."""
        if mode not in ("auto", "low", "off"):
            raise ValueError("mode must be auto, low or off")
        if self.gpu.busy:
            raise ValueError("a GPU mode change is already running")
        self.gpu_retry_left = 0  # an explicit request supersedes startup retries
        self.events.emit("gpu_mode_request", mode=mode)
        res = self.gpu.apply_mode(mode, self.config["gpu"]["cap_w"])
        self.gpu_result = res
        with self.cfg_lock:
            self.config = config.apply_updates(self.config, {"gpu.mode": res["mode"]})
            try:
                config.save(self.paths.config, self.config)
                res["persisted"] = True
            except OSError as exc:
                res["persisted"] = False
                res["persist_error"] = str(exc)
        self.events.emit("gpu_mode", requested=mode, **res)
        return res

    def reload_config(self) -> None:
        cfg, problems = config.load(self.paths.config)
        with self.cfg_lock:
            self.config = cfg
        self.events.emit("config_reload", problems=problems, digest=config.digest(cfg))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="turbomacd", description="TurboMac power governor daemon")
    ap.add_argument("--no-socket", action="store_true", help="do not open the control socket")
    args = ap.parse_args(argv)
    d = Daemon(Paths.from_env())
    signal.signal(signal.SIGTERM, lambda *_: d.stop_event.set())
    signal.signal(signal.SIGINT, lambda *_: d.stop_event.set())
    signal.signal(signal.SIGHUP, lambda *_: d.reload_flag.set())
    d.setup(start_server=not args.no_socket)
    try:
        d.run()
    finally:
        d.shutdown()
    return 0
