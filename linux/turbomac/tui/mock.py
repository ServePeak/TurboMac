"""MockBackend: a deterministic simulation of old-mac behind turbomacd (no hardware, no socket).

Physics (all randomness from random.Random(seed)): idle is ~21 W input with a
6 W package and a 7 W dGPU. Every 120 s an 8-thread "whisper" burst runs for
45 s and drives the package to PL1; every other burst also loads the dGPU to its
cap. Input = 14.0 W + 1.21 x package + (GPU - 7 W) + fan power, lagging the
package by ~1 s (fit to the Phase 0 receipts: 21 W idle, 56-58 W at a 35 W cap).
Package temperature, GPU temperature and fans follow first-order lags; the fans
follow the SMC curve (74-92 °C) or the fan floor, whichever is higher, at 120 rpm/s.

The daemon's own logic runs on the simulated hardware: Sampler (4 Hz/1 Hz
windows, 1 Hz aggregate records, history, peaks), governor.step (states, cuts,
recoveries, events), rapl.clamp_limits, config.validate, the FanController (on
fake applesmc fan files) and Daemon.build_status itself, so the status shape
cannot drift from turbomacd's. In dynamic mode a GPU-loaded burst at the 40 W
ceiling crosses the shed band and gets cut.
"""
from __future__ import annotations

import calendar
import math
import random
import threading
import time
from typing import Any, Iterator

from turbomac import __version__, config
from turbomac.config import HARD_MAX_W
from turbomac.daemon import governor
from turbomac.daemon.fans import FanController
from turbomac.daemon.main import Daemon
from turbomac.daemon.rapl import clamp_limits
from turbomac.daemon.sampler import EXTRA_SMC_KEYS, Sampler
from turbomac.tui.backend import BackendError, SimBackend

DT = 0.25
ACPW_MW = 79496
BURST_PERIOD_S, BURST_START_S, BURST_LEN_S = 120.0, 30.0, 45.0
FANS = (("Left side", 2160, 5927), ("Right side", 2000, 5489))
# label, idle value (pkg 61 C / GPU 52 C on old-mac), source (c = package temp, g = GPU temp), gain
SMC_TEMPS = [(f[0], float(f[1]), f[2], float(f[3])) for f in (s.split() for s in """
TA0V 31.0 c 0.05|TC0E 63.0 c 1.0|TC0F 66.8 c 1.05|TC0P 53.5 c 0.5|TC1C 57.0 c 1.0|TC2C 57.0 c 1.0|TC3C 57.0 c 0.95
TC4C 58.0 c 1.0|TCGC 57.0 c 0.9|TCSA 57.0 c 0.9|TCTD -0.2 c 0.0|TCXC 58.0 c 1.1|TG0D 56.2 g 1.0|TG0F 56.2 g 1.0
TG0P 52.0 g 0.9|TGDD 52.0 g 1.0|TGVP 50.0 g 0.8|TH0A 44.0 c 0.3|TH0B 41.5 c 0.3|TH0C 43.8 c 0.3|TH0F -33.2 c 0.0
TH0R -33.2 c 0.0|TH0a 44.0 c 0.3|TH0b 41.5 c 0.3|TH0c 43.8 c 0.3|TM0P 50.8 c 0.2|TPCD 43.0 c 0.2|TTLD 47.2 c 0.25
TTRD 51.0 c 0.25|TW0P 57.5 c 0.2|TaLC 35.8 c 0.2|TaRC 45.8 c 0.2|Th1H 50.2 c 0.35|Th2H 51.2 c 0.35|Ts0P 32.2 c 0.15
Ts0S 41.8 c 0.15|Ts1P 30.8 c 0.15|Ts1S 41.2 c 0.15|Ts2S 40.0 c 0.15""".replace("\n", "|").split("|") if s.strip())]
IDLE_PROCS = (("python3", 8.0), ("uvicorn", 2.0), ("mongod", 2.0), ("redis-server", 1.5), ("hermes", 1.0),
              ("postgres", 1.0), ("dockerd", 0.6), ("systemd-journal", 0.3))
