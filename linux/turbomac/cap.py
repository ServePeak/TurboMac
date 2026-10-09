"""Early-boot static cap (turbomac-cap.service, ExecStartPre/ExecStopPost).

python3 -m turbomac.cap apply | show | mark-clean | capture [--force] | init-config

Minimal imports, no daemon state: applies PL1 = PL2 = governor.static_cap_w and
the GPU mode/cap from the config, verifies readback, prints what it did.
Exit status of apply reflects only the CPU cap: 0 verified, 3 written but readback
mismatch, 4 sysfs write error, 2 RAPL unavailable. turbomac-cap.sh falls back to a
plain echo of the static cap on anything except 0 and 3.
"""
from __future__ import annotations

import json
import os
import sys

from turbomac import config
from turbomac.config import HARD_MAX_W
from turbomac.daemon.msr import Msr
from turbomac.daemon.paths import Paths
from turbomac.daemon.rapl import Rapl, apply_and_verify, load_or_capture_firmware
from turbomac.daemon.sysfs import read_text


def _say(msg: str) -> None:
    print(f"turbomac-cap: {msg}", flush=True)


def apply(paths: Paths) -> int:
    cfg, problems = config.load(paths.config)
    for k, v in problems.items():
        _say(f"config {k}: {v} (default used)")
    rapl, msr = Rapl(paths.powercap), Msr(paths.msr_dev)
    if not rapl.available():
        _say(f"RAPL powercap not found at {paths.powercap}")
        return 2
    fw, warn = load_or_capture_firmware(paths.firmware_limits, rapl, msr, read_text(paths.boot_id, quiet=True))
    if warn:
        _say("WARNING " + warn)
    static = float(cfg["governor"]["static_cap_w"])
    rb = apply_and_verify(rapl, msr, static, static, HARD_MAX_W, fw)
    status = "WRITE FAILED" if rb["write_errors"] else ("OK" if rb["ok"] else "MISMATCH")
    _say(f"PL1 {rb['pl1_w']:g} W, PL2 {rb['pl2_w']:g} W (static cap {static:g}, firmware "
         f"{(fw or {}).get('pl1_w')}/{(fw or {}).get('pl2_w')} W): {status} - {rb['reason']}")
    try:
        from turbomac.daemon.gpu import Gpu
        g = cfg["gpu"]
        res = Gpu(paths).apply_mode(g["mode"], g["cap_w"], wait_s=0.0)
        _say(f"GPU mode {g['mode']} -> {res['mode']}: {'OK' if res['ok'] else 'FAILED'} - {res['reason']}")
    except Exception as exc:  # noqa: BLE001 - GPU trouble must never fail the CPU cap
        _say(f"GPU step skipped: {exc!r}")
    if rb["write_errors"]:
        return 4
    return 0 if rb["ok"] else 3


def show(paths: Paths) -> int:
    rapl, msr = Rapl(paths.powercap), Msr(paths.msr_dev)
    out = {"sysfs": rapl.read_limits() if rapl.available() else None, "msr_0x610": msr.pkg_limit(),
           "msr_0x1b1": msr.therm_status(), "msr_units": msr.units()}
    try:
        with open(paths.firmware_limits, "r", encoding="utf-8") as fh:
            out["firmware"] = json.load(fh)
    except (OSError, ValueError):
        out["firmware"] = None
    from turbomac.daemon.gpu import Gpu
    gpu = Gpu(paths)
    out["gpu"] = {"switcheroo": gpu.switcheroo(), "device": gpu.dev}
    if gpu.mode != "off" and gpu.hwmon:
        out["gpu"].update({"power1_cap": read_text(os.path.join(gpu.hwmon, "power1_cap"), quiet=True),
                           "dpm": read_text(os.path.join(gpu.dev or "", "power_dpm_force_performance_level"),
                                            quiet=True)})
    print(json.dumps(out, indent=2, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    cmd = args[0] if args else "apply"
    paths = Paths.from_env()
    if cmd == "apply":
        return apply(paths)
    if cmd == "show":
        return show(paths)
    if cmd == "mark-clean":
        from turbomac.daemon.incidents import mark_clean
        mark_clean(paths)
        _say(f"clean-shutdown marker written to {paths.clean_marker}")
        return 0
    if cmd == "capture":
        rapl, msr = Rapl(paths.powercap), Msr(paths.msr_dev)
        fw, warn = load_or_capture_firmware(paths.firmware_limits, rapl, msr,
                                            read_text(paths.boot_id, quiet=True), force="--force" in args)
        _say(f"firmware limits: {json.dumps(fw)}" + (f"\nWARNING {warn}" if warn else ""))
        return 0 if fw else 2
    if cmd == "init-config":
        if os.path.exists(paths.config):
            _say(f"{paths.config} exists, left unchanged")
        else:
            config.save(paths.config, config.defaults())
            _say(f"wrote defaults to {paths.config}")
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
