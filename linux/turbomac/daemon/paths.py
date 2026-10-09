"""Every filesystem path the daemon touches, derived from one sysroot prefix."""
from __future__ import annotations

import glob
import os
from dataclasses import dataclass


@dataclass
class Paths:
    sysroot: str = "/"
    socket: str = ""
    config: str = ""
    state: str = ""

    def __post_init__(self) -> None:
        self.socket = self.socket or self.sys("run/turbomac/turbomacd.sock")
        self.config = self.config or self.sys("etc/turbomac/config.json")
        self.state = self.state or self.sys("var/lib/turbomac")

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Paths":
        e = os.environ if env is None else env
        return cls(sysroot=e.get("TURBOMAC_SYSROOT", "/"), socket=e.get("TURBOMAC_SOCKET", ""),
                   config=e.get("TURBOMAC_CONFIG", ""), state=e.get("TURBOMAC_STATE", ""))

    def sys(self, rel: str) -> str:
        return os.path.join(self.sysroot, rel.lstrip("/"))

    # hardware ---------------------------------------------------------------
    def smc_dir(self) -> str | None:
        hits = sorted(glob.glob(self.sys("sys/devices/platform/applesmc.*")))
        return hits[0] if hits else None

    @property
    def powercap(self) -> str:
        return self.sys("sys/class/powercap/intel-rapl:0")

    @property
    def msr_dev(self) -> str:
        return self.sys("dev/cpu/0/msr")

    @property
    def drm(self) -> str:
        return self.sys("sys/class/drm")

    @property
    def hwmon(self) -> str:
        return self.sys("sys/class/hwmon")

    @property
    def thermal(self) -> str:
        return self.sys("sys/class/thermal")

    @property
    def cpu(self) -> str:
        return self.sys("sys/devices/system/cpu")

    @property
    def proc(self) -> str:
        return self.sys("proc")

    @property
    def switcheroo(self) -> str:
        return self.sys("sys/kernel/debug/vgaswitcheroo/switch")

    @property
    def vtconsole(self) -> str:
        return self.sys("sys/class/vtconsole")

    @property
    def boot_id(self) -> str:
        return self.sys("proc/sys/kernel/random/boot_id")

    # state ------------------------------------------------------------------
    @property
    def samples_dir(self) -> str:
        return os.path.join(self.state, "samples")

    @property
    def incidents_dir(self) -> str:
        return os.path.join(self.state, "incidents")

    @property
    def smc_keys_cache(self) -> str:
        return os.path.join(self.state, "smc-keys.json")

    @property
    def firmware_limits(self) -> str:
        return os.path.join(self.state, "firmware-limits.json")

    @property
    def fan_defaults(self) -> str:
        return os.path.join(self.state, "fan-defaults.json")

    @property
    def clean_marker(self) -> str:
        return os.path.join(self.state, "clean-shutdown")

    @property
    def last_boot_checked(self) -> str:
        return os.path.join(self.state, "last-boot-checked")
