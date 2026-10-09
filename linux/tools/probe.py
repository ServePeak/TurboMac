#!/usr/bin/env python3
"""Phase 0 probe for old-mac: read input power, RAPL, temps, fans, GPU at a fixed
rate, optionally while running a bounded stress-ng load with abort guards.

Stdlib only. Must run as root. Reads SMC keys through applesmc's key_at_index
selector and restores the selector on exit. Nothing here writes a power limit.

Usage:
  probe.py --seconds 10
  probe.py --seconds 25 --stress 8 --abort-input-w 72 --abort-temp-c 97 --json out.json
  probe.py --msr        # decode MSR 0x606 / 0x610 / 0x1B1 and exit
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import signal
import struct
import subprocess
import sys
import time

SMC_DIR = glob.glob("/sys/devices/platform/applesmc.*")
SMC_DIR = SMC_DIR[0] if SMC_DIR else None
RAPL = "/sys/class/powercap/intel-rapl:0"
MSR_DEV = "/dev/cpu/0/msr"


def read_text(path: str, default: str | None = None) -> str | None:
    try:
        with open(path, "r", encoding="ascii", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return default


def read_msr(reg: int) -> int:
    fd = os.open(MSR_DEV, os.O_RDONLY)
    try:
        data = os.pread(fd, 8, reg)
    finally:
        os.close(fd)
    return struct.unpack("<Q", data)[0]


def decode_msrs() -> dict:
    units = read_msr(0x606)
    pu = 1.0 / (1 << (units & 0xF))
    tu = 1.0 / (1 << ((units >> 16) & 0xF))
    pl = read_msr(0x610)

    def window(field: int) -> float:
        y = field & 0x1F
        z = (field >> 5) & 0x3
        return (1 << y) * (1.0 + z / 4.0) * tu

    out = {
        "msr_0x606": hex(units),
        "power_unit_w": pu,
        "time_unit_s": tu,
        "msr_0x610": hex(pl),
        "pl1_w": (pl & 0x7FFF) * pu,
        "pl1_enable": bool(pl >> 15 & 1),
        "pl1_clamp": bool(pl >> 16 & 1),
        "pl1_window_s": window((pl >> 17) & 0x7F),
        "pl2_w": ((pl >> 32) & 0x7FFF) * pu,
        "pl2_enable": bool(pl >> 47 & 1),
        "pl2_clamp": bool(pl >> 48 & 1),
        "pl2_window_s": window((pl >> 49) & 0x7F),
        "lock": bool(pl >> 63 & 1),
    }
    st = read_msr(0x1B1)
    out["msr_0x1b1"] = hex(st)
    out["pkg_thermal_status"] = bool(st & 1)
    out["pkg_prochot"] = bool(st >> 2 & 1)
    out["pkg_power_limited"] = bool(st >> 10 & 1)
    out["pkg_power_limit_log"] = bool(st >> 11 & 1)
    return out


class SMC:
    """Reads applesmc keys by name through the key_at_index selector."""

    def __init__(self) -> None:
        if SMC_DIR is None:
            raise RuntimeError("applesmc platform device not found")
        self.sel = os.path.join(SMC_DIR, "key_at_index")
        self.original = read_text(self.sel, "0")
        self.count = int(read_text(os.path.join(SMC_DIR, "key_count"), "0"))
        self.table: dict[str, tuple[int, str, int]] = {}
        for i in range(self.count):
            self._select(i)
            name = read_text(os.path.join(SMC_DIR, "key_at_index_name"), "")
            typ = read_text(os.path.join(SMC_DIR, "key_at_index_type"), "")
            ln = int(read_text(os.path.join(SMC_DIR, "key_at_index_data_length"), "0"))
            if name:
                self.table[name] = (i, typ, ln)

    def _select(self, index: int) -> None:
        with open(self.sel, "w") as fh:
            fh.write(str(index))

    def restore(self) -> None:
        try:
            with open(self.sel, "w") as fh:
                fh.write(self.original or "0")
        except OSError:
            pass

    def raw(self, key: str) -> tuple[str, bytes]:
        index, typ, ln = self.table[key]
        self._select(index)
        name = read_text(os.path.join(SMC_DIR, "key_at_index_name"), "")
        if name != key:
            raise RuntimeError(f"selector mismatch: wanted {key} got {name}")
        with open(os.path.join(SMC_DIR, "key_at_index_data"), "rb") as fh:
            data = fh.read()
        return typ, data[:ln] if ln else data

    def read(self, key: str):
        typ, data = self.raw(key)
        t = typ.strip()
        if t == "flt" and len(data) >= 4:
            return struct.unpack("<f", data[:4])[0]
        if t in ("ui8", "ui16", "ui32", "ui64"):
            return int.from_bytes(data, "big", signed=False)
        if t in ("si8", "si16", "si32"):
            return int.from_bytes(data, "big", signed=True)
        if t == "sp78" and len(data) >= 2:
            return int.from_bytes(data[:2], "big", signed=True) / 256.0
        if t == "fpe2" and len(data) >= 2:
            return int.from_bytes(data[:2], "big", signed=False) / 4.0
        if len(t) == 4 and t[:2] in ("fp", "sp") and len(data) >= 2:
            frac = int(t[3], 16)
            signed = t[:2] == "sp"
            return int.from_bytes(data[:2], "big", signed=signed) / float(1 << frac)
        if t == "flag":
            return bool(data and data[0])
        return data.hex()


class Energy:
    def __init__(self, zone: str) -> None:
        self.path = os.path.join(zone, "energy_uj")
        self.max = int(read_text(os.path.join(zone, "max_energy_range_uj"), "0") or 0)
        self.last: tuple[float, int] | None = None

    def watts(self) -> float | None:
        now = time.monotonic()
        val = read_text(self.path)
        if val is None:
            return None
        cur = int(val)
        if self.last is None:
            self.last = (now, cur)
            return None
        t0, e0 = self.last
        self.last = (now, cur)
        delta = cur - e0
        if delta < 0 and self.max:
            delta += self.max
        dt = now - t0
        return delta / 1e6 / dt if dt > 0 else None


def find_hwmon(name: str) -> str | None:
    for d in glob.glob("/sys/class/hwmon/hwmon*"):
        if read_text(os.path.join(d, "name")) == name:
            return d
    return None


def amd_hwmon() -> str | None:
    for card in glob.glob("/sys/class/drm/card*/device"):
        if read_text(os.path.join(card, "vendor")) == "0x1002":
            hw = glob.glob(os.path.join(card, "hwmon", "hwmon*"))
            return hw[0] if hw else None
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=10)
    ap.add_argument("--hz", type=float, default=2)
    ap.add_argument("--stress", type=int, default=0, help="stress-ng matrixprod workers")
    ap.add_argument("--abort-input-w", type=float, default=72.0)
    ap.add_argument("--abort-temp-c", type=float, default=97.0)
    ap.add_argument("--json", default=None)
    ap.add_argument("--msr", action="store_true")
    ap.add_argument("--keys", action="store_true", help="dump the SMC key table and exit")
    args = ap.parse_args()

    if args.msr:
        print(json.dumps(decode_msrs(), indent=2))
        return 0

    smc = SMC()
    try:
        if args.keys:
            print(json.dumps({k: {"index": v[0], "type": v[1], "len": v[2]} for k, v in smc.table.items()}, indent=1))
            return 0
        for k in ("PDTR", "ACPW", "VD0R", "ID0R"):
            if k not in smc.table:
                print(f"missing SMC key {k}", file=sys.stderr)
                return 2
        pkg, core, unc, dram = (Energy(RAPL), Energy(RAPL + ":0"), Energy(RAPL + ":1"), Energy(RAPL + ":2"))
        coretemp = find_hwmon("coretemp")
        gpu = amd_hwmon()
        extra = [k for k in ("PSTR", "PCPC", "PCPG", "PCPT", "PG0R", "PHPC", "PDTR") if k in smc.table]

        proc = None
        if args.stress:
            cmd = ["stress-ng", "--cpu", str(args.stress), "--cpu-method", "matrixprod", "--timeout", f"{int(args.seconds)}s", "--quiet"]
            proc = subprocess.Popen(cmd)
            print("started:", " ".join(cmd))

        peaks: dict[str, float] = {}
        rows = []
        aborted = None
        acpw = smc.read("ACPW") / 1000.0
        print(f"ACPW {acpw:.3f} W   keys: {', '.join(extra)}")
        print(f"{'t':>6} {'in_W':>6} {'V':>6} {'A':>6} {'pkg_W':>6} {'core':>6} {'gpu_W':>6} {'tC':>4} {'fanL':>5} {'fanR':>5}")
        t_start = time.monotonic()
        period = 1.0 / args.hz
        next_t = t_start
        for z in (pkg, core, unc, dram):
            z.watts()
        while True:
            now = time.monotonic()
            if now < next_t:
                time.sleep(next_t - now)
            next_t += period
            t = time.monotonic() - t_start
            row = {
                "t": round(t, 2),
                "input_w": smc.read("PDTR"),
                "v": smc.read("VD0R"),
                "a": smc.read("ID0R"),
                "acpw_w": smc.read("ACPW") / 1000.0,
                "pkg_w": pkg.watts(),
                "core_w": core.watts(),
                "uncore_w": unc.watts(),
                "dram_w": dram.watts(),
                "temp_c": (int(read_text(os.path.join(coretemp, "temp1_input"), "0")) / 1000.0) if coretemp else None,
                "gpu_w": (int(read_text(os.path.join(gpu, "power1_input"), "0")) / 1e6) if gpu else None,
                "fan_l": int(read_text(os.path.join(SMC_DIR, "fan1_input"), "0")),
                "fan_r": int(read_text(os.path.join(SMC_DIR, "fan2_input"), "0")),
            }
            for k in extra:
                if k != "PDTR":
                    try:
                        row[k] = smc.read(k)
                    except Exception:
                        row[k] = None
            rows.append(row)
            for k in ("input_w", "pkg_w", "temp_c", "gpu_w", "a"):
                v = row.get(k)
                if v is not None:
                    peaks[k] = max(peaks.get(k, -1e9), v)
            print(f"{t:6.1f} {row['input_w']:6.2f} {row['v']:6.2f} {row['a']:6.3f} "
                  f"{(row['pkg_w'] or 0):6.2f} {(row['core_w'] or 0):6.2f} {(row['gpu_w'] or 0):6.2f} "
                  f"{(row['temp_c'] or 0):4.0f} {row['fan_l']:5d} {row['fan_r']:5d}", flush=True)
            if row["input_w"] >= args.abort_input_w or (row["temp_c"] or 0) >= args.abort_temp_c:
                aborted = f"abort guard: input {row['input_w']:.1f} W temp {row['temp_c']} C"
                print(aborted, flush=True)
                if proc:
                    proc.send_signal(signal.SIGTERM)
                break
            if proc is not None and proc.poll() is not None:
                print("stress-ng finished", flush=True)
                break
            if t >= args.seconds and proc is None:
                break
        if proc:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        means = {}
        for k in ("input_w", "pkg_w", "gpu_w"):
            vals = [r[k] for r in rows if r.get(k) is not None]
            means[k] = sum(vals) / len(vals) if vals else None
        summary = {"acpw_w": acpw, "samples": len(rows), "peaks": peaks, "means": means,
                   "aborted": aborted, "stress_workers": args.stress, "seconds": args.seconds}
        print(json.dumps(summary, indent=2))
        if args.json:
            with open(args.json, "w") as fh:
                json.dump({"summary": summary, "rows": rows}, fh)
        return 0
    finally:
        smc.restore()


if __name__ == "__main__":
    sys.exit(main())
