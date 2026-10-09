"""turbomacctl: thin CLI over the turbomacd socket (PLAN 6)."""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

from turbomac.protocol import Client, ProtocolError


def _f(v: Any, fmt: str = "{:.1f}", none: str = "-") -> str:
    return fmt.format(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else none


def _hms(ts: Any) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts)) if isinstance(ts, (int, float)) else "-"


def _dur(sec: Any) -> str:
    if not isinstance(sec, (int, float)):
        return "-"
    sec = int(sec)
    return f"{sec // 3600}h{sec % 3600 // 60:02d}m" if sec >= 3600 else f"{sec // 60}m{sec % 60:02d}s"


def _yn(v: Any) -> str:
    return "?" if v is None else ("YES" if v else "no")


def fan_floor_text(fan: dict[str, Any] | None) -> str:
    """'floor 3500 rpm (power 31 W ≥ 25 W)', or 'floor off' (no floor written, the SMC curve alone)."""
    fan = fan or {}
    t = fan.get("target_rpm")
    if fan.get("mode") == "floor" and isinstance(t, int) and t > 0:
        out = f"floor {t} rpm ({fan.get('reason')})"
    else:
        out = "floor off" + (" (fan.mode auto)" if fan.get("mode") == "auto" else "")
    return out + (f"  write errors {fan['write_errors']}" if fan.get("write_errors") else "")


def render_status(s: dict[str, Any]) -> str:
    if not s:
        return "daemon has no sample yet"
    lat, der, gov = s.get("latest") or {}, s.get("derived") or {}, s.get("governor") or {}
    th, lim, gpu = s.get("thresholds") or {}, s.get("limits") or {}, s.get("gpu") or {}
    cpu, msr, cnt = s.get("cpu") or {}, s.get("msr") or {}, gov.get("counters") or {}
    sysfs, ml, fw = lim.get("sysfs") or {}, lim.get("msr") or {}, lim.get("firmware") or {}
    temps = cpu.get("temps") or {}
    up = s.get("daemon_uptime_s") or 0
    lines = [
        f"TurboMac {s.get('version')} on {s.get('host')}  governor {str(gov.get('mode')).upper()}  "
        f"state {gov.get('state')} for {_dur(gov.get('in_state_s'))}"
        f"  daemon up {_dur(up)}" + ("" if s.get("smc_ok") else "  [SMC UNAVAILABLE]"),
        f"input   {_f(der.get('input_mean_1s'))} W (1 s mean)  latest {_f(lat.get('pdtr_w'))} W  "
        f"{_f(lat.get('v'), '{:.2f}')} V x {_f(lat.get('a'), '{:.2f}')} A  ACPW {_f(der.get('acpw_median_10s'), '{:.2f}')} W  "
        f"headroom {_f(der.get('headroom_w'))} W",
        f"bands   shed {_f(th.get('shed_w'))}  emergency {_f(th.get('emergency_w'))}  recover {_f(th.get('recover_w'))} W"
        f"   ceiling {_f(gov.get('ceiling_w'), '{:g}')} W ~ {_f(der.get('pred_input_at_ceiling_w'))} W input",
        f"cpu     package {_f(lat.get('package_w'))} W  core {_f(lat.get('core_w'))}  uncore {_f(lat.get('uncore_w'))}  "
        f"dram {_f(lat.get('dram_w'))}  non-CPU est {_f(der.get('noncpu_est_w'))} W  busy {_f(cpu.get('busy_pct'), '{:.0f}')} %",
        f"limits  PL1 {_f(gov.get('pl1_w'), '{:g}')} / PL2 {_f(gov.get('pl2_w'), '{:g}')} W commanded  "
        f"sysfs {_f(sysfs.get('pl1_w'), '{:g}')}/{_f(sysfs.get('pl2_w'), '{:g}')}  "
        f"MSR {_f(ml.get('pl1_w'), '{:g}')}/{_f(ml.get('pl2_w'), '{:g}')}{' clamp' if ml.get('pl1_clamp') else ''}  "
        f"firmware {_f(fw.get('pl1_w'), '{:g}')}/{_f(fw.get('pl2_w'), '{:g}')}{' (SUSPECT, ignored)' if fw.get('suspect') else ''}"
        f"  hard max {_f(lim.get('hard_max_w'), '{:g}')}",
        f"flags   power-limited {_yn(msr.get('power_limited'))}  thermal {_yn(msr.get('thermal'))}  "
        f"PROCHOT {_yn(msr.get('prochot'))}",
        f"gpu     {gpu.get('config_mode')} (live {gpu.get('mode')})  {_f(gpu.get('power_w'))} W  cap {_f(gpu.get('cap_w'), '{:g}')} W  "
        f"{_f(gpu.get('temp_c'), '{:.0f}')} C  busy {_f(gpu.get('busy_pct'), '{:.0f}')} %  {_f(gpu.get('sclk_mhz'), '{:d}')} MHz  "
        f"dpm {gpu.get('dpm_level') or '-'}  rt {gpu.get('runtime_status') or '-'}  pci {gpu.get('power_state') or '-'}",
        f"temps   pkg {_f(temps.get('pkg_c'), '{:.0f}')} C  nvme {_f(temps.get('nvme_c'), '{:.0f}')} C  "
        + "  ".join(f"{k} {_f(v, '{:.0f}')}" for k, v in (temps.get("cores") or {}).items()),
        "fans    " + ("  ".join(f"{f.get('label')} {f.get('rpm')} rpm" for f in s.get("fans") or []) or "-")
        + ("  " + fan_floor_text(s["fan"]) if s.get("fan") else ""),
        f"counts  cuts {cnt.get('cuts', 0)}  emergencies {cnt.get('emergencies', 0)}  fallbacks {cnt.get('fallbacks', 0)}  "
        f"recoveries {cnt.get('recoveries', 0)}  writes {cnt.get('writes', 0)}  readback failures {cnt.get('readback_failures', 0)}",
        "peaks   " + "  ".join(f"{k} {v['value']:g} @ {_hms(v['ts'])}" for k, v in (s.get("peaks") or {}).items()),
    ]
    if gov.get("fallback_reason"):
        lines.append(f"FALLBACK {gov['fallback_reason']}")
    if gov.get("last_action"):
        lines.append(f"last    {gov['last_action']}")
    ic = s.get("incident_check") or {}
    lines.append(f"boot    previous boot: {ic.get('verdict', '?')} ({ic.get('reason', '')})"
                 + (f" incident {ic['incident_id']}" if ic.get("incident_id") else ""))
    if gov.get("mode") == "off":
        lines.append("WARNING governor is OFF: telemetry only, no limits are being enforced by the daemon")
    return "\n".join(lines)


