"""Fan floor (Phase 4): raise applesmc fan{N}_min ahead of the SMC's lagging fan curve.

Why: at a sustained 40 W the package reaches 97-100 °C in ~30 s while the SMC
curve has the fans at ~3,900 of 5,927 rpm (receipts/phase3-20261010). The SMC
keeps automatic control; fan{N}_min is only a floor it respects. TurboMac never
writes fan{N}_manual or fan{N}_output (SysfsFanIO cannot: it only knows
write_min).

decide() is pure; FanController does the I/O and runs from the daemon's 1 Hz
slow tick, never the fast path.
"""
from __future__ import annotations

import glob
import json
import math
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from turbomac.daemon.sysfs import read_int, read_text, write_text

POWER_HYST_W = 5.0          # the power rule releases below power_floor_w - 5 W
READBACK_TOL_RPM = 50       # the SMC may round what it stores
RETRY_S = 30.0              # a failed write of the same value is retried at most this often
KNOWN_HW_MIN = {1: 2160, 2: 2000}   # verified fan1/fan2 hardware minimums on this machine (MacBookPro15,1;
                                    # measured identical to the MacBookPro14,3 reference: 2160 / 2000 rpm)
CAPTURE_SLACK_RPM = 500     # a live fan{N}_min this far above KNOWN_HW_MIN means a floor is already applied


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


@dataclass(frozen=True)
class FanState:
    power_on: bool = False
    power_below_since: float | None = None   # pkg W below the release threshold since
    level: int = 0                            # temperature ladder steps engaged, 0..3
    temp_below_since: float | None = None     # pkg °C below the current step's release threshold since


def ladder(cfg: dict[str, Any]) -> list[tuple[float, int]]:
    return [(float(cfg[f"t{i}_c"]), int(cfg[f"t{i}_rpm"])) for i in (1, 2, 3)]


def decide(state: FanState, temp_c: Any, pkg_w: Any, cfg: dict[str, Any], now: float
           ) -> tuple[FanState, int, str]:
    """(new_state, target_rpm, reason); target 0 means the hardware minimum.

    Target = max(power rule, temperature ladder), capped at max_rpm. Each rule
    engages as soon as its threshold is met and releases only after its input
    has been below threshold - hysteresis (hysteresis_c for temperature steps,
    5 W for power) continuously for step_down_interval_s; the ladder steps down
    one step per interval. A missing input resets its rule and contributes 0.
    """
    hyst, interval = float(cfg["hysteresis_c"]), float(cfg["step_down_interval_s"])
    pw_th = float(cfg["power_floor_w"])
    steps = ladder(cfg)
    have_t, have_p = _num(temp_c), _num(pkg_w)

    power_on, p_since = state.power_on, state.power_below_since
    if not have_p:
        power_on, p_since = False, None
    elif pkg_w >= pw_th:
        power_on, p_since = True, None
    elif power_on and pkg_w < pw_th - POWER_HYST_W:
        p_since = now if p_since is None else p_since
        if now - p_since >= interval:
            power_on, p_since = False, None
    else:
        p_since = None

    level, t_since = state.level, state.temp_below_since
    if not have_t:
        level, t_since = 0, None
    else:
        met = max((i + 1 for i, (c, _) in enumerate(steps) if temp_c >= c), default=0)
        if met >= level:                     # at or above the current step: hold or step up now
            level, t_since = met, None
        elif temp_c < steps[level - 1][0] - hyst:
            t_since = now if t_since is None else t_since
            if now - t_since >= interval:    # one step per interval
                level -= 1
                t_since = now if level else None
        else:
            t_since = None

    cands: list[tuple[int, str]] = []
    if level:
        c, rpm = steps[level - 1]
        cands.append((rpm, f"pkg {temp_c:g} °C ≥ {c:g} °C" if temp_c >= c
                      else f"cooling: {rpm} rpm until pkg < {c - hyst:g} °C for {interval:g} s"))
    if power_on:
        rpm = int(cfg["power_floor_rpm"])
        cands.append((rpm, f"power {pkg_w:.0f} W ≥ {pw_th:g} W" if pkg_w >= pw_th
                      else f"cooling: {rpm} rpm until power < {pw_th - POWER_HYST_W:g} W for {interval:g} s"))
    new = FanState(power_on, p_since, level, t_since)
    if not cands:
        return new, 0, ("below thresholds" if have_t or have_p else "no temperature or power data")
    target, reason = max(cands, key=lambda x: x[0])   # ties: the temperature step is named
    cap = int(cfg["max_rpm"])
    if target > cap:
        target, reason = cap, f"{reason} (max {cap} rpm)"
    return new, target, reason


