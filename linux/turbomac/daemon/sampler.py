"""4 Hz fast / 1 Hz slow sampler, trailing windows, 1 s aggregates and peaks (PLAN 4.1)."""
from __future__ import annotations

import math
import statistics
import threading
import time
from collections import deque
from typing import Any

from turbomac.daemon import governor
from turbomac.daemon.smc import SMC_SLOW_EVERY_S, due, read_fans, read_temps

FAST_HZ = 4
SLOW_EVERY = 4
HISTORY_LEN = 600
EXTRA_SMC_KEYS = ("PSTR", "PCPC", "PCPG", "PCPT", "PG0R", "PHPC")
FAST_SMC_KEYS = ("PDTR",)                    # every fast tick (4 Hz)
FULL_SMC_KEYS = ("PDTR", "VD0R", "ID0R", "ACPW")   # on the slow tick (1 Hz)
HISTORY_FIELDS = ("ts", "input_w", "pkg_w", "gpu_w", "pkg_c", "pl1_w", "state")
TEMP_FRESH_S = 3.0   # the governor ignores a package temperature older than this


class Window:
    """Timestamped values; mean/median over a trailing span."""

    def __init__(self, keep_s: float) -> None:
        self.keep_s = keep_s
        self.d: deque[tuple[float, float]] = deque()

    def push(self, t: float, v: Any) -> None:
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v):
            return
        self.d.append((t, float(v)))
        while self.d and self.d[0][0] < t - self.keep_s:
            self.d.popleft()

    def values(self, now: float, span: float) -> list[float]:
        return [v for t, v in self.d if t > now - span + 1e-9]

    def mean(self, now: float, span: float) -> float | None:
        v = self.values(now, span)
        return sum(v) / len(v) if v else None

    def median(self, now: float, span: float) -> float | None:
        v = self.values(now, span)
        return statistics.median(v) if v else None

    def max(self, now: float, span: float) -> float | None:
        v = self.values(now, span)
        return max(v) if v else None


def _valid_input(v: Any) -> bool:
    return isinstance(v, float) and math.isfinite(v) and 0.0 <= v < 250.0


def _r(v: Any, nd: int = 2) -> Any:
    return round(v, nd) if isinstance(v, float) else v


