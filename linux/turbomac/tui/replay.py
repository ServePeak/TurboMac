"""ReplayBackend: play a turbomacd sample log (.jsonl or .jsonl.gz) as if it were live.

Each line is one 1 Hz record from Sampler.aggregate; a {"kind": "header"} line
carries the boot id. Records play at 1 record per second x speed and loop; each
loop shifts timestamps by the log's span + 1 s so time keeps moving forward.
history() returns the records already played (the last 600).

Record -> status mapping (keys as in Daemon.build_status; any missing or null
field stays None, which the TUI shows as "-"):
  ts, latest.ts            rec.ts (+ loop offset)
  host / version / boot_id "replay" / this turbomac / header boot_id
  daemon_uptime_s          seconds since the first record
  latest                   pdtr_w = input_w (the log has 1 s means, not 4 Hz samples), v, a,
                           acpw_w, package_w = pkg_w, core_w, uncore_w, dram_w; valid = smc_valid
  derived                  input_mean_1s = input_w, input_max_1s = input_max_w,
                           pkg_median_1s = pkg_w, acpw_median_10s = acpw_w,
                           noncpu_est_w = noncpu_w, headroom_w = headroom_w,
                           pred_input_at_ceiling_w = governor.predicted_input_w(config ceiling)
  thresholds               governor.thresholds(acpw_w, config): follows config_set
  governor                 state, pl1_w, pl2_w from the record; mode from config (initially
                           inferred: STATIC -> static, OFF -> off, else dynamic); since/in_state_s
                           from state changes seen; counters reconstructed from PL1 steps
                           (cuts = decreases, recoveries = increases, writes = changes) and
                           EMERGENCY/FALLBACK entries; ceiling/static/floor from config
  limits                   sysfs = rec.sysfs_limits, msr = rec.msr_limits, firmware None, hard max 45 W
  msr, smc, smc_temps, fans, cpu   copied
  gpu                      rec.gpu + config_mode/cap_cfg_w from config, busy False, last_result
  fan                      rec.fan (FanController.log_record: mode, target_rpm, reason, write_errors,
                           per-fan min_written/hw_min lists) joined with rec.fans for label/rpm/max;
                           last_change_ts None. Logs from before the fan floor have no rec.fan: mode
                           "auto", target 0, min_written = hw_min = the logged fan*_min
  peaks                    max of input_max_w, pkg_w, a, gpu_w, pkg_c over the records played
  smc_ok, config_digest    rec.smc_valid, digest of the in-memory config
  incident_check           {"verdict": "unknown", "reason": "replay"}
config_set/config_reset/gpu_set are validated like the daemon does and reflected
in what is shown (bands, ceiling, modes) but never persisted.
"""
from __future__ import annotations

import gzip
import json
import os
import threading
import time
from collections import deque
from typing import Any, Iterator

from turbomac import __version__, config
from turbomac.config import HARD_MAX_W
from turbomac.daemon import governor
from turbomac.daemon.sampler import HISTORY_FIELDS, HISTORY_LEN
from turbomac.tui.backend import BackendError, SimBackend

PEAK_FIELDS = (("input_w", "input_max_w"), ("pkg_w", "pkg_w"), ("a", "a"), ("gpu_w", "gpu_w"), ("pkg_c", "pkg_c"))