FIRMWARE = {"pl1_w": 100.0, "pl2_w": 125.0, "pl1_uw": 100000000, "pl2_uw": 125000000,
            "pl1_window_us": 27983872, "pl2_window_us": 2440, "enabled": 1, "captured_at": "2026-10-10T00:05:12Z"}
BOOT_ID = "e9386991-ebcf-4f25-9a71-e74a6134c095"


def _lag(cur: float, target: float, tau: float, dt: float) -> float:
    return target + (cur - target) * math.exp(-dt / tau)


class _Sim:
    """True machine state. Doubles as the fake SMC/RAPL/MSR the daemon's Sampler reads."""

    ok = True

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.t = 0.0
        self.pl1 = self.pl2 = 35.0
        self.pkg, self.input, self.pdtr = 6.0, 21.3, 21.3
        self.gpu_w, self.pkg_c, self.gpu_c = 7.02, 60.0, 52.0
        self.fan = [float(f[1]) for f in FANS]
        self.fan_min = [f[1] for f in FANS]      # fan{N}_min: the floor the SMC curve respects
        self.gpu_mode, self.gpu_cap = "auto", 15.0
        self.load = 0.45
        self.mhz, self.busy = [800] * 8, [1.0] * 8
        self.top: list[dict[str, Any]] = []
        self.table = {k: i for i, k in enumerate(("ACPW", "ID0R", "PDTR", "VD0R", *EXTRA_SMC_KEYS))}

    def burst(self) -> tuple[bool, bool]:
        """(burst running, burst also loads the dGPU)."""
        n, phase = divmod(self.t, BURST_PERIOD_S)
        return BURST_START_S <= phase < BURST_START_S + BURST_LEN_S, int(n) % 2 == 1

    def fan_frac(self) -> float:
        return sum((rpm - lo) / (hi - lo) for rpm, (_, lo, hi) in zip(self.fan, FANS)) / len(FANS)

    def step(self, dt: float, cfg: dict[str, Any]) -> None:
        g = self.rng.gauss
        self.t += dt
        active, gpu_load = self.burst()
        cpu = cfg["cpu"]
        demand = 44.0 if active else 6.0 + abs(g(0, 0.8))
        if cpu.get("no_turbo"):
            demand *= 0.65
        if cpu.get("max_perf_pct") is not None:
            demand *= max(0.3, cpu["max_perf_pct"] / 100.0)
        target = min(demand, self.pl1)
        self.pkg = min(self.pl2, max(0.5, _lag(self.pkg, target, 0.4, dt) + g(0, 0.15)))
        if self.gpu_mode == "off":
            gpu_target = 0.0
        elif active and gpu_load:
            gpu_target = min(self.gpu_cap if self.gpu_mode == "auto" else 10.0, 14.6) - 0.3
        else:
            gpu_target = 7.02 if self.gpu_mode == "auto" else 6.9
        self.gpu_w = max(0.0, _lag(self.gpu_w, gpu_target, 1.5, dt) + (g(0, 0.05) if gpu_target else 0.0))
        in_target = 14.0 + 1.21 * self.pkg + (self.gpu_w - 7.02) + 2.0 * self.fan_frac()
        self.input = _lag(self.input, in_target, 0.8, dt)
        self.pdtr = max(0.0, self.input + g(0, 0.35))
        self.pkg_c = _lag(self.pkg_c, 50.0 + 1.0 * self.pkg - 6.0 * self.fan_frac(), 20.0, dt)
        self.gpu_c = _lag(self.gpu_c, 45.0 + 1.0 * self.gpu_w, 15.0, dt) if self.gpu_mode != "off" else self.gpu_c
        frac = min(1.0, max(0.0, (self.pkg_c - 74.0) / 18.0))
        for i, (_, lo, hi) in enumerate(FANS):
            want = max(lo + frac * (hi - lo), self.fan_min[i])
            self.fan[i] += max(-120 * dt, min(120 * dt, want - self.fan[i]))
        self.load = _lag(self.load, 8.0 if active else 0.4, 60.0, dt)

    def slow_tick(self) -> None:
        """Per-second CPU details (frequencies, busy, top processes)."""
        r, active = self.rng, self.burst()[0]
        if active:
            self.mhz = [int(1700 + 33 * self.pkg + r.uniform(-40, 40)) for _ in range(8)]
            self.busy = [round(r.uniform(96.5, 100.0), 1) for _ in range(8)]
            procs = [("whisper-cli", 780.0 + r.uniform(0, 15))] + [(n, v) for n, v in IDLE_PROCS[:7]]
        else:
            self.mhz = [r.choice((800, 800, 1200, 2100, 2900, 3600)) for _ in range(8)]
            self.busy = [round(r.uniform(0.5, 8.0), 1) for _ in range(8)]
            procs = list(IDLE_PROCS)
        self.top = [{"pid": 1000 + 7919 * i % 99991, "name": n, "cpu_pct": round(v * r.uniform(0.7, 1.3), 1)}
                    for i, (n, v) in enumerate(procs)]
        self.top.sort(key=lambda p: -p["cpu_pct"])

    # fake SMC ------------------------------------------------------------------
    def has(self, key: str) -> bool:
        return key in EXTRA_SMC_KEYS

    def read_many(self, keys: list[str]) -> dict[str, Any]:
        v = 20.32 - 0.045 * self.input / 20.0
        a = self.pdtr / v
        core = max(0.0, self.pkg * 0.88 - 0.4)
        vals = {"PDTR": self.pdtr, "VD0R": v, "ID0R": a, "ACPW": ACPW_MW, "PSTR": 0.0, "PCPC": core - 0.4,
                "PCPG": 0.0, "PCPT": self.pkg + 0.5, "PG0R": self.gpu_w * 0.74, "PHPC": self.pkg + 5.7}
        return {k: vals.get(k) for k in keys}

    # fake RAPL / MSR --------------------------------------------------------------
    def power(self, now: float) -> dict[str, float]:
        core = max(0.0, self.pkg * 0.88 - 0.4)
        return {"package": self.pkg, "core": core, "uncore": 0.0, "dram": 0.55 + 0.012 * self.pkg}

    def read_limits(self) -> dict[str, Any]:
        return {"pl1_uw": int(round(self.pl1 * 1e6)), "pl2_uw": int(round(self.pl2 * 1e6)), "pl1_w": self.pl1,
                "pl2_w": self.pl2, "pl1_window_us": 27983872, "pl2_window_us": 2440,
                "max_power_uw": 45000000, "enabled": 1}

    def therm_status(self) -> dict[str, Any]:
        return {"power_limited": self.burst()[0] and self.pkg >= self.pl1 - 0.8,
                "thermal_status": self.pkg_c >= 97.0, "prochot": False}

    def pkg_limit(self) -> dict[str, Any]:
        return {"pl1_w": self.pl1, "pl2_w": self.pl2, "pl1_enable": True, "pl1_clamp": True,
                "pl2_enable": True, "pl2_clamp": True, "lock": False}

    def smc_temps(self) -> dict[str, float]:
        return {lab: round(base + gain * ((self.pkg_c - 61.0) if src == "c" else (self.gpu_c - 52.0)), 1)
                for lab, base, src, gain in SMC_TEMPS}

    def fans(self) -> list[dict[str, Any]]:
        return [{"label": lab, "rpm": int(rpm), "min": fmin, "max": hi, "manual": 0}
                for rpm, fmin, (lab, lo, hi) in zip(self.fan, self.fan_min, FANS)]


