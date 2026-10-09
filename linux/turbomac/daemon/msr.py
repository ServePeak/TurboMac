"""Read-only MSR access and decoding for RAPL limit/status registers.

Always os.open + os.pread: a buffered Python file object returned a stale
buffer slice during testing. This module never writes an MSR.
"""
from __future__ import annotations

import os
import struct
from typing import Any

from turbomac.daemon.sysfs import warn_once

MSR_RAPL_POWER_UNIT = 0x606
MSR_PKG_POWER_LIMIT = 0x610
MSR_PKG_THERM_STATUS = 0x1B1


def read_msr(dev: str, reg: int) -> int | None:
    try:
        fd = os.open(dev, os.O_RDONLY)
    except OSError as exc:
        warn_once(f"msr-open-{dev}", f"MSR device {dev} unavailable: {exc.strerror or exc}")
        return None
    try:
        data = os.pread(fd, 8, reg)
        return struct.unpack("<Q", data)[0] if len(data) == 8 else None
    except OSError as exc:
        warn_once(f"msr-{reg:x}", f"MSR {reg:#x} read failed: {exc.strerror or exc}")
        return None
    finally:
        os.close(fd)


def decode_units(v: int) -> dict[str, float]:
    return {"power_unit_w": 1.0 / (1 << (v & 0xF)),
            "energy_unit_j": 1.0 / (1 << ((v >> 8) & 0x1F)),
            "time_unit_s": 1.0 / (1 << ((v >> 16) & 0xF))}


def _window(field: int, tu: float) -> float:
    y, z = field & 0x1F, (field >> 5) & 0x3
    return (1 << y) * (1.0 + z / 4.0) * tu


def decode_pkg_limit(v: int, units: dict[str, float]) -> dict[str, Any]:
    pu, tu = units["power_unit_w"], units["time_unit_s"]
    return {
        "raw": hex(v),
        "pl1_w": (v & 0x7FFF) * pu, "pl1_enable": bool(v >> 15 & 1), "pl1_clamp": bool(v >> 16 & 1),
        "pl1_window_s": _window((v >> 17) & 0x7F, tu),
        "pl2_w": ((v >> 32) & 0x7FFF) * pu, "pl2_enable": bool(v >> 47 & 1), "pl2_clamp": bool(v >> 48 & 1),
        "pl2_window_s": _window((v >> 49) & 0x7F, tu),
        "lock": bool(v >> 63 & 1),
    }


def decode_therm_status(v: int) -> dict[str, Any]:
    return {"raw": hex(v), "thermal_status": bool(v & 1), "prochot": bool(v >> 2 & 1),
            "power_limited": bool(v >> 10 & 1), "power_limit_log": bool(v >> 11 & 1)}


class Msr:
    """Cached unit decode plus 0x610 / 0x1B1 readers; missing device gives None."""

    def __init__(self, dev: str) -> None:
        self.dev = dev
        self._units: dict[str, float] | None = None

    def units(self) -> dict[str, float] | None:
        if self._units is None:
            v = read_msr(self.dev, MSR_RAPL_POWER_UNIT)
            self._units = decode_units(v) if v is not None else None
        return self._units

    def pkg_limit(self) -> dict[str, Any] | None:
        u = self.units()
        v = read_msr(self.dev, MSR_PKG_POWER_LIMIT) if u else None
        return decode_pkg_limit(v, u) if v is not None and u else None

    def therm_status(self) -> dict[str, Any] | None:
        v = read_msr(self.dev, MSR_PKG_THERM_STATUS)
        return decode_therm_status(v) if v is not None else None
