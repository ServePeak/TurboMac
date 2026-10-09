"""amdgpu (Radeon Pro 560) telemetry and power mode control (PLAN 4.4).

While the mode is "off" only the vga_switcheroo file and the PCI power_state
attribute are read (neither wakes the device); every other amdgpu read is
skipped, since reading hwmon on a powered-down dGPU can wake it.
"""
from __future__ import annotations

import glob
import os
import threading
import time
from typing import Any, Callable

from turbomac.daemon.paths import Paths
from turbomac.daemon.sysfs import read_int, read_text, write_text

LOW_CAP_W = 10.0


def parse_switcheroo(text: str | None) -> dict[str, dict[str, Any]] | None:
    """'2:DIS:+:Pwr:0000:01:00.0' lines -> {"DIS": {"id": 2, "active": True, "power": "Pwr", "pci": ...}}."""
    if text is None:
        return None
    out: dict[str, dict[str, Any]] = {}
    for line in text.splitlines():
        parts = line.strip().split(":", 4)
        if len(parts) < 4:
            continue
        out[parts[1]] = {"id": int(parts[0]) if parts[0].isdigit() else parts[0],
                         "active": parts[2] == "+", "power": parts[3],
                         "pci": parts[4] if len(parts) > 4 else None}
    return out


def parse_sclk(text: str | None) -> int | None:
    """Current level of pp_dpm_sclk ('1: 300Mhz *') in MHz."""
    for line in (text or "").splitlines():
        if line.strip().endswith("*"):
            digits = "".join(ch for ch in line.split(":", 1)[-1] if ch.isdigit())
            return int(digits) if digits else None
    return None


