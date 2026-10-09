"""intel-rapl powercap: energy counters -> watts, PL1/PL2 write and readback."""
from __future__ import annotations

import glob
import math
import os
from typing import Any

from turbomac.daemon.sysfs import read_int, read_text, write_text

MSR_TOLERANCE_W = 0.125  # MSR power unit is 1/8 W on this CPU


def w_to_uw(w: float) -> int:
    return int(round(w * 1_000_000))


def energy_delta_w(prev_uj: int, cur_uj: int, max_range_uj: int, dt_s: float) -> float | None:
    """Average watts between two energy_uj readings, handling one counter wrap."""
    if dt_s <= 0:
        return None
    delta = cur_uj - prev_uj
    if delta < 0:
        if max_range_uj <= 0:
            return None
        delta += max_range_uj
    return delta / 1e6 / dt_s


def _num(v: Any) -> bool:
    return type(v) in (int, float) and math.isfinite(v) and v > 0


def valid_firmware(fw: Any) -> bool:
    """Usable as a clamp: numeric pl1_w/pl2_w and not flagged suspect."""
    return isinstance(fw, dict) and not fw.get("suspect") and _num(fw.get("pl1_w")) and _num(fw.get("pl2_w"))


def floor_eighth(w: float) -> float:
    """Round down to the 1/8 W RAPL power unit (the kernel rounds down the same way)."""
    return math.floor(w * 8 + 1e-9) / 8


def clamp_limits(pl1_w: float, pl2_w: float, hard_max_w: float,
                 firmware: dict[str, Any] | None) -> tuple[float, float]:
    """Never above hard_max_w or valid firmware limits; rounded down to 1/8 W."""
    m1 = m2 = hard_max_w
    if valid_firmware(firmware):
        assert firmware is not None
        m1, m2 = min(m1, firmware["pl1_w"]), min(m2, firmware["pl2_w"])
    return floor_eighth(min(pl1_w, m1)), floor_eighth(min(pl2_w, m2))


def compare_limits(want_pl1: float, want_pl2: float, sysfs: dict[str, Any] | None,
                   msr: dict[str, Any] | None) -> tuple[bool, str]:
    """Readback check: sysfs must match to the microwatt, MSR within 1/8 W."""
    if not sysfs or sysfs.get("pl1_uw") is None or sysfs.get("pl2_uw") is None:
        return False, "sysfs readback unavailable"
    if sysfs["pl1_uw"] != w_to_uw(want_pl1) or sysfs["pl2_uw"] != w_to_uw(want_pl2):
        return False, (f"sysfs reads PL1 {sysfs['pl1_uw'] / 1e6:g} W / PL2 {sysfs['pl2_uw'] / 1e6:g} W, "
                       f"wanted {want_pl1:g} / {want_pl2:g}")
    if sysfs.get("enabled") != 1:
        return False, "sysfs enabled != 1"
    if msr is None:
        return True, "sysfs ok (MSR unavailable)"
    if abs(msr["pl1_w"] - want_pl1) > MSR_TOLERANCE_W or abs(msr["pl2_w"] - want_pl2) > MSR_TOLERANCE_W:
        return False, f"MSR 0x610 reads PL1 {msr['pl1_w']:g} W / PL2 {msr['pl2_w']:g} W, wanted {want_pl1:g} / {want_pl2:g}"
    if not msr["pl1_enable"]:
        return False, "MSR 0x610 PL1 not enabled"
    return True, "sysfs and MSR ok"


