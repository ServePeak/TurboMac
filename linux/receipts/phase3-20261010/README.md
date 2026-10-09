# Phase 3 receipt — supervised validation, 2026-10-09 22:20–22:40 UTC

Owner present, SSH open, production stack running (19 containers). Loads were
run through `tools/phase3_run.py`, which watches the daemon's status stream
(never the SMC directly) and kills the workload on guard limits. Raw rows are
in the JSON files beside this file. ACPW 79.496 W throughout.

## Results

| Test | Governor | Load | Input mean / peak | Package | Pkg °C peak | Governor actions | Outcome |
|---|---|---|---|---|---|---|---|
| 1 static40 | static, cap 40 W | stress-ng matrixprod ×8, 30 s | 62.5 / 64.1 W | 39.9 W held | 92 | none (static) | pass; CPU reports power-limited |
| 2 dyn40 | dynamic, ceiling 40 W | same, 30 s | 60.5 / 61.8 W | 38.1 W mean | 97 (guard at 29 s, load ending) | ramp 35→40 W in 1 W/3 s steps before the run; NORMAL throughout, 0 cuts | pass |
| 3a shed75 | dynamic, ceiling 40, bands lowered to shed 0.75 (59.6 W) / recover 0.70 (55.6 W) | same, 30 s + 25 s settle | 61.7 W peak then held 50–56 W | 30→35 W | 89 | TRIM at 2.3 s, 2 cuts (40→35→30, rate-limited), 10 recoveries inside the hysteresis band, back to NORMAL 40 W 17 s after the load ended; 0 emergencies, 0 fallbacks | pass: cut-and-hold works live |
| 3b ceil45 | dynamic, ceiling 45 W, real bands | same, 25 s | 66.6 / 68.8 W | 43.3 W mean, drooping | 97 | 0 cuts (stayed under the 70 W shed band) | pass, but thermally limited |
| 4 whisper8 | dynamic, ceiling 40 W | cold start (66 °C), `whisper-cli ggml-small -t 8` on a 130 s clip, run as `parham` | 59.0 / 61.1 W | 36.5 W mean, 39.3 W peak | 97 (guard at 31 s) | 0 cuts; input reached 58 W at 1.5 s and 60.7 W at 2.5 s, never above 61.1 W | pass: the real-world trigger stays 18 W under the adapter |

Fans never exceeded ~3,900 rpm (max 5,927) in any run: the SMC fan curve lags
the load by 20–30 s, so sustained 40 W reaches the CPU's 97–100 °C limit before
the fans catch up. That is a cooling issue, not a power one; the hardware
throttle then reduces package power, which only lowers input further.

## Decision and persisted settings

`governor.mode = dynamic`, `ceiling_w = 40`, `static_cap_w = 35`, bands
0.88 / 0.94 / 0.78 (defaults), `gpu.mode = auto` with `cap_w = 15`. Written to
`/etc/turbomac/config.json` by the daemon; a reboot starts at the static 35 W
cap and the governor ramps to 40 W within 15 s of valid telemetry.

Why 40 and not 45: 45 W gives about 10 % more CPU for 11 W of margin and
97 °C within 25 s; 40 W gives 17–18 W of margin and the same thermal ceiling
a little later.

## GPU off (owner's go given 22:45 UTC)

`turbomacctl gpu off --yes` from an SSH session: fbcon unbound, switcheroo
`IGD` then `OFF`, verified immediately and after 10 s (switcheroo `DIS: :Off`,
PCI `power_state` D3hot). Kernel: "Console: switching to colour dummy device",
"snd_hda_intel 0000:01:00.1: Disabling via vga_switcheroo", "amdgpu
0000:01:00.0: switched off". No daemon errors; Hermes gateway, telegram-bot-api
and all 19 containers kept running. `gpu.mode = off` is persisted and is applied
by `turbomac-cap.service` at every boot.

| | Before | After |
|---|---|---|
| Idle input | 21–23 W | 11–12 W |
| Non-CPU estimate | ~14 W | ~8 W |
| Headroom at idle | ~57 W | ~68 W |

The internal panel stays black until a reboot or `turbomacctl gpu auto`
(switcheroo `ON` + `DIS`, fbcon rebound).

## Reboot test (22:40 UTC, owner watching)

`systemctl reboot`; SSH back after 46 s. Boot timeline (kernel-monotonic):

| t | Event |
|---|---|
| 9.22 s | `turbomac-cap.service` starts |
| 9.36 s | PL1 = PL2 = 35 W written and verified in sysfs and MSR 0x610 (firmware 100/125 recorded) |
| 9.37 s | GPU `off` step FAILS: the vga_switcheroo debugfs entry and amdgpu are not up yet |
| 9.76 s | `turbomacd` started; previous boot judged **clean** ("Journal stopped"); dynamic mode resumes from 35 W (TRIM) |
| 11.05 s | user session (Hermes gateway) starts, already under the cap |
| 14.03 s | Docker starts, already under the cap |
| ~25 s | governor reaches 40 W (NORMAL); daemon's GPU retry succeeds: switcheroo `DIS Off`, PCI D3hot |

All 19 containers and Hermes came back. Fix applied after the test and
redeployed: "vga_switcheroo not available" is now an explicit retry trigger
(it had matched only through the revert text), and `apply_mode` re-discovers
the amdgpu device if it was absent at daemon start, so status shows the PCI
power state after an early-boot retry. The Radeon is on for roughly the first
25 s after boot; the CPU cap is in place from 9.4 s.

## Notes

- `turbomacctl --json status`: the `--json` flag goes before the subcommand.
- `tools/phase3_run.py` prints the guard once and keeps watching for `--settle`
  seconds after the workload ends so recovery is visible.
- Possible Phase 4 item: a fan floor (applesmc `fan*_min` is writable) raised
  while the package is above ~80 °C, so sustained whisper runs stop hitting the
  thermal limit.