class Gpu:
    def __init__(self, paths: Paths) -> None:
        self.paths = paths
        self.dev: str | None = None
        self.hwmon: str | None = None
        self.pci: str | None = None
        self.mode: str | None = None   # last applied mode
        self.lock = threading.Lock()   # held while a mode change runs
        self.find()
        if self._dis_off():
            self.mode = "off"  # already powered down (early boot): never touch amdgpu files

    def find(self) -> bool:
        for card in sorted(glob.glob(os.path.join(self.paths.drm, "card*", "device"))):
            if read_text(os.path.join(card, "vendor"), quiet=True) == "0x1002":
                self.dev = card
                hw = sorted(glob.glob(os.path.join(card, "hwmon", "hwmon*")))
                self.hwmon = hw[0] if hw else None
                self.pci = os.path.basename(os.path.realpath(card))
                return True
        return False

    @property
    def busy(self) -> bool:
        return self.lock.locked()

    def switcheroo(self) -> dict[str, dict[str, Any]] | None:
        return parse_switcheroo(read_text(self.paths.switcheroo, quiet=True))

    def _d(self, name: str) -> str:
        return os.path.join(self.dev or "/nonexistent-amdgpu", name)

    def _h(self, name: str) -> str:
        return os.path.join(self.hwmon or "/nonexistent-amdgpu-hwmon", name)

    def power_state(self) -> str | None:
        """PCI power state (D0, D3hot, D3cold); safe to read while the dGPU is off."""
        return read_text(self._d("power_state"), quiet=True) if self.dev else None

    def read(self) -> dict[str, Any]:
        """1 Hz telemetry. Only switcheroo + power_state while off or mid-change."""
        out: dict[str, Any] = {"mode": self.mode, "present": self.dev is not None,
                               "power_state": self.power_state()}
        sw = self.switcheroo()
        if sw is not None:
            out["switcheroo"] = {k: f"{'+' if v['active'] else ' '}:{v['power']}" for k, v in sw.items()}
        if self.mode == "off" or self.busy or not self.dev:
            return out
        uw = lambda name: (lambda v: v / 1e6 if v is not None else None)(read_int(self._h(name), quiet=True))  # noqa: E731
        temp = read_int(self._h("temp1_input"), quiet=True)
        out.update({
            "power_w": uw("power1_input") if os.path.exists(self._h("power1_input")) else uw("power1_average"),
            "cap_w": uw("power1_cap"), "cap_min_w": uw("power1_cap_min"), "cap_max_w": uw("power1_cap_max"),
            "temp_c": temp / 1000.0 if temp is not None else None,
            "busy_pct": read_int(self._d("gpu_busy_percent"), quiet=True),
            "sclk_mhz": parse_sclk(read_text(self._d("pp_dpm_sclk"), quiet=True)),
            "dpm_level": read_text(self._d("power_dpm_force_performance_level"), quiet=True),
            "runtime_status": read_text(self._d("power/runtime_status"), quiet=True),
        })
        return out

    # control -----------------------------------------------------------------
    def _fbcon_bind(self, value: str) -> str | None:
        for vt in sorted(glob.glob(os.path.join(self.paths.vtconsole, "vtcon*"))):
            if "frame buffer" in (read_text(os.path.join(vt, "name"), "", quiet=True) or ""):
                return write_text(os.path.join(vt, "bind"), value)
        return "no frame buffer vtconsole found"

    def _dis_off(self) -> bool:
        sw = self.switcheroo() or {}
        return sw.get("DIS", {}).get("power") == "Off"

    def _set_cap_dpm(self, cap_w: float, level: str, steps: list[str]) -> str | None:
        lo = (read_int(self._h("power1_cap_min"), quiet=True) or 0) / 1e6
        hi = (read_int(self._h("power1_cap_max"), quiet=True) or 35_000_000) / 1e6
        cap = min(max(cap_w, lo), hi)
        for path, val in ((self._h("power1_cap"), str(int(round(cap * 1e6)))),
                          (self._d("power_dpm_force_performance_level"), level)):
            err = write_text(path, val)
            steps.append(f"write {val} -> {os.path.basename(path)}" + (f" FAILED: {err}" if err else ""))
            if err:
                return err
        got = read_int(self._h("power1_cap"), quiet=True)
        if got is None or abs(got - cap * 1e6) >= 1e6:
            return f"power1_cap reads {got}, wanted {cap:g} W"
        lvl = read_text(self._d("power_dpm_force_performance_level"), quiet=True)
        if lvl != level:
            return f"dpm level reads {lvl}, wanted {level}"
        return None

    def _power_on(self, steps: list[str]) -> None:
        for cmd in ("ON", "DIS"):
            err = write_text(self.paths.switcheroo, cmd)
            steps.append(f"switcheroo {cmd}" + (f" FAILED: {err}" if err else ""))
        err = self._fbcon_bind("1")
        steps.append("fbcon bind" + (f" FAILED: {err}" if err else ""))

    def apply_mode(self, mode: str, cap_w: float, wait_s: float = 10.0,
                   sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
        """Apply auto/low/off. A failed off reverts to low. Returns the outcome."""
        steps: list[str] = []
        with self.lock:
            if mode == "off":
                done: list[str] = []
                res = self._apply_off(wait_s, sleep, steps, done)
                if res is None:
                    self.mode = "off"
                    return {"ok": True, "mode": "off", "reason": "dGPU off (switcheroo DIS Off)", "steps": steps}
                steps.append(f"off failed: {res}; reverting to low")
                if "switcheroo" in done:
                    self._power_on(steps)
                elif "fbcon" in done:
                    err = self._fbcon_bind("1")
                    steps.append("fbcon bind" + (f" FAILED: {err}" if err else ""))
                err = self._apply_on("low", cap_w, steps)
                self.mode = "low"
                return {"ok": False, "mode": "low", "reason": res + (f"; revert: {err}" if err else ""),
                        "steps": steps}
            err = self._apply_on(mode, cap_w, steps)
            self.mode = mode
            return {"ok": err is None, "mode": mode, "reason": err or f"{mode} applied", "steps": steps}

    def _apply_on(self, mode: str, cap_w: float, steps: list[str]) -> str | None:
        if self._dis_off():
            self._power_on(steps)
        if (not self.dev or not self.hwmon) and not self.find():
            return "amdgpu device not found"
        if not self.hwmon:
            return "amdgpu hwmon not found"
        if mode == "low":
            return self._set_cap_dpm(LOW_CAP_W, "low", steps)
        return self._set_cap_dpm(cap_w, "auto", steps)

    def _apply_off(self, wait_s: float, sleep: Callable[[float], None], steps: list[str],
                   done: list[str]) -> str | None:
        sw = self.switcheroo()
        if sw is None or "DIS" not in sw or "IGD" not in sw:
            return "vga_switcheroo not available (is debugfs mounted?)"
        if self._dis_off():
            steps.append("already off")
            return None
        err = self._fbcon_bind("0")
        steps.append("fbcon unbind" + (f" FAILED: {err}" if err else ""))
        if err:
            return f"fbcon unbind: {err}"
        done.append("fbcon")
        for cmd in ("IGD", "OFF"):
            done.append("switcheroo")
            err = write_text(self.paths.switcheroo, cmd)
            steps.append(f"switcheroo {cmd}" + (f" FAILED: {err}" if err else ""))
            if err:
                return f"switcheroo {cmd}: {err}"
        # No runtime PM on this dGPU: runtime_status can stay "active" after a real
        # power-off, so success is switcheroo DIS Off plus PCI power_state != D0.
        for phase in ("immediate", f"after {wait_s:g} s"):
            if phase != "immediate":
                sleep(wait_s)
            if not self._dis_off():
                return f"switcheroo does not report DIS Off ({phase})"
            ps = self.power_state()
            if ps == "D0":
                return f"PCI power_state still D0 ({phase})"
            steps.append(f"verified {phase}: DIS Off, power_state {ps or 'n/a'}")
        return None