class Sampler:
    def __init__(self, smc: Any = None, rapl: Any = None, msr: Any = None, gpu: Any = None,
                 cpu: Any = None, smc_dir: str | None = None, smc_slow_every_s: float = SMC_SLOW_EVERY_S) -> None:
        self.smc, self.rapl, self.msr, self.gpu, self.cpu, self.smc_dir = smc, rapl, msr, gpu, cpu, smc_dir
        self.smc_slow_every_s = smc_slow_every_s
        self.held: dict[str, Any] = {"v": None, "a": None, "acpw_w": None}   # the last 1 Hz VD0R/ID0R/ACPW
        self._smc_slow: dict[str, Any] = {"smc": {}, "smc_temps": {}}    # extra rails + applesmc temps
        self._smc_slow_t: float | None = None
        self.extra = [k for k in EXTRA_SMC_KEYS if smc is not None and smc.has(k)]
        self.w = {k: Window(12.0) for k in ("input", "pkg", "core", "uncore", "dram", "acpw", "v", "a")}
        self.latest: dict[str, Any] = {}
        self.slow_data: dict[str, Any] = {}
        self.slow_mono: float | None = None
        self.history: deque[dict[str, Any]] = deque(maxlen=HISTORY_LEN)
        self.hist_lock = threading.Lock()
        self.peaks: dict[str, dict[str, float]] = {}
        self.last_valid_mono: float | None = None

    def _peak(self, key: str, v: Any, wall: float) -> None:
        if isinstance(v, (int, float)) and not isinstance(v, bool) and (key not in self.peaks or v > self.peaks[key]["value"]):
            self.peaks[key] = {"value": round(float(v), 2), "ts": round(wall, 3)}

    def reset_peaks(self) -> None:
        self.peaks = {}

    def fast(self, now: float, wall: float | None = None, full: bool = True) -> dict[str, Any]:
        """RAPL energy deltas for 4 zones + SMC PDTR; with full (the daemon's 1 Hz slow tick) also
        VD0R/ID0R/ACPW, whose latest v/a/acpw_w hold the last full read in between (each SMC key read
        costs two busy-waiting SMC transactions). valid: PDTR valid and an ACPW 10 s median available."""
        wall = time.time() if wall is None else wall
        p = self.rapl.power(now) if self.rapl is not None else {}
        keys = FULL_SMC_KEYS if full else FAST_SMC_KEYS
        smc = self.smc.read_many(list(keys)) if self.smc is not None and self.smc.ok else {}
        pdtr = smc.get("PDTR")
        pdtr_w = pdtr if _valid_input(pdtr) else None
        if full:
            acpw = smc.get("ACPW")
            self.held = {"v": smc.get("VD0R"), "a": smc.get("ID0R"),   # ACPW sanity: 30..100 W
                         "acpw_w": acpw / 1000.0 if type(acpw) is int and 30_000 <= acpw <= 100_000 else None}
        fresh = self.held if full else {}   # windows get each 1 Hz value once
        for key, val in (("input", pdtr_w), ("pkg", p.get("package")), ("core", p.get("core")),
                         ("uncore", p.get("uncore")), ("dram", p.get("dram")), ("acpw", fresh.get("acpw_w")),
                         ("v", fresh.get("v")), ("a", fresh.get("a"))):
            self.w[key].push(now, val)
        valid = pdtr_w is not None and self.w["acpw"].median(now, 10.0) is not None
        if valid:
            self.last_valid_mono = now
        s = {"ts": wall, "mono": now, "valid": valid, "pdtr_w": pdtr_w,
             "v": self.held["v"], "a": self.held["a"], "acpw_w": self.held["acpw_w"],
             "package_w": p.get("package"), "core_w": p.get("core"), "uncore_w": p.get("uncore"),
             "dram_w": p.get("dram")}
        self._peak("input_w", s["pdtr_w"], wall)
        self._peak("pkg_w", s["package_w"], wall)
        self._peak("a", fresh.get("a"), wall)
        self.latest = s
        return s

    def slow(self, now: float, wall: float | None = None) -> dict[str, Any]:
        """1 Hz reads: MSRs, limits, extra SMC keys, temps, fans, GPU, CPU/proc.

        The extra SMC rails and the applesmc temperatures are re-read only every smc_slow_every_s
        of `now` (the previous dicts are returned in between); fans: see read_fans.
        Gpu.read() guards itself: while off or mid-change it reads only the
        switcheroo file and PCI power_state."""
        wall = time.time() if wall is None else wall
        if due(self._smc_slow_t, now, self.smc_slow_every_s):
            self._smc_slow = {
                "smc": {k: _r(v, 3) for k, v in self.smc.read_many(self.extra).items()} if self.extra else {},
                "smc_temps": read_temps(self.smc_dir)}
            self._smc_slow_t = now
        therm = self.msr.therm_status() if self.msr is not None else None
        lim = self.msr.pkg_limit() if self.msr is not None else None
        d: dict[str, Any] = {
            "smc": self._smc_slow["smc"],
            "msr": {"power_limited": therm["power_limited"], "thermal": therm["thermal_status"],
                    "prochot": therm["prochot"]} if therm else None,
            "msr_limits": {k: lim[k] for k in ("pl1_w", "pl2_w", "pl1_enable", "pl1_clamp", "pl2_enable",
                                               "pl2_clamp", "lock")} if lim else None,
            "limits": self.rapl.read_limits() if self.rapl is not None else None,
            "smc_temps": self._smc_slow["smc_temps"],
            "fans": read_fans(self.smc_dir, now, self.smc_slow_every_s),
            "gpu": self.gpu.read() if self.gpu is not None else {},
            "cpu": self.cpu.read(now) if self.cpu is not None else {},
        }
        self._peak("gpu_w", d["gpu"].get("power_w"), wall)
        self._peak("pkg_c", d["cpu"].get("temps", {}).get("pkg_c"), wall)
        self.slow_data, self.slow_mono = d, now
        return d

    def derived(self, now: float, ceiling_w: float) -> dict[str, Any]:
        mean = self.w["input"].mean(now, 1.0)
        pkg_med = self.w["pkg"].median(now, 1.0)
        acpw = self.w["acpw"].median(now, 10.0)
        return {"input_mean_1s": mean, "input_max_1s": self.w["input"].max(now, 1.0),
                "pkg_median_1s": pkg_med, "acpw_median_10s": acpw,
                "noncpu_est_w": mean - pkg_med if mean is not None and pkg_med is not None else None,
                "headroom_w": acpw - mean if acpw is not None and mean is not None else None,
                "pred_input_at_ceiling_w": governor.predicted_input_w(ceiling_w)}

    def gov_input(self, now: float) -> dict[str, Any]:
        """What the governor step consumes."""
        fresh = self.slow_mono is not None and now - self.slow_mono <= TEMP_FRESH_S
        return {"pdtr_w": self.latest.get("pdtr_w") if self.latest.get("valid") else None,
                "input_mean_1s": self.w["input"].mean(now, 1.0),
                "acpw_w": self.w["acpw"].median(now, 10.0),
                "pkg_c": (self.slow_data.get("cpu") or {}).get("temps", {}).get("pkg_c") if fresh else None}

    def aggregate(self, now: float, wall: float, ceiling_w: float, gov: dict[str, Any],
                  fan: dict[str, Any] | None = None, derived: dict[str, Any] | None = None,
                  meter: dict[str, Any] | None = None) -> dict[str, Any]:
        """The 1 Hz record: logged as one JSONL line and kept in the 600 s ring. fan: FanController.log_record();
        derived: this tick's derived(now, ceiling_w) if already computed; meter: the daemon's own CPU/RSS."""
        d = derived if derived is not None else self.derived(now, ceiling_w)
        sd = self.slow_data
        gpu = sd.get("gpu") or {}
        rec = {
            "ts": round(wall, 3), "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(wall)),
            "input_w": _r(d["input_mean_1s"]), "input_max_w": _r(d["input_max_1s"]),
            "v": _r(self.w["v"].mean(now, 1.0), 3), "a": _r(self.w["a"].mean(now, 1.0), 3),
            "acpw_w": _r(d["acpw_median_10s"], 3),
            "pkg_w": _r(d["pkg_median_1s"]), "core_w": _r(self.w["core"].mean(now, 1.0)),
            "uncore_w": _r(self.w["uncore"].mean(now, 1.0)), "dram_w": _r(self.w["dram"].mean(now, 1.0)),
            "noncpu_w": _r(d["noncpu_est_w"]), "headroom_w": _r(d["headroom_w"]),
            "gpu_w": _r(gpu.get("power_w")), "pkg_c": (sd.get("cpu") or {}).get("temps", {}).get("pkg_c"),
            "pl1_w": gov.get("pl1_w"), "pl2_w": gov.get("pl2_w"), "state": gov.get("state"),
            "thermal_cap_w": gov.get("thermal_cap_w"),
            "smc_valid": self.latest.get("valid", False),
        }
        rec.update({k: sd.get(k) for k in ("smc", "msr", "msr_limits", "smc_temps", "fans", "gpu", "cpu")})
        lim = sd.get("limits") or {}
        rec["sysfs_limits"] = {"pl1_w": lim.get("pl1_w"), "pl2_w": lim.get("pl2_w"), "enabled": lim.get("enabled")}
        if fan is not None:
            rec["fan"] = fan
        if meter is not None:
            rec["self"] = meter
        with self.hist_lock:
            self.history.append(rec)
        return rec

    def history_slice(self, seconds: float, full: bool = False) -> list[dict[str, Any]]:
        n = max(0, min(int(seconds), HISTORY_LEN))
        with self.hist_lock:
            rows = list(self.history)[-n:] if n else []
        return rows if full else [{k: r.get(k) for k in HISTORY_FIELDS} for r in rows]