class SysfsFanIO:
    """applesmc fan{N}_* attributes. The only write it offers is fan{N}_min."""

    def __init__(self, base: str | None) -> None:
        self.base = base

    def _p(self, n: int, attr: str) -> str:
        return os.path.join(self.base or "/nonexistent-applesmc", f"fan{n}_{attr}")

    def numbers(self) -> list[int]:
        if not self.base:
            return []
        out = []
        for p in glob.glob(os.path.join(self.base, "fan*_input")):
            m = re.fullmatch(r"fan(\d+)_input", os.path.basename(p))
            if m:
                out.append(int(m.group(1)))
        return sorted(out)

    def read_int(self, n: int, attr: str) -> int | None:
        return read_int(self._p(n, attr), quiet=True)

    def read_text(self, n: int, attr: str) -> str | None:
        return read_text(self._p(n, attr), quiet=True)

    def write_min(self, n: int, rpm: int) -> str | None:
        return write_text(self._p(n, "min"), str(int(rpm)))


class FanController:
    """Discovers the fans, captures their hardware minimums once, applies and restores the floor.

    Thread-safe: tick() runs on the control loop, on_config() on a socket thread.
    """

    def __init__(self, paths: Any, events: Any, io: Any = None, clock: Callable[[], float] = time.time,
                 defaults_path: str | None = None) -> None:
        self.events = events
        self.io = io if io is not None else SysfsFanIO(paths.smc_dir() if paths is not None else None)
        self.defaults_path = defaults_path or (paths.fan_defaults if paths is not None else None)
        self.clock = clock
        self.lock = threading.RLock()
        self.state = FanState()
        self.mode: str | None = None
        self.target_rpm = 0
        self.reason = "not started"
        self.last_change_ts: float | None = None
        self.write_errors = 0
        self.fans: dict[int, dict[str, Any]] = {}
        self.written: dict[int, int | None] = {}     # what fan{N}_min holds, as far as we know
        self.failed: dict[int, tuple[int, float]] = {}
        self.defaults: dict[int, int] = {}
        self.discover()

    def _emit(self, typ: str, **fields: Any) -> None:
        if self.events is not None:
            self.events.emit(typ, **fields)

    # discovery and hardware defaults ------------------------------------------------
    def discover(self) -> None:
        for n in self.io.numbers():
            self.fans[n] = {"label": self.io.read_text(n, "label") or f"fan{n}",
                            "max": self.io.read_int(n, "max"), "rpm": self.io.read_int(n, "input")}
            self.written[n] = self.io.read_int(n, "min")
        self.defaults = self._load_or_capture() if self.fans else {}

    def _plausible(self, n: int, v: Any) -> bool:
        """Looks like a hardware default: positive int, not above max, not clearly a floor already applied."""
        if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
            return False
        mx = self.fans.get(n, {}).get("max")
        if isinstance(mx, int) and v > mx:
            return False
        return n not in KNOWN_HW_MIN or v <= KNOWN_HW_MIN[n] + CAPTURE_SLACK_RPM

    def _load_or_capture(self) -> dict[int, int]:
        """fan-defaults.json if valid; else a live capture saved once; else the known minimums, unsaved."""
        path, stale = self.defaults_path, None
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)["fans"]
                saved = {n: raw[str(n)]["min"] for n in self.fans}
                if all(self._plausible(n, v) for n, v in saved.items()):
                    return saved
            except (OSError, ValueError, KeyError, TypeError):
                pass
            stale = f"{path} is invalid or implausible and was ignored"
        live = dict(self.written)
        if all(self._plausible(n, v) for n, v in live.items()):
            if path:
                data = {"fans": {str(n): {"label": f["label"], "min": live[n], "max": f["max"]}
                                 for n, f in self.fans.items()},
                        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
                try:
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    tmp = path + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as fh:
                        json.dump(data, fh, indent=1)
                        fh.flush()
                        os.fsync(fh.fileno())
                    os.replace(tmp, path)
                except OSError as exc:
                    stale = (stale + "; " if stale else "") + f"cannot save {path}: {exc}"
            self._emit("fan_defaults", source="captured", mins={self.fans[n]["label"]: v for n, v in live.items()},
                       **({"warning": stale} if stale else {}))
            return live
        out = {}
        for n, v in live.items():
            if self._plausible(n, v):
                out[n] = v
            elif n in KNOWN_HW_MIN:
                out[n] = KNOWN_HW_MIN[n]
        self._emit("fan_defaults_warning", message=(stale + "; " if stale else "") +
                   f"live fan minimums {live} look like a floor already applied: not saved; restoring to "
                   f"{out} (known hardware minimums) until a later start captures plausible values")
        return out

    # control ---------------------------------------------------------------------------
    def _clamp(self, n: int, target: int, max_rpm: int) -> int:
        lo = self.defaults[n]
        mx = self.fans[n].get("max")
        hi = max(lo, min(int(max_rpm), mx) if isinstance(mx, int) else int(max_rpm))
        return lo if target <= 0 else min(max(int(target), lo), hi)

    def _write(self, n: int, rpm: int) -> str | None:
        """Write fan{n}_min and read it back; None if it landed, else why not."""
        err = self.io.write_min(n, rpm)
        if err:
            return err
        got = self.io.read_int(n, "min")
        mx = self.fans[n].get("max")
        if got is None:
            return f"fan{n}_min unreadable after writing {rpm}"
        if abs(got - rpm) > READBACK_TOL_RPM:
            return f"fan{n}_min reads {got}, wanted {rpm}"
        if isinstance(mx, int) and got > mx:
            return f"fan{n}_min reads {got}, above fan{n}_max {mx}"
        return None

    def apply(self, target_rpm: int, max_rpm: int, reason: str = "", temp_c: Any = None, pkg_w: Any = None) -> bool:
        """Write each fan's clamped floor only when it differs from what was last written. True if any changed."""
        with self.lock:
            now = self.clock()
            changed: dict[str, int] = {}
            for n, f in self.fans.items():
                if n not in self.defaults:
                    continue
                want = self._clamp(n, target_rpm, max_rpm)
                if want == self.written.get(n):
                    continue
                fail = self.failed.get(n)
                if fail and fail[0] == want and now - fail[1] < RETRY_S:
                    continue
                err = self._write(n, want)
                if err:
                    self.written[n] = None
                    self.failed[n] = (want, now)
                    self.write_errors += 1
                    self._emit("fan_floor_error", fan=f["label"], rpm=want, error=err, reason=reason)
                    continue
                self.written[n] = want
                self.failed.pop(n, None)
                changed[f["label"]] = want
            if changed:
                self.last_change_ts = now
                self._emit("fan_floor", target_rpm=target_rpm, fans=changed, temp_c=temp_c,
                           pkg_w=round(pkg_w, 2) if _num(pkg_w) else None, reason=reason)
            return bool(changed)

    def restore(self) -> bool:
        """Write the captured hardware minimums back wherever fan{N}_min differs. True if all are restored."""
        with self.lock:
            self.state, self.target_rpm = FanState(), 0
            restored: dict[str, int] = {}
            ok = True
            for n, f in self.fans.items():
                d = self.defaults.get(n)
                if d is None:
                    continue
                if self.io.read_int(n, "min") == d:
                    self.written[n] = d
                    continue
                err = self._write(n, d)
                if err:
                    ok = False
                    self.written[n] = None
                    self.write_errors += 1
                    self._emit("fan_floor_error", fan=f["label"], rpm=d, error=err, reason="restore")
                    continue
                self.written[n] = d
                self.failed.pop(n, None)
                restored[f["label"]] = d
            if restored:
                self.last_change_ts = self.clock()
                self._emit("fan_restore", fans=restored)
            return ok

    def start(self, cfg: dict[str, Any]) -> None:
        """Apply the configured mode at daemon start: auto clears any floor left behind; floor starts on the first tick."""
        with self.lock:
            self.mode = cfg["mode"]
            if not self.fans:
                self.reason = "no applesmc fans found"
            elif self.mode == "auto":
                self.restore()
                self.reason = "fan.mode auto: SMC curve only"
            else:
                self.reason = "waiting for the first sample"

    def on_config(self, cfg: dict[str, Any]) -> None:
        """A fan.* key changed: auto restores now; anything else is picked up on the next tick."""
        with self.lock:
            if cfg["mode"] == "auto" and self.mode != "auto":
                self._set_mode("auto")

    def _set_mode(self, mode: str) -> None:
        self.mode = mode
        self.state = FanState()
        self._emit("fan_mode", mode=mode)
        if mode == "auto":
            self.restore()
            self.reason = "fan.mode auto: SMC curve only"

    def tick(self, now: float, temp_c: Any, pkg_w: Any, cfg: dict[str, Any]) -> None:
        """1 Hz: follow the mode, decide the floor, write it if it changed, refresh the rpm readings."""
        with self.lock:
            if cfg["mode"] != self.mode:
                self._set_mode(cfg["mode"])
            for n, f in self.fans.items():
                f["rpm"] = self.io.read_int(n, "input")
            if not self.fans:
                self.reason = "no applesmc fans found"
                return
            if self.mode != "floor":
                return
            self.state, target, reason = decide(self.state, temp_c, pkg_w, cfg, now)
            self.target_rpm, self.reason = target, reason
            self.apply(target, cfg["max_rpm"], reason, temp_c, pkg_w)

    # reporting ----------------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        with self.lock:
            return {"mode": self.mode, "target_rpm": self.target_rpm, "reason": self.reason,
                    "fans": [{"label": f["label"], "rpm": f["rpm"], "min_written": self.written.get(n),
                              "hw_min": self.defaults.get(n), "max": f["max"]} for n, f in self.fans.items()],
                    "last_change_ts": self.last_change_ts, "write_errors": self.write_errors}

    def log_record(self) -> dict[str, Any]:
        """Compact form for the 1 Hz sample log (per-fan lists in fan number order)."""
        with self.lock:
            return {"mode": self.mode, "target_rpm": self.target_rpm, "reason": self.reason,
                    "min_written": [self.written.get(n) for n in self.fans],
                    "hw_min": [self.defaults.get(n) for n in self.fans], "write_errors": self.write_errors}