def load_records(path: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """(header, records). Torn or non-JSON lines are skipped."""
    opener = gzip.open if path.endswith(".gz") else open
    header: dict[str, Any] = {}
    recs: list[dict[str, Any]] = []
    with opener(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            if rec.get("kind") == "header":
                header = header or rec
            elif isinstance(rec.get("ts"), (int, float)):
                recs.append(rec)
    if not recs:
        raise BackendError(f"{path}: no sample records")
    return header, recs


def _mode_for(state: Any) -> str:
    return {"STATIC": "static", "OFF": "off"}.get(state, "dynamic")


class ReplayBackend(SimBackend):
    kind = "replay"
    persisted = False
    not_persisted_reason = "replay: changes are shown, never saved"
    config_path = "(replay: in memory, not persisted)"

    def __init__(self, path: str, speed: float = 1.0) -> None:
        self.path = path
        self.header, self.records = load_records(path)
        self.t0 = self.records[0]["ts"]
        self.span = self.records[-1]["ts"] - self.t0 + 1.0
        cfg = config.apply_updates(config.defaults(), {
            "governor.mode": _mode_for(self.records[0].get("state")),
            "gpu.mode": (self.records[0].get("gpu") or {}).get("mode") or "auto"})
        super().__init__(cfg)
        self.speed = max(0.05, float(speed))
        self.pos = 0.0            # record-seconds played
        self.played = -1          # absolute index of the last record played (across loops)
        self.history_rows: deque[dict[str, Any]] = deque(maxlen=HISTORY_LEN)
        self.peaks: dict[str, dict[str, float]] = {}
        self.counters = {"cuts": 0, "emergencies": 0, "fallbacks": 0, "recoveries": 0, "writes": 0,
                         "readback_failures": 0}
        self.state, self.since, self.prev_pl1 = None, self.t0, None
        self.gpu_result: dict[str, Any] | None = None
        self.emit_at = self.t0
        self._play_to(0)
        self.connected = True

    @property
    def label(self) -> str:
        name = os.path.basename(self.path).removesuffix(".gz").removesuffix(".jsonl")
        return f"replay ×{self.speed:g} {name}"

    def wall(self) -> float:
        return self.emit_at

    def _rec(self, k: int) -> dict[str, Any]:
        """Absolute index k -> record copy with loop-shifted ts/time."""
        loop, i = divmod(k, len(self.records))
        rec = dict(self.records[i])
        if loop:
            rec["ts"] = rec["ts"] + loop * self.span
            rec["time"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(rec["ts"]))
        return rec

    def _play_to(self, k: int) -> None:
        while self.played < k:
            self.played += 1
            rec = self._rec(self.played)
            self.emit_at = rec["ts"]
            if self.played == 0:
                self.emit("replay_start", file=os.path.basename(self.path), records=len(self.records))
            elif self.played % len(self.records) == 0:
                self.emit("replay_loop", loop=self.played // len(self.records))
            st, pl1 = rec.get("state"), rec.get("pl1_w")
            if st != self.state:
                if self.state is not None:
                    self.emit("state", **{"from": self.state, "to": st})
                    if st == "EMERGENCY":
                        self.counters["emergencies"] += 1
                    if st == "FALLBACK":
                        self.counters["fallbacks"] += 1
                self.state, self.since = st, rec["ts"]
            if isinstance(pl1, (int, float)) and isinstance(self.prev_pl1, (int, float)) and pl1 != self.prev_pl1:
                self.counters["writes"] += 1
                self.counters["cuts" if pl1 < self.prev_pl1 else "recoveries"] += 1
                self.emit("write", why="replay", ok=True, pl1_w=pl1, pl2_w=rec.get("pl2_w"))
            self.prev_pl1 = pl1 if pl1 is not None else self.prev_pl1
            for key, src in PEAK_FIELDS:
                v = rec.get(src)
                if isinstance(v, (int, float)) and not isinstance(v, bool) and (
                        key not in self.peaks or v > self.peaks[key]["value"]):
                    self.peaks[key] = {"value": round(float(v), 2), "ts": rec["ts"]}
            self.history_rows.append(rec)

    def advance(self, seconds: float) -> None:
        """Advance by wall seconds (x speed record-seconds)."""
        with self.lock:
            self.pos += seconds * self.speed
            self._play_to(int(self.pos))

    def _stream(self, stop: threading.Event) -> Iterator[dict[str, Any]]:
        last = time.monotonic()
        while not stop.is_set():
            now = time.monotonic()
            self.advance(now - last)
            last = now
            yield self.status()
            stop.wait(self.period_s)

    def status(self) -> dict[str, Any]:
        with self.lock:
            return self.map_record(self.history_rows[-1])

    def map_record(self, rec: dict[str, Any]) -> dict[str, Any]:
        """One aggregate record -> a status dict shaped like Daemon.build_status (see module doc)."""
        g = self.config["governor"]
        acpw = rec.get("acpw_w")
        lim = rec.get("sysfs_limits") or {}
        gpu = dict(rec.get("gpu") or {})
        gpu.update(config_mode=self.config["gpu"]["mode"], cap_cfg_w=self.config["gpu"]["cap_w"], busy=False,
                   last_result=self.gpu_result)
        return {
            "ts": rec["ts"], "host": "replay", "version": __version__, "boot_id": self.header.get("boot_id"),
            "daemon_uptime_s": round(rec["ts"] - self.t0, 1),
            "latest": {"ts": rec["ts"], "mono": rec["ts"] - self.t0, "valid": bool(rec.get("smc_valid")),
                       "pdtr_w": rec.get("input_w"), "v": rec.get("v"), "a": rec.get("a"), "acpw_w": acpw,
                       "package_w": rec.get("pkg_w"), "core_w": rec.get("core_w"),
                       "uncore_w": rec.get("uncore_w"), "dram_w": rec.get("dram_w")},
            "derived": {"input_mean_1s": rec.get("input_w"), "input_max_1s": rec.get("input_max_w"),
                        "pkg_median_1s": rec.get("pkg_w"), "acpw_median_10s": acpw,
                        "noncpu_est_w": rec.get("noncpu_w"), "headroom_w": rec.get("headroom_w"),
                        "pred_input_at_ceiling_w": governor.predicted_input_w(g["ceiling_w"])},
            "thresholds": governor.thresholds(acpw, g),
            "governor": {"mode": g["mode"], "state": rec.get("state"), "since": self.since,
                         "pl1_w": rec.get("pl1_w"), "pl2_w": rec.get("pl2_w"), "fallback_reason": None,
                         "retry_at": None, "backoff_s": 0.0, "last_action": None, "counters": dict(self.counters),
                         "time_in_state": {}, "in_state_s": round(rec["ts"] - self.since, 1),
                         "ceiling_w": g["ceiling_w"], "static_cap_w": g["static_cap_w"], "floor_w": g["floor_w"],
                         "last_write": None},
            "limits": {"sysfs": lim or None, "msr": rec.get("msr_limits"), "firmware": None,
                       "hard_max_w": HARD_MAX_W},
            "msr": rec.get("msr"), "smc": rec.get("smc"), "smc_temps": rec.get("smc_temps"), "fans": rec.get("fans"),
            "gpu": gpu, "fan": self.map_fan(rec), "cpu": rec.get("cpu"),
            "peaks": {k: dict(v) for k, v in self.peaks.items()},
            "smc_ok": bool(rec.get("smc_valid")), "config_digest": config.digest(self.config),
            "incident_check": {"verdict": "unknown", "reason": "replay: no boot check"},
        }

    @staticmethod
    def map_fan(rec: dict[str, Any]) -> dict[str, Any]:
        """rec.fan + rec.fans -> the shape of FanController.status() (see module doc)."""
        fans = [f for f in rec.get("fans") or [] if isinstance(f, dict)]
        fan = rec.get("fan")
        if not isinstance(fan, dict):
            return {"mode": "auto", "target_rpm": 0, "reason": "replay: no fan floor in this log",
                    "fans": [{"label": f.get("label"), "rpm": f.get("rpm"), "min_written": f.get("min"),
                              "hw_min": f.get("min"), "max": f.get("max")} for f in fans],
                    "last_change_ts": None, "write_errors": 0}
        mw, hw = fan.get("min_written") or [], fan.get("hw_min") or []
        return {"mode": fan.get("mode"), "target_rpm": fan.get("target_rpm"), "reason": fan.get("reason"),
                "fans": [{"label": f.get("label"), "rpm": f.get("rpm"), "min_written": mw[i] if i < len(mw) else None,
                          "hw_min": hw[i] if i < len(hw) else None, "max": f.get("max")} for i, f in enumerate(fans)],
                "last_change_ts": None, "write_errors": fan.get("write_errors", 0)}

    def history(self, seconds: float = 600, full: bool = False) -> list[dict[str, Any]]:
        n = max(0, min(int(seconds), HISTORY_LEN))
        with self.lock:
            rows = list(self.history_rows)[-n:] if n else []
        return rows if full else [{k: r.get(k) for k in HISTORY_FIELDS} for r in rows]

    def gpu_set(self, mode: str) -> dict[str, Any]:
        if mode not in ("auto", "low", "off"):
            raise BackendError("mode must be auto, low or off")
        with self.lock:
            self.config = config.apply_updates(self.config, {"gpu.mode": mode})
            self.gpu_result = {"ok": True, "mode": mode, "reason": "replay: nothing applied",
                               "steps": [f"replay: would apply {mode}"], "persisted": False}
            self.last_change_ts = self.wall()
            self.emit("gpu_mode", requested=mode, **self.gpu_result)
            return dict(self.gpu_result)

    def incidents(self) -> list[dict[str, Any]]:
        return []

    def incident(self, incident_id: str) -> dict[str, Any]:
        raise BackendError(f"no incident {incident_id!r} (replay has none)")

    def smc_keys(self) -> list[dict[str, Any]]:
        rec = self.history_rows[-1]
        names = ["ACPW", "ID0R", "PDTR", "VD0R", *(rec.get("smc") or {}), *(rec.get("smc_temps") or {})]
        return [{"name": n, "index": None, "type": "?", "len": None} for n in sorted(set(names))]
