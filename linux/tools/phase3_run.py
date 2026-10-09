#!/usr/bin/env python3
"""Phase 3 supervised run: launch one workload while watching turbomacd's status
stream, print one line per second, kill the workload on guard limits, and write
a JSON summary. Reads nothing from sysfs itself (the daemon owns the SMC
selector); everything comes from the control socket.

  phase3_run.py --label cap40 --stress 8 --seconds 30 --json cap40.json
  phase3_run.py --label whisper --seconds 600 --json w.json -- sudo -u parham whisper-cli -t 8 ...
"""
from __future__ import annotations

import argparse
import json
import signal
import subprocess
import sys
import time

sys.path.insert(0, "/opt/turbomac/src")
from turbomac.protocol import Client  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--seconds", type=float, default=30, help="stress-ng timeout, or max run time for --cmd")
    ap.add_argument("--stress", type=int, default=0, help="stress-ng matrixprod workers")
    ap.add_argument("--abort-input-w", type=float, default=72.0)
    ap.add_argument("--abort-temp-c", type=float, default=97.0)
    ap.add_argument("--settle", type=float, default=6.0, help="seconds to keep watching after the workload ends")
    ap.add_argument("--json", default=None)
    ap.add_argument("cmd", nargs="*", help="workload command after --")
    a = ap.parse_args()

    cmd: list[str] | None = None
    if a.stress:
        cmd = ["stress-ng", "--cpu", str(a.stress), "--cpu-method", "matrixprod",
               "--timeout", f"{int(a.seconds)}s", "--quiet"]
    elif a.cmd:
        cmd = [c for c in a.cmd if c != "--"]

    cl = Client()
    st = cl.call("status")
    gov, der = st["governor"], st["derived"]
    print(f"[{a.label}] governor {gov['mode']} state {gov['state']} PL1 {gov['pl1_w']} W ceiling {gov['ceiling_w']} "
          f"static {gov['static_cap_w']} floor {gov['floor_w']}  ACPW {der['acpw_median_10s']}  "
          f"input {der['input_mean_1s']} W  pkg_c {((st.get('cpu') or {}).get('temps') or {}).get('pkg_c')}", flush=True)
    counters0 = dict(gov["counters"])

    proc = subprocess.Popen(cmd) if cmd else None
    if cmd:
        print("workload:", " ".join(cmd), flush=True)
    t0 = time.monotonic()
    rows: list[dict] = []
    states: list[tuple[float, str]] = []
    aborted = None
    ended_at = None
    last_print = -1.0
    for s in cl.stream("subscribe", timeout=None):
        if not s:
            continue
        t = time.monotonic() - t0
        lat, der, gov = s.get("latest") or {}, s.get("derived") or {}, s.get("governor") or {}
        cpu = s.get("cpu") or {}
        temps = cpu.get("temps") or {}
        gpu = s.get("gpu") or {}
        c = gov.get("counters") or {}
        row = {"t": round(t, 2), "input_w": lat.get("pdtr_w"), "input_1s": der.get("input_mean_1s"),
               "pkg_w": lat.get("package_w"), "gpu_w": gpu.get("power_w"), "pkg_c": temps.get("pkg_c"),
               "pl1_w": gov.get("pl1_w"), "state": gov.get("state"),
               "cuts": c.get("cuts"), "emerg": c.get("emergencies"), "fb": c.get("fallbacks"), "recov": c.get("recoveries"),
               "fans": [f.get("rpm") for f in s.get("fans") or []],
               "power_limited": (s.get("msr") or {}).get("power_limited"),
               "mhz": cpu.get("mhz"), "busy": cpu.get("busy_pct"), "v": lat.get("v"), "a": lat.get("a")}
        rows.append(row)
        if not states or states[-1][1] != row["state"]:
            states.append((round(t, 1), str(row["state"])))
        if t - last_print >= 1.0:
            last_print = t
            print(f"{t:6.1f}s  in {row['input_w'] or 0:5.1f} W (1s {row['input_1s'] or 0:5.1f})  pkg {row['pkg_w'] or 0:5.1f} W  "
                  f"gpu {row['gpu_w'] or 0:4.1f}  {row['pkg_c'] or 0:3.0f} C  PL1 {str(row['pl1_w']):>5}  {str(row['state']):<9}  "
                  f"cuts {row['cuts']} em {row['emerg']} fb {row['fb']}  fans {row['fans']}  busy {row['busy']}"
                  + ("  PL!" if row["power_limited"] else ""), flush=True)
        inp, tc = row["input_w"] or 0.0, row["pkg_c"] or 0.0
        if aborted is None and (inp >= a.abort_input_w or tc >= a.abort_temp_c):
            aborted = f"guard tripped at t={t:.1f}s: input {inp:.1f} W, pkg {tc:.0f} C"
            print("ABORT:", aborted, flush=True)
            if proc is not None and proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
            ended_at = t
        if proc is not None and ended_at is None and proc.poll() is not None:
            ended_at = t
            print(f"workload finished (rc={proc.returncode}) at {t:.1f}s; settling {a.settle:g}s", flush=True)
        if proc is None and ended_at is None and t >= a.seconds:
            ended_at = t
        if proc is not None and ended_at is None and t >= a.seconds + 120:
            print("workload overran; terminating", flush=True)
            proc.send_signal(signal.SIGTERM)
            ended_at = t
        if ended_at is not None and t - ended_at >= a.settle:
            break
    if proc is not None:
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()

    loaded = [r for r in rows if 2.0 <= r["t"] <= (ended_at if ended_at is not None else 1e9)]

    def mx(k):
        v = [r[k] for r in rows if isinstance(r.get(k), (int, float))]
        return max(v) if v else None

    def mean_loaded(k):
        v = [r[k] for r in loaded if isinstance(r.get(k), (int, float))]
        return round(sum(v) / len(v), 2) if v else None

    fin = cl.call("status")["governor"]["counters"]
    summary = {
        "label": a.label, "cmd": cmd, "seconds": a.seconds, "samples": len(rows), "aborted": aborted,
        "ended_at_s": ended_at, "states": states,
        "peak": {k: mx(k) for k in ("input_w", "input_1s", "pkg_w", "gpu_w", "pkg_c")},
        "loaded_mean": {k: mean_loaded(k) for k in ("input_w", "pkg_w", "gpu_w")},
        "fans_max": max((max(r["fans"]) for r in rows if r["fans"]), default=None),
        "pl1_min": min((r["pl1_w"] for r in rows if isinstance(r.get("pl1_w"), (int, float))), default=None),
        "counters_delta": {k: (fin.get(k, 0) - counters0.get(k, 0)) for k in fin},
    }
    print(json.dumps(summary, indent=1), flush=True)
    if a.json:
        with open(a.json, "w") as fh:
            json.dump({"summary": summary, "rows": rows}, fh)
    return 3 if aborted else 0


if __name__ == "__main__":
    sys.exit(main())