def watch_line(s: dict[str, Any]) -> str:
    lat, der, gov = s.get("latest") or {}, s.get("derived") or {}, s.get("governor") or {}
    msr = s.get("msr") or {}
    temps = (s.get("cpu") or {}).get("temps") or {}
    fan = s.get("fan") or {}
    floor = fan.get("target_rpm") if fan.get("mode") == "floor" else None
    return (f"{_hms(s.get('ts'))}  in {_f(lat.get('pdtr_w')):>5} W  1s {_f(der.get('input_mean_1s')):>5}  "
            f"pkg {_f(lat.get('package_w')):>5} W  PL1 {_f(gov.get('pl1_w'), '{:g}'):>3}  {gov.get('state', '?'):<9}  "
            f"gpu {_f((s.get('gpu') or {}).get('power_w')):>4} W  {_f(temps.get('pkg_c'), '{:.0f}'):>3} C"
            + ("  PL!" if msr.get("power_limited") else "") + ("  THERM!" if msr.get("thermal") else "")
            + (f" fan {floor}" if isinstance(floor, int) and floor > 0 else ""))


def parse_assignments(items: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in items:
        key, sep, raw = item.partition("=")
        if not sep:
            raise SystemExit(f"expected key=value, got {item!r}")
        try:
            out[key.strip()] = json.loads(raw)
        except ValueError:
            out[key.strip()] = raw
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="turbomacctl", description="Control and inspect turbomacd")
    ap.add_argument("--socket", help="control socket path")
    ap.add_argument("--json", action="store_true", help="print raw JSON")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("watch")
    c = sub.add_parser("config")
    c.add_argument("assign", nargs="*", help="key=value (JSON values: 38, true, null, \"low\")")
    sub.add_parser("reset")
    g = sub.add_parser("gpu")
    g.add_argument("mode", choices=["auto", "low", "off"])
    g.add_argument("--yes", action="store_true", help="skip the OFF confirmation")
    i = sub.add_parser("incidents")
    i.add_argument("id", nargs="?")
    e = sub.add_parser("events")
    e.add_argument("n", nargs="?", type=int, default=50)
    sub.add_parser("smc-keys")
    a = ap.parse_args(argv)
    cl = Client(a.socket)
    out = lambda obj: print(json.dumps(obj, indent=2, default=str))  # noqa: E731
    try:
        if a.cmd == "status":
            s = cl.call("status")
            out(s) if a.json else print(render_status(s))
        elif a.cmd == "watch":
            for s in cl.stream("subscribe", timeout=None):
                print(json.dumps(s, default=str) if a.json else watch_line(s), flush=True)
        elif a.cmd == "config":
            if not a.assign:
                r = cl.call("config.get")
                if a.json:
                    out(r)
                else:
                    from turbomac.config import flatten
                    print("\n".join(f"{k} = {json.dumps(v)}" for k, v in flatten(r["config"]).items()))
            else:
                r = cl.call("config.set", timeout=60, **parse_assignments(a.assign))
                if a.json:
                    out(r)
                else:
                    for k, v in r["accepted"].items():
                        print(f"accepted {k} = {json.dumps(v)}")
                    for k, v in r["rejected"].items():
                        print(f"REJECTED {k}: {v}")
                    if not r.get("persisted", True):
                        print(f"WARNING applied live but not saved: {r.get('error')}")
                    return 1 if r["rejected"] else 0
        elif a.cmd == "reset":
            r = cl.call("config.reset", timeout=60)
            out(r) if a.json else print(f"reset to defaults ({len(r['accepted'])} keys), digest {r['digest']}")
        elif a.cmd == "gpu":
            if a.mode == "off" and not a.yes:
                print("Powering off the dGPU may leave the internal panel black until reboot.")
                if input("type OFF to confirm: ").strip() != "OFF":
                    print("aborted")
                    return 1
            r = cl.call("gpu.set", timeout=90, mode=a.mode)
            if a.json:
                out(r)
            else:
                print(f"gpu {a.mode}: {'OK' if r['ok'] else 'FAILED'} -> mode {r['mode']}: {r['reason']}")
                print("\n".join("  " + st for st in r.get("steps", [])))
            return 0 if r["ok"] else 1
        elif a.cmd == "incidents":
            if a.id:
                r = cl.call("incident", id=a.id)
                if a.json:
                    out(r)
                else:
                    print(f"incident {r['id']}  boot {r.get('boot_id')}  last sample {r.get('last_sample_time')}")
                    print(f"reason: {r.get('reason')}")
                    for row in (r.get("samples") or [])[-15:]:
                        print(f"  {row.get('time')}  in {_f(row.get('input_w'))} W  pkg {_f(row.get('pkg_w'))} W  "
                              f"gpu {_f(row.get('gpu_w'))} W  PL1 {row.get('pl1_w')}  {row.get('state')}")
                    print("journal tail:\n" + "\n".join("  " + ln for ln in r.get("journal_tail") or []))
            else:
                r = cl.call("incidents")
                if a.json:
                    out(r)
                else:
                    print("\n".join(f"{x['id']}  boot {x.get('boot_id')}  last sample {x.get('last_sample_time')}  "
                                    f"{x.get('n_samples')} samples" for x in r) or "no incidents recorded")
        elif a.cmd == "events":
            r = cl.call("events", n=a.n)
            if a.json:
                out(r)
            else:
                for ev in r:
                    rest = " ".join(f"{k}={json.dumps(v, default=str, ensure_ascii=False)}" for k, v in ev.items() if k not in ("ts", "type"))
                    print(f"{_hms(ev.get('ts'))} {ev.get('type'):<16} {rest}")
        elif a.cmd == "smc-keys":
            r = cl.call("smc.keys")
            out(r) if a.json else print("\n".join(f"{k['name']:<5} {k['type']:<5} {k['len']:>2}  #{k['index']}"
                                                  for k in r) or "no SMC keys (applesmc unavailable)")
    except ProtocolError as exc:
        print(f"turbomacctl: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