class _FanIO:
    """Fake applesmc fan{N}_* files for the real FanController; the simulated SMC clamps fan{N}_min to min..max."""

    def __init__(self, sim: _Sim) -> None:
        self.sim = sim

    def numbers(self) -> list[int]:
        return [i + 1 for i in range(len(FANS))]

    def read_int(self, n: int, attr: str) -> int | None:
        return {"input": int(self.sim.fan[n - 1]), "min": self.sim.fan_min[n - 1], "max": FANS[n - 1][2]}.get(attr)

    def read_text(self, n: int, attr: str) -> str | None:
        return FANS[n - 1][0] if attr == "label" else None

    def write_min(self, n: int, rpm: int) -> str | None:
        _, lo, hi = FANS[n - 1]
        self.sim.fan_min[n - 1] = min(max(int(rpm), lo), hi)
        return None


class _Gpu:
    """Fake amdgpu: Gpu.read()'s shape, including the reduced read while off or busy."""

    def __init__(self, sim: _Sim) -> None:
        self.sim = sim
        self.busy = False

    def read(self) -> dict[str, Any]:
        s, off = self.sim, self.sim.gpu_mode == "off"
        out: dict[str, Any] = {"mode": s.gpu_mode, "present": True, "power_state": "D3cold" if off else "D0",
                               "switcheroo": {"IGD": "+:Pwr" if off else " :Pwr", "DIS-Audio": " :DynOff",
                                              "DIS": " :Off" if off else "+:Pwr"}}
        if off or self.busy:
            return out
        loaded = s.gpu_w > 8.0
        low = s.gpu_mode == "low"
        out.update({"power_w": round(s.gpu_w, 3), "cap_w": 10.0 if low else s.gpu_cap, "cap_min_w": 0.0,
                    "cap_max_w": 35.0, "temp_c": float(round(s.gpu_c)),
                    "busy_pct": int(s.rng.uniform(55, 70)) if loaded else int(s.rng.uniform(0, 2)),
                    "sclk_mhz": (300 if low else 907) if loaded else 214, "dpm_level": "low" if low else "auto",
                    "runtime_status": "active"})
        return out


