"""Shared test helpers: import path and a fake sysroot tree."""
from __future__ import annotations

import os
import struct
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

FIXTURES = os.path.join(ROOT, "tests", "fixtures")


def write(path: str, content: str | bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb" if isinstance(content, bytes) else "w") as fh:
        fh.write(content)


def read(path: str) -> str:
    with open(path) as fh:
        return fh.read().strip()


SWITCHEROO_ON = "0:IGD: :Pwr:0000:00:02.0\n2:DIS:+:Pwr:0000:01:00.0\n"


def add_amdgpu(root: str) -> tuple[str, str]:
    """Radeon Pro 560 as card2 (+ an Intel card0), switcheroo and fbcon vtconsole. Returns (dev, hwmon)."""
    dev = os.path.join(root, "sys/class/drm/card2/device")
    hw = os.path.join(dev, "hwmon/hwmon5")
    for f, v in {"vendor": "0x1002", "gpu_busy_percent": "3", "power_dpm_force_performance_level": "auto",
                 "pp_dpm_sclk": "0: 214Mhz *\n1: 481Mhz\n", "power/runtime_status": "active",
                 "power_state": "D0"}.items():
        write(os.path.join(dev, f), v + "\n")
    for f, v in {"power1_input": "7020000", "power1_cap": "35000000", "power1_cap_min": "0",
                 "power1_cap_max": "35000000", "temp1_input": "50000"}.items():
        write(os.path.join(hw, f), v + "\n")
    write(os.path.join(root, "sys/class/drm/card0/device/vendor"), "0x8086\n")
    write(os.path.join(root, "sys/kernel/debug/vgaswitcheroo/switch"), SWITCHEROO_ON)
    write(os.path.join(root, "sys/class/vtconsole/vtcon0/name"), "(S) dummy device\n")
    write(os.path.join(root, "sys/class/vtconsole/vtcon1/name"), "(M) frame buffer device\n")
    write(os.path.join(root, "sys/class/vtconsole/vtcon1/bind"), "1\n")
    return dev, hw


def add_applesmc_fans(root: str, mins: tuple[int, int] = (2160, 2000)) -> str:
    """applesmc.768 with old-mac's two fans (min as given, hardware max 5927 / 5489). Returns the dir."""
    base = os.path.join(root, "sys/devices/platform/applesmc.768")
    for n, (label, mn, mx, rpm) in enumerate((("Left side", mins[0], 5927, 2161),
                                               ("Right side", mins[1], 5489, 1999)), start=1):
        for attr, v in {"label": label, "input": rpm, "min": mn, "max": mx, "manual": 0, "output": rpm}.items():
            write(os.path.join(base, f"fan{n}_{attr}"), f"{v}\n")
    return base


def add_coretemp(root: str, pkg_c: float) -> str:
    """coretemp hwmon with a package sensor at pkg_c. Returns the temp1_input path."""
    hw = os.path.join(root, "sys/class/hwmon/hwmon3")
    write(os.path.join(hw, "name"), "coretemp\n")
    write(os.path.join(hw, "temp1_label"), "Package id 0\n")
    write(os.path.join(hw, "temp1_input"), f"{int(pkg_c * 1000)}\n")
    return os.path.join(hw, "temp1_input")


def no_journal(args: list[str]) -> None:
    """Stub journalctl: never consult the host journal from tests (verdict 'unknown')."""
    return None


def make_daemon(paths, events=None):
    """A Daemon wired to a fake sysroot and the stub journal, hermetic on any host."""
    from turbomac.daemon.main import Daemon
    return Daemon(paths, events, journal=no_journal)


class FakeSys:
    """A temp sysroot with RAPL zones, optional MSR file and boot id."""

    def __init__(self, msr: dict[int, int] | None = None) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        z = self.p("sys/class/powercap/intel-rapl:0")
        self.zone = z
        for name, val in {"name": "package-0", "energy_uj": "1000000", "max_energy_range_uj": "262143328850",
                          "enabled": "1", "constraint_0_name": "long_term",
                          "constraint_0_power_limit_uw": "100000000", "constraint_0_time_window_us": "27983872",
                          "constraint_0_max_power_uw": "45000000", "constraint_1_name": "short_term",
                          "constraint_1_power_limit_uw": "125000000",
                          "constraint_1_time_window_us": "2440"}.items():
            write(os.path.join(z, name), val + "\n")
        for i, sub in enumerate(("core", "uncore", "dram")):
            write(f"{z}:{i}/name", sub + "\n")
            write(f"{z}:{i}/energy_uj", "500000\n")
            write(f"{z}:{i}/max_energy_range_uj", "262143328850\n")
        write(self.p("proc/sys/kernel/random/boot_id"), "cur-boot-0000\n")
        if msr is not None:
            for reg, val in msr.items():
                self.set_msr(reg, val)

    def p(self, rel: str) -> str:
        return os.path.join(self.root, rel)

    def set_msr(self, reg: int, val: int) -> None:
        path = self.p("dev/cpu/0/msr")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "ab"):
            pass
        with open(path, "r+b") as fh:
            fh.seek(reg)
            fh.write(struct.pack("<Q", val))

    def cleanup(self) -> None:
        self.tmp.cleanup()
