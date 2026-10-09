"""coretemp, cpufreq, intel_pstate, throttle counters and /proc readers."""
from __future__ import annotations

import glob
import os
from typing import Any

from turbomac.daemon.paths import Paths
from turbomac.daemon.sysfs import read_int, read_text, write_text

TOP_EVERY_S = 5.0   # the /proc/<pid>/stat scan (hundreds of files) runs at most this often


def find_hwmon(hwmon_root: str, name: str) -> str | None:
    for d in sorted(glob.glob(os.path.join(hwmon_root, "hwmon*"))):
        if read_text(os.path.join(d, "name"), quiet=True) == name:
            return d
    return None


def parse_proc_stat(text: str) -> dict[str, tuple[int, int]]:
    """'cpuN ...' lines -> {name: (busy_ticks, total_ticks)}."""
    out: dict[str, tuple[int, int]] = {}
    for line in text.splitlines():
        if not line.startswith("cpu"):
            continue
        parts = line.split()
        vals = [int(x) for x in parts[1:9]]
        total = sum(vals)
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        out[parts[0]] = (total - idle, total)
    return out


def _milli(path: str) -> float | None:
    v = read_int(path, quiet=True)
    return v / 1000.0 if v is not None else None


class CpuReader:
    def __init__(self, paths: Paths, top_n: int = 8, top_every_s: float = TOP_EVERY_S) -> None:
        self.paths = paths
        self.top_n = top_n
        self.top_every_s = top_every_s
        self.coretemp = find_hwmon(paths.hwmon, "coretemp")
        self.nvme = find_hwmon(paths.hwmon, "nvme")
        self.pkg_zone = None
        for z in sorted(glob.glob(os.path.join(paths.thermal, "thermal_zone*"))):
            if read_text(os.path.join(z, "type"), quiet=True) == "x86_pkg_temp":
                self.pkg_zone = z
        self.cpus = sorted(glob.glob(os.path.join(paths.cpu, "cpu[0-9]*")),
                           key=lambda p: int(os.path.basename(p)[3:]))
        self.pstate = os.path.join(paths.cpu, "intel_pstate")
        # per-cpu files never change while the daemon runs: build the path lists once
        self._freq_paths = [os.path.join(c, "cpufreq", "scaling_cur_freq") for c in self.cpus]
        self._core_thr_paths = [os.path.join(c, "thermal_throttle", "core_throttle_count") for c in self.cpus]
        self._pkg_thr_paths = [os.path.join(c, "thermal_throttle", "package_throttle_count") for c in self.cpus]
        self._coretemp_sensors: list[tuple[str, str]] | None = None   # (temp*_input, label), found on first use
        self._stat_prev: dict[str, tuple[int, int]] | None = None
        self._procs_prev: dict[int, int] = {}
        self._procs_t: float | None = None
        self._top_cache: list[dict[str, Any]] = []
        try:
            self.hz = os.sysconf("SC_CLK_TCK")
        except (ValueError, OSError, AttributeError):
            self.hz = 100

    def _coretemp(self) -> list[tuple[str, str]]:
        """coretemp temp*_input paths with their label text, globbed once (an empty result is retried)."""
        if self._coretemp_sensors is None and self.coretemp:
            found = [(p, read_text(p.replace("_input", "_label"), "", quiet=True) or os.path.basename(p))
                     for p in sorted(glob.glob(os.path.join(self.coretemp, "temp*_input")))]
            if found:
                self._coretemp_sensors = found
            return found
        return self._coretemp_sensors or []

    def temps(self) -> dict[str, Any]:
        out: dict[str, Any] = {"cores": {}}
        if self.coretemp:
            for p, label in self._coretemp():
                val = _milli(p)
                if label.startswith("Package"):
                    out["pkg_c"] = val
                else:
                    out["cores"][label.replace("Core ", "c")] = val
        if self.pkg_zone:
            out["x86_pkg_c"] = _milli(os.path.join(self.pkg_zone, "temp"))
        if self.nvme:
            out["nvme_c"] = _milli(os.path.join(self.nvme, "temp1_input"))
        return out

    def _busy(self) -> tuple[float | None, list[float | None]]:
        txt = read_text(os.path.join(self.paths.proc, "stat"), quiet=True)
        if txt is None:
            return None, []
        cur = parse_proc_stat(txt)
        prev, self._stat_prev = self._stat_prev, cur
        if prev is None:
            return None, []

        def pct(name: str) -> float | None:
            if name not in prev or name not in cur:
                return None
            db, dt = cur[name][0] - prev[name][0], cur[name][1] - prev[name][1]
            return round(100.0 * db / dt, 1) if dt > 0 else None
        names = sorted((n for n in cur if n != "cpu"), key=lambda n: int(n[3:]))
        return pct("cpu"), [pct(n) for n in names]

    def _top(self, now: float) -> list[dict[str, Any]]:
        """Top processes by CPU% over the interval since the previous scan. Scans /proc at most every
        top_every_s of `now`; in between the previous (stale) list is returned unchanged."""
        if self._procs_t is not None and 0 <= now - self._procs_t < self.top_every_s:
            return self._top_cache
        cur: dict[int, int] = {}
        for entry in os.scandir(self.paths.proc) if os.path.isdir(self.paths.proc) else []:
            if not entry.name.isdigit():
                continue
            try:
                with open(os.path.join(entry.path, "stat"), "r") as fh:
                    fields = fh.read().rsplit(")", 1)[1].split()
                cur[int(entry.name)] = int(fields[11]) + int(fields[12])
            except (OSError, IndexError, ValueError):
                continue
        prev, prev_t = self._procs_prev, self._procs_t
        self._procs_prev, self._procs_t = cur, now
        self._top_cache = []
        if prev_t is None or now <= prev_t:
            return self._top_cache
        scale = 100.0 / (self.hz * (now - prev_t))
        deltas = sorted(((t - prev[p], p) for p, t in cur.items() if p in prev and t > prev[p]), reverse=True)
        out = []
        for d, pid in deltas[: self.top_n]:
            name = read_text(os.path.join(self.paths.proc, str(pid), "comm"), "?", quiet=True)
            out.append({"pid": pid, "name": name, "cpu_pct": round(d * scale, 1)})
        self._top_cache = out
        return out

    def read(self, now: float) -> dict[str, Any]:
        total, per_cpu = self._busy()
        freqs = [read_int(p, quiet=True) for p in self._freq_paths]
        load = (read_text(os.path.join(self.paths.proc, "loadavg"), "", quiet=True) or "").split()[:3]
        mem: dict[str, int] = {}
        for line in (read_text(os.path.join(self.paths.proc, "meminfo"), "", quiet=True) or "").splitlines():
            k, _, v = line.partition(":")
            if k in ("MemTotal", "MemAvailable"):
                mem[k] = int(v.split()[0]) // 1024
        up = (read_text(os.path.join(self.paths.proc, "uptime"), "", quiet=True) or "").split()
        core_thr = [read_int(p, quiet=True) for p in self._core_thr_paths]
        pkg_thr = [read_int(p, quiet=True) for p in self._pkg_thr_paths]
        return {
            "temps": self.temps(),
            "mhz": [round(f / 1000) if f else None for f in freqs],
            "busy_pct": total, "busy_per_cpu": per_cpu,
            "load": [float(x) for x in load] if load else None,
            "mem_total_mib": mem.get("MemTotal"), "mem_avail_mib": mem.get("MemAvailable"),
            "uptime_s": float(up[0]) if up else None,
            "no_turbo": read_int(os.path.join(self.pstate, "no_turbo"), quiet=True),
            "max_perf_pct": read_int(os.path.join(self.pstate, "max_perf_pct"), quiet=True),
            "min_perf_pct": read_int(os.path.join(self.pstate, "min_perf_pct"), quiet=True),
            "pstate": read_text(os.path.join(self.pstate, "status"), quiet=True),
            "epp": read_text(os.path.join(self.cpus[0], "cpufreq", "energy_performance_preference"), quiet=True)
            if self.cpus else None,
            "throttle_core": sum(x or 0 for x in core_thr),
            "throttle_pkg": max((x for x in pkg_thr if x is not None), default=None),
            "top": self._top(now),
        }

    def apply_settings(self, cpu_cfg: dict[str, Any]) -> list[str]:
        """Apply cpu.* settings; None means leave the kernel value alone."""
        msgs: list[str] = []

        def put(path: str, value: str) -> None:
            if not os.path.exists(path) or read_text(path, quiet=True) == value:
                return
            err = write_text(path, value)
            msgs.append(err or f"{path} = {value}")
        if cpu_cfg.get("no_turbo") is not None:
            put(os.path.join(self.pstate, "no_turbo"), "1" if cpu_cfg["no_turbo"] else "0")
        if cpu_cfg.get("max_perf_pct") is not None:
            put(os.path.join(self.pstate, "max_perf_pct"), str(int(cpu_cfg["max_perf_pct"])))
        if cpu_cfg.get("epp") is not None:
            for c in self.cpus:
                put(os.path.join(c, "cpufreq", "energy_performance_preference"), cpu_cfg["epp"])
        return msgs