class Rapl:
    """Package zone plus core/uncore/dram subzones under a powercap directory."""

    def __init__(self, zone: str) -> None:
        self.zone = zone
        self.zones: dict[str, str] = {"package": zone}
        for sub in sorted(glob.glob(zone + ":*")):
            name = read_text(os.path.join(sub, "name"), quiet=True)
            if name in ("core", "uncore", "dram"):
                self.zones[name] = sub
        self.max_range = {k: read_int(os.path.join(z, "max_energy_range_uj"), 0, quiet=True) or 0
                          for k, z in self.zones.items()}
        self._last: dict[str, tuple[float, int]] = {}
        self.c_long, self.c_short = 0, 1
        for i in (0, 1):
            name = read_text(os.path.join(zone, f"constraint_{i}_name"), quiet=True)
            if name == "long_term":
                self.c_long = i
            elif name == "short_term":
                self.c_short = i

    def available(self) -> bool:
        return os.path.exists(os.path.join(self.zone, "energy_uj"))

    def power(self, now: float) -> dict[str, float | None]:
        """Watts per zone since the previous call (None on the first call)."""
        out: dict[str, float | None] = {}
        for name, z in self.zones.items():
            cur = read_int(os.path.join(z, "energy_uj"))
            prev = self._last.get(name)
            if cur is None:
                out[name] = None
                continue
            self._last[name] = (now, cur)
            out[name] = energy_delta_w(prev[1], cur, self.max_range[name], now - prev[0]) if prev else None
        return out

    def _c(self, i: int, attr: str) -> str:
        return os.path.join(self.zone, f"constraint_{i}_{attr}")

    def read_limits(self) -> dict[str, Any]:
        pl1 = read_int(self._c(self.c_long, "power_limit_uw"))
        pl2 = read_int(self._c(self.c_short, "power_limit_uw"))
        return {"pl1_uw": pl1, "pl2_uw": pl2,
                "pl1_w": pl1 / 1e6 if pl1 is not None else None,
                "pl2_w": pl2 / 1e6 if pl2 is not None else None,
                "pl1_window_us": read_int(self._c(self.c_long, "time_window_us"), quiet=True),
                "pl2_window_us": read_int(self._c(self.c_short, "time_window_us"), quiet=True),
                "max_power_uw": read_int(self._c(self.c_long, "max_power_uw"), quiet=True),
                "enabled": read_int(os.path.join(self.zone, "enabled"))}

    def write_limits(self, pl1_w: float, pl2_w: float) -> list[str]:
        """Write PL1, PL2 (µW) and enabled=1. Time windows are never touched."""
        errs = [write_text(self._c(self.c_long, "power_limit_uw"), str(w_to_uw(pl1_w))),
                write_text(self._c(self.c_short, "power_limit_uw"), str(w_to_uw(pl2_w))),
                write_text(os.path.join(self.zone, "enabled"), "1")]
        return [e for e in errs if e]


def apply_and_verify(rapl: Rapl, msr: Any, pl1_w: float, pl2_w: float, hard_max_w: float,
                     firmware: dict[str, Any] | None) -> dict[str, Any]:
    """Clamp, write, then read back sysfs and MSR 0x610. Returns a readback record."""
    pl1_w, pl2_w = clamp_limits(pl1_w, pl2_w, hard_max_w, firmware)
    errs = rapl.write_limits(pl1_w, pl2_w)
    sysfs = rapl.read_limits()
    msr_lim = msr.pkg_limit() if msr is not None else None
    ok, reason = compare_limits(pl1_w, pl2_w, sysfs, msr_lim)
    if errs:
        ok, reason = False, "; ".join(errs)
    return {"ok": ok, "reason": reason, "pl1_w": pl1_w, "pl2_w": pl2_w,
            "sysfs": sysfs, "msr": msr_lim, "write_errors": errs}


def load_or_capture_firmware(path: str, rapl: Rapl, msr: Any, boot_id: str | None = None,
                             force: bool = False) -> tuple[dict[str, Any] | None, str | None]:
    """Firmware PL1/PL2 as captured before TurboMac ever wrote a limit. Returns (limits, warning).

    A saved file that is suspect or non-numeric is treated as missing. A live
    capture with PL1 below HARD_MAX_W (a cap is already active) is returned in
    memory flagged suspect, never saved, and never used as a clamp.
    """
    import json
    import time

    from turbomac.config import HARD_MAX_W
    stale = None
    if not force and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = None
        if valid_firmware(data):
            return data, None
        stale = f"{path} is suspect or invalid and was ignored"
    lim = rapl.read_limits()
    if lim.get("pl1_uw") is None or lim.get("pl2_uw") is None:
        return None, (stale + "; " if stale else "") + "RAPL limits unreadable; firmware limits not captured"
    m = msr.pkg_limit() if msr is not None else None
    data = {k: lim[k] for k in ("pl1_w", "pl2_w", "pl1_uw", "pl2_uw", "pl1_window_us", "pl2_window_us", "enabled")}
    data.update({"msr": m, "boot_id": boot_id, "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    if data["pl1_w"] < HARD_MAX_W:
        data["suspect"] = True
        return data, ((stale + "; " if stale else "") +
                      f"live PL1 {data['pl1_w']:g} W is below {HARD_MAX_W:g} W, so a cap is already active: "
                      f"not saved, clamping to {HARD_MAX_W:g} W only. The next boot captures the true values "
                      "before the cap service writes (or install with --firmware-limits).")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return data, (stale + "; replaced by a fresh capture") if stale else None
