# Phase 1 receipt — installed 2026-10-09 21:10 UTC, redeployed 21:18 UTC

`turbomacd` 0.1.0 is installed on old-mac and enabled at boot. Governor mode
`static`: PL1 = PL2 = 35 W, GPU `auto` with a 15 W cap, thermald masked.

## What is on the host

| Item | Where |
|---|---|
| Code | `/opt/turbomac/src/turbomac` (wrappers in `/opt/turbomac/bin`, symlinks `turbomacd`, `turbomacctl` in `/usr/local/bin`) |
| Staging copy of this tree (deploy/rollback source) | `/root/turbomac-src/` |
| Config | `/etc/turbomac/config.json` (defaults from PLAN §4.7, `logging.max_mib` 1024) |
| State | `/var/lib/turbomac/` — `samples/` (1 Hz JSONL, hourly files, gzipped when finished), `incidents/`, `smc-keys.json`, `firmware-limits.json`, `last-boot-checked` |
| Firmware record | seeded from the Phase 0 baseline: PL1 100 W / PL2 125 W (the capture that would otherwise have recorded the Phase 0 35 W cap is now never saved) |
| Units | `turbomac-cap.service` (oneshot, before docker and user sessions), `turbomacd.service` (notify, `Restart=always`, `WatchdogSec=10`, cap re-applied by `ExecStartPre` and `ExecStopPost`) |
| Socket | `/run/turbomac/turbomacd.sock`, 0660 root:turbomac; user `parham` is in the group (re-login needed) |
| Modules | `/etc/modules-load.d/turbomac.conf`: `msr`, `intel_rapl_msr` |

Install command used (idempotent):

```bash
sh /root/turbomac-src/deploy/install.sh --firmware-limits /root/turbomac-src/receipts/phase0-20261009/firmware-limits.json
```

Rollback: `sh /root/turbomac-src/deploy/rollback.sh` (restores 100/125 W, GPU cap 35 W, unmasks thermald; `--purge` also removes group, symlinks, modules-load).

## Verification on the host

- `turbomacctl status`: commanded 35/35, sysfs 35/35, MSR 35/35 with clamp, firmware 100/125, hard max 45; GPU auto 7.0 W cap 15 W; SMC 911 keys; previous boot (Oct 5) classified abrupt and an incident record written.
- Re-assert writes every 10 s with "sysfs and MSR ok"; no readback failures, no restarts, watchdog pings arriving.
- Stop/start path: `ExecStopPost` re-applied the cap before the new instance started; limits never left 35/35 during the redeploy.
- Daemon cost: ~4.3 % of one core (21.7 s CPU over 8.4 min), 30 MB RSS. Worth trimming in Phase 2 (the per-second `/proc` scan for top processes is the likely cost).
- Test suite: 82 tests pass on macOS (Python 3.14.6) and on old-mac as user `parham` with a non-existent sysroot (Python 3.14.4).

## Review and fixes applied before this deploy

An independent review (opus) found no path that writes above 45 W and
confirmed a crash or stop leaves the static cap in place. Fixed before the
redeploy: firmware capture never saves a value taken under an active cap and
ignores suspect files; limits round down to 1/8 W before the exact readback
compare; telemetry/log/status errors cannot skip a governor step; a write
exception counts as a failed readback; the watchdog ping is sent only when a
governor step succeeded in the last 5 s; a fallback never raises the limit and
the emergency rule still fires while waiting to retry; the journal verdict
outranks the clean-shutdown marker; the fsync moved to a writer thread with a
bounded queue (still one fsync per line); SMC rescans are limited to one per
minute; `cpu.no_turbo` defaults to untouched; ACPW outside 30–100 W is invalid;
state dirs are 0750 root:turbomac; the cap script's shell fallback uses the
configured static cap and runs for write errors too.

## Open items

- Dynamic mode is implemented and unit-tested but not yet enabled on the host (Phase 3 validation first).
- GPU `off` is implemented (switcheroo IGD → OFF, verified via PCI `power_state`) but untested on this machine; use only in Phase 3 with SSH open.
- The replay test is open-loop; a closed-loop model would be a nicer regression check.
- The `linux/` tree is not committed to git yet.