class _Cpu:
    """Fake CpuReader.read()."""

    def __init__(self, sim: _Sim, cfg: Any) -> None:
        self.sim, self.cfg = sim, cfg

    def read(self, now: float) -> dict[str, Any]:
        s = self.sim
        s.slow_tick()
        cpu = self.cfg()["cpu"]
        cores = {f"c{i}": float(round(s.pkg_c - (0, 1, 3, 2)[i] + s.rng.uniform(-1, 1))) for i in range(4)}
        mem_avail = 9800 if s.burst()[0] else 12026
        return {"temps": {"cores": cores, "pkg_c": float(round(s.pkg_c)), "x86_pkg_c": float(round(s.pkg_c + 2)),
                          "nvme_c": round(38.85 + 0.05 * (s.pkg_c - 60), 2)},
                "mhz": list(s.mhz), "busy_pct": round(sum(s.busy) / 8, 1), "busy_per_cpu": list(s.busy),
                "load": [round(s.load, 2), round(s.load * 0.8 + 0.1, 2), round(s.load * 0.5 + 0.2, 2)],
                "mem_total_mib": 15372, "mem_avail_mib": mem_avail, "uptime_s": round(352_800.0 + s.t, 2),
                "no_turbo": 1 if cpu.get("no_turbo") else 0, "max_perf_pct": cpu.get("max_perf_pct") or 100,
                "min_perf_pct": 20, "pstate": "active", "epp": cpu.get("epp") or "balance_performance",
                "throttle_core": 107628, "throttle_pkg": 37877, "top": list(s.top)}


def _utc(*ymdhms: int) -> float:
    return float(calendar.timegm((*ymdhms, 0, 0, 0)))


def _incident(iid: str, boot: str, start: float, rows: list[tuple[float, float, float, float, str, float]],
              top: list[tuple[str, float]], journal: list[str]) -> dict[str, Any]:
    samples = []
    for i, (inp, pkg, gpu, pl1, state, temp) in enumerate(rows):
        ts = start + i
        samples.append({"ts": ts, "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)), "input_w": inp,
                        "input_max_w": round(inp + 0.6, 2), "acpw_w": 79.496, "pkg_w": pkg, "gpu_w": gpu,
                        "pkg_c": temp, "pl1_w": pl1, "pl2_w": pl1 if pl1 < 100 else 125.0, "state": state,
                        "headroom_w": round(79.496 - inp, 2), "noncpu_w": round(inp - pkg, 2), "smc_valid": True,
                        "cpu": {"top": [{"pid": 4000 + j, "name": n, "cpu_pct": v} for j, (n, v) in enumerate(top)]}})
    stamp = iid.split("-")[0]
    return {"id": iid, "detected_at": stamp, "boot_id": boot, "current_boot_id": BOOT_ID,
            "reason": "previous boot journal does not end in 'Journal stopped'", "marker_present": False,
            "last_sample_time": samples[-1]["time"], "samples": samples, "journal_tail": journal}


