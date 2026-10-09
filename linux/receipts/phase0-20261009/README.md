# Phase 0 receipt — 2026-10-09, 20:31–20:40 UTC

Manual, supervised, reversible changes applied to old-mac over SSH as root.
Evidence files in this directory came from `/root/turbomac-phase0/` on the host.

## Changes

| Item | Before | After | How |
|---|---|---|---|
| thermald | active, enabled | masked, inactive | `systemctl mask --now thermald` |
| RAPL PL1 (`intel-rapl:0/constraint_0_power_limit_uw`) | 100 W | 35 W | sysfs write |
| RAPL PL2 (`constraint_1_power_limit_uw`) | 125 W | 35 W | sysfs write |
| RAPL time windows | 28 s / 2.44 ms | unchanged | not touched |
| MSR 0x610 readback | `0x4283e800dd8320` | `0x43811800dd8118` = PL1 35 W en+clamp, PL2 35 W en+clamp, unlocked | `tools/probe.py --msr` |
| Radeon `power1_cap` | 35 W | 15 W | sysfs write (`cap_min` is 0) |
| Radeon DPM level, runtime PM, switcheroo | auto / active / `2:DIS:+:Pwr` | unchanged | — |
| intel_pstate (turbo, EPP, perf pct) | on / balance_performance / 20–100 | unchanged | — |

Not persistent: a reboot restores the firmware values until the Phase 1
`turbomac-cap.service` is installed. Rollback script on the host:
`/root/turbomac-phase0/rollback.sh` (restores 100/125 W, GPU cap 35 W, unmasks
thermald).

## Acceptance run

`probe.py --seconds 25 --hz 2 --stress 8` = `stress-ng --cpu 8 --cpu-method matrixprod`,
abort guards 72 W input / 97 °C (not triggered). ACPW 79.496 W.

| Measure | Idle (6 s) | Loaded (t ≥ 2 s) |
|---|---|---|
| Input PDTR mean / peak | 21.8 W / 31.0 W (probe key scan) | 55.9 W / 56.5 W |
| Package RAPL mean / peak | 6.6 W / 14.1 W | 34.9 W / 35.0 W (cap held exactly) |
| Core RAPL | — | ~30.9 W |
| GPU | 7.0 W | 7.1–7.2 W |
| Input V × A | 20.30 V × 1.07 A | 20.19 V × 2.79 A |
| Package temp | 57–65 °C | 73 → 85 °C, still rising at 25 s |
| Fans L / R | 2165 / 1998 rpm (minimum) | unchanged, SMC had not ramped yet |
| Non-CPU (input − package) | ~15 W | ~21 W |

Headroom to ACPW under full CPU load: ~23 W. The September model
(input ≈ 20.9 + 1.24 × package) predicted 64 W at 35 W; the measured 56 W is
lower because that model came from runs with a RAM stressor and fans at
maximum. Load composition moves total input by roughly 8–10 W, which is why
the dynamic mode acts on measured input rather than on a model.

Additional SMC power keys confirmed present and readable: PSTR, PCPC, PCPG,
PCPT, PG0R, PHPC (values in `stress-35w.json`).

## Observations

- MSR 0x1B1 showed the PROCHOT log bit and the power-limit log bit set before
  any change (something asserted PROCHOT earlier in this boot). Live bits were
  clear during the run; the cap did not trigger the hardware power-limit flag
  at 2 Hz sampling.
- Reading `/dev/cpu/0/msr` through a buffered Python file object after a seek
  returns a stale buffer slice. Use `os.pread`. `probe.py` does.
- stress-ng 0.20.01 syntax is `--cpu N --cpu-method matrixprod`.