def make_incidents(rng: random.Random) -> list[dict[str, Any]]:
    """Two fake incidents shaped like incidents.check_previous_boot's records (newest first)."""
    g = rng.gauss
    idle = [(round(21.0 + g(0, 0.5), 2), round(6.0 + abs(g(0, 0.7)), 2), 7.02, 35.0, "STATIC",
             float(round(60 + g(0, 0.6)))) for _ in range(60)]
    oct5 = _incident("20261005T091447Z-7c1e2a90", "7c1e2a90-55d3-4b8e-9d0e-2f4c1a6b8e11", _utc(2026, 10, 5, 9, 11, 45), idle,
                     [n_v for n_v in IDLE_PROCS],
                     ["Oct 05 09:12:01 old-mac systemd[1]: Starting sysstat-collect.service - system activity "
                      "accounting tool...", "Oct 05 09:12:01 old-mac systemd[1]: sysstat-collect.service: "
                      "Deactivated successfully.", "Oct 05 09:12:01 old-mac systemd[1]: Finished "
                      "sysstat-collect.service - system activity accounting tool.",
                      "Oct 05 09:12:30 old-mac hermes[2485]: heartbeat ok (queue 0)",
                      "Oct 05 09:12:41 old-mac kernel: applesmc: send_byte(0x00, 0x0300) fail: 0x40"])
    load = []
    for i in range(60):
        inp = min(78.3, 66.0 + 0.21 * i + g(0, 0.4))
        load.append((round(inp, 2), round(min(45.0, 41.0 + 0.07 * i), 2), 7.1, 100.0, "OFF", float(min(99, 92 + i // 8))))
    sep29 = _incident("20260929T184212Z-41d0b7e3", "41d0b7e3-0c2a-4f61-8a55-93b2d7e0c4aa", _utc(2026, 9, 29, 18, 40, 10), load,
                      [("whisper-cli", 788.0), ("stress-ng-vm", 96.0), ("python3", 6.0), ("dockerd", 1.0)],
                      ["Sep 29 18:40:31 old-mac kernel: mce: CPU2: Package temperature above threshold, cpu clock "
                       "throttled", "Sep 29 18:40:31 old-mac kernel: mce: CPU2: Core temperature/speed normal",
                       "Sep 29 18:40:58 old-mac stress-pilot[91822]: step 7 input 77.9 W pkg 44.8 W",
                       "Sep 29 18:41:09 old-mac stress-pilot[91822]: step 8 input 78.3 W pkg 45.0 W"])
    return [oct5, sep29]


def make_smc_keys() -> list[dict[str, Any]]:
    names = [("ACPW", "ui32", 4), ("ID0R", "flt", 4), ("PDTR", "flt", 4), ("VD0R", "flt", 4)]
    names += [(k, "flt", 4) for k in EXTRA_SMC_KEYS] + [(lab, "sp78", 2) for lab, *_ in SMC_TEMPS]
    names += [(f"F{i}{s}", "fpe2", 2) for i in range(2) for s in ("Ac", "Mn", "Mx", "Tg")]
    return [{"name": n, "index": i * 7 + 3, "type": t, "len": ln} for i, (n, t, ln) in enumerate(sorted(names))]


class MockBackend(SimBackend):
    """Deterministic for a given seed: the same seed gives the same sequence of samples."""

    kind = "mock"
    config_path = "/etc/turbomac/config.json (mock, in memory)"

    def __init__(self, seed: int = 1, warmup_s: float = 600.0, config_updates: dict[str, Any] | None = None,
                 gpu_delay_s: float = 0.8) -> None:
        super().__init__()
        self.seed = seed
        self.rng = random.Random(seed)
        self.gpu_delay_s = gpu_delay_s
        self.sim = _Sim(self.rng)
        self.smc = self.sim
        self.gpu = _Gpu(self.sim)
        self.sampler = Sampler(self.sim, self.sim, self.sim, self.gpu, _Cpu(self.sim, lambda: self.config), None)
        self.t, self.ticks = 0.0, 0
        self.mono0 = 10_000.0
        self.wall0 = time.time() - warmup_s
        self.started_wall = self.wall0
        self.boot_id = BOOT_ID
        self.firmware = dict(FIRMWARE)
        self.gov = governor.initial(self.mono0, self.firmware)
        self.readback: dict[str, Any] | None = None
        self.last_write: dict[str, Any] | None = None
        self.gpu_result: dict[str, Any] | None = None
        self.incident_list = make_incidents(random.Random(seed + 1))
        self.incident_check = {"verdict": "abrupt", "reason": "previous boot journal does not end in "
                               "'Journal stopped'", "incident_id": self.incident_list[0]["id"]}
        self._snapshot: dict[str, Any] | None = None
        self.meter: dict[str, Any] | None = None   # Daemon.meter: turbomacd's own CPU/RSS (simulated)
        self.emit("boot_check", **self.incident_check)
        self.fanctl = FanController(None, self, io=_FanIO(self.sim), clock=self.wall)
        self.fanctl.start(self.config["fan"])
        self.emit("started", version=__version__, mode=self.config["governor"]["mode"],
                  smc_keys=len(make_smc_keys()), firmware=self.firmware)
        self.gpu_result = {"ok": True, "mode": "auto", "reason": "auto applied",
                           "steps": ["write 15000000 -> power1_cap", "write auto -> power_dpm_force_performance_level"]}
        self.emit("gpu_mode", requested="auto", attempt=0, **self.gpu_result)
        if config_updates:
            self.config_set(config_updates)
        self.advance(warmup_s)
        self.connected = True

    @property
    def label(self) -> str:
        return f"mock seed {self.seed}"

    def os_name(self) -> str | None:
        return "Ubuntu 26.04"

    def wall(self) -> float:
        return self.wall0 + self.t

    # simulation -------------------------------------------------------------------
    def advance(self, seconds: float) -> None:
        with self.lock:
            for _ in range(max(1, int(round(seconds / DT)))):
                self._tick()

    def _tick(self) -> None:
        self.t += DT
        now, wall = self.mono0 + self.t, self.wall()
        self.sim.step(DT, self.config)
        slow = self.ticks % 4 == 0
        self.sampler.fast(now, wall, full=slow)   # as in Daemon.tick
        if slow:
            sd = self.sampler.slow(now, wall)
            sd["smc_temps"], sd["fans"] = self.sim.smc_temps(), self.sim.fans()
        sample = self.sampler.gov_input(now)
        sample["readback"], self.readback = self.readback, None
        self.gov, actions = governor.step(self.gov, sample, self.config, now)
        for a in actions:
            if a["op"] == "event":
                self.emit(a["type"], **{k: v for k, v in a.items() if k not in ("op", "type")})
            elif a["op"] == "set_limits":
                pl1, pl2 = clamp_limits(a["pl1_w"], a["pl2_w"], HARD_MAX_W, self.firmware)
                self.sim.pl1, self.sim.pl2 = pl1, pl2
                self.readback = {"ok": True, "reason": "sysfs and MSR match"}
                self.last_write = {"ts": round(wall, 3), "why": a["reason"], "ok": True,
                                   "reason": "sysfs and MSR match", "pl1_w": pl1, "pl2_w": pl2}
                self.emit("write", **self.last_write)
        if slow:   # as in Daemon.tick: the fan step runs after the governor, before the log record
            temp = ((sd.get("cpu") or {}).get("temps") or {}).get("pkg_c")
            der = self.sampler.derived(now, self.config["governor"]["ceiling_w"])
            self.fanctl.tick(now, temp, der["pkg_median_1s"], self.config["fan"])
            self.meter = {"cpu_pct": 1.6 if self.sim.burst()[0] else 1.1, "rss_mib": 38.6}
            self.sampler.aggregate(now, wall, self.config["governor"]["ceiling_w"], governor.summary(self.gov),
                                   fan=self.fanctl.log_record(), derived=der, meter=self.meter)
        self.ticks += 1
        self._snapshot = None

    def on_config(self, accepted: dict[str, Any]) -> None:
        if any(k.startswith("fan.") for k in accepted):
            self.fanctl.on_config(self.config["fan"])
        if "gpu.cap_w" in accepted:
            self.sim.gpu_cap = float(accepted["gpu.cap_w"])
        for k, v in accepted.items():
            if k.startswith("cpu.") and v is not None:
                self.emit("cpu_setting", message=f"{k} = {v}")

    # Backend API -------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        with self.lock:
            if self._snapshot is None:
                st = Daemon.build_status(self, self.mono0 + self.t)  # type: ignore[arg-type]
                st.update(ts=round(self.wall(), 3), host="old-mac", daemon_uptime_s=round(self.t, 1))
                self._snapshot = st
            return self._snapshot

    def _stream(self, stop: threading.Event) -> Iterator[dict[str, Any]]:
        while not stop.is_set():
            self.advance(self.period_s)
            yield self.status()
            stop.wait(self.period_s)

    def history(self, seconds: float = 600, full: bool = False) -> list[dict[str, Any]]:
        return self.sampler.history_slice(seconds, full)

    def gpu_set(self, mode: str) -> dict[str, Any]:
        if mode not in ("auto", "low", "off"):
            raise BackendError("mode must be auto, low or off")
        with self.lock:
            if self.gpu.busy:
                raise BackendError("a GPU mode change is already running")
            self.gpu.busy = True
            self.emit("gpu_mode_request", mode=mode)
            was_off = self.sim.gpu_mode == "off"
        if self.gpu_delay_s:
            time.sleep(self.gpu_delay_s * (3 if mode == "off" else 1))
        with self.lock:
            steps: list[str] = []
            if mode == "off":
                steps += ["already off"] if was_off else [
                    "fbcon unbind", "switcheroo IGD", "switcheroo OFF", "verified immediate: DIS Off, power_state D3cold",
                    "verified after 10 s: DIS Off, power_state D3cold"]
                res = {"ok": True, "mode": "off", "reason": "dGPU off (switcheroo DIS Off)", "steps": steps}
            else:
                if was_off:
                    steps += ["switcheroo ON", "switcheroo DIS", "fbcon bind"]
                cap = 10.0 if mode == "low" else float(self.config["gpu"]["cap_w"])
                steps += [f"write {int(cap * 1e6)} -> power1_cap",
                          f"write {'low' if mode == 'low' else 'auto'} -> power_dpm_force_performance_level"]
                res = {"ok": True, "mode": mode, "reason": f"{mode} applied", "steps": steps}
            self.sim.gpu_mode = mode
            self.config = config.apply_updates(self.config, {"gpu.mode": mode})
            res["persisted"] = True
            self.gpu_result = res
            self.gpu.busy = False
            self.last_change_ts = self.wall()
            self.emit("gpu_mode", requested=mode, **res)
            self._snapshot = None
            return res

    def incidents(self) -> list[dict[str, Any]]:
        return [{k: inc.get(k) for k in ("id", "detected_at", "boot_id", "last_sample_time", "reason")}
                | {"n_samples": len(inc["samples"])} for inc in self.incident_list]

    def incident(self, incident_id: str) -> dict[str, Any]:
        for inc in self.incident_list:
            if inc["id"] == incident_id:
                return inc
        raise BackendError(f"no incident {incident_id!r}")

    def smc_keys(self) -> list[dict[str, Any]]:
        return make_smc_keys()
