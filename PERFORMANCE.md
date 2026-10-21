# TurboMac performance adaptation — MacBookPro15,1 audit and rollout plan

Engineering handoff-driven adaptation: maximize sustained CPU throughput on the
host MacBook Pro (15-inch, 2018), accepting battery discharge, 100 °C junction
operation, and loud fans. This document is the audit, the staged actuation plan,
and the rollback path from the 2026-10-20 handoff. Identifying data is
deliberately redacted here; the daemon binds full identity internally
(`calibration.conf`).

## 1. What was verified on the host (read-only, 2026-10-20)

- Model `MacBookPro15,1`; CPU `Intel(R) Core(TM) i7-8750H CPU @ 2.20GHz`
  (family 6, model 158 = 0x9E, stepping 10) — **passes the KEXT CPUID gate**.
- macOS 15.8.1 (build 24H32), Darwin 24.6.0 x86_64. Build/tested via
  CommandLineTools SDK; the Makefile targets are green (see section 7).
- Battery installed and healthy (8 cycles, ~103 % design capacity), Apple
  adapter negotiating 20 V × 4.7 A ≈ 94 W; no thermal/performance warnings
  recorded by `pmset -g therm`.
- Baseline clamp, under all-core load: package power **23.11 W**, average CPU
  clock 2.1 GHz (95 % of nominal), max active cores 5.98/6, CPU die 72.9 °C,
  0 PROCHOT assertions, iGPU ~65 % active. **The bottleneck is the legacy XCPM
  frequency ceiling, not package power or package temperature.**
- The macOS clock cap is policy, not power: with the captured PL1 expected at
  45 W (to be confirmed at first boot), the chip never approaches it while
  pinned at 2.1–2.3 GHz all-core. SMC thermal level signals (CPU plimit 20.00)
  have an undocumented scale; direct AppleSMC user-client reads were rejected
  on this build (`e00002c2`), so external probes cannot decode them.

## 2. Audit of the Live code (what enforces what)

| Assumption | Enforcer | Status for this host |
|---|---|---|
| CPU must be family 6, model 0x9E | `TurboMac/TurboMac.cpp: initializeCapabilitiesLocked()` | matches (0x9E) |
| Profile bound to UUID + model + CPU brand + OS version/build + SMC key types | `Daemon/Profile.cpp: currentIdentity/matches`, `Daemon/main.cpp: loadProfile()` | binding is captured from this host at dark-install time; no 14,3 data is used |
| Never write above Apple's captured PL1/PL2 | `TurboMac.cpp: installLimitsLocked()` + `validateRequestLocked()` | unchanged; protects against over-limit writes |
| RAPL lock bit refusal | `TurboMac.cpp: installLimitsLocked()` reads `TM_RAPL_LOCK` first | unchanged |
| Arm order: guard verified → capture → 5 W bootstrap (only when HWP is boot-disabled) → limits → HWP release → then clear BD_PROCHOT | `TurboMac.cpp: arm()` (lines ~323–426) | preserved as designed |
| Disarm/fault restores guard **first**, then HWP fail-safe, then limits | `TurboMac.cpp: restoreLocked()` (~line 1412) + passive watchdog `ensurePassiveGuardLocked()` | preserved; tested below |
| Batteryless input-power control loop (Guard/Shed/Emergency driven by ACPW ratios) | `Daemon/Policy.cpp` + schema-2 ratio profiles | **wrong primary loop for this host — replaced by schema 3 (below)** |
| 45/52/58 W bands and ACPW calibration from the 14,3 | schema-2 profile content only | never reused; schema 3 does not read them |

## 3. The adaptation (implemented in this tree)

Schema version 3 = **performance profile**. Changes:

- `Daemon/Profile.h/.cpp`: `performanceMode`, `performancePL1W`,
  `performancePL2W` fields; schema 3 keys; validation requires
  `performancePL1W ≥ raplFloorW`, `performancePL1W ≤ profile PL1 ceiling`,
  `performancePL2W ≥ performancePL1W`, `performancePL2W ≤ profile PL2 ceiling`,
  and **always rejects `autoArm`** (performance arm is manual/supervised only).
- `Daemon/Policy.h/.cpp`: `fixedTargets` mode — PL1/PL2 come straight from the
  validated targets, clamped to the verified live ceilings; the ACPW bands stay
  computed for telemetry/TUI but drive no limit changes.
- `Daemon/main.cpp`: `configurePolicy()` applies fixed targets from the
  profile (never above the live captured limits); `status` now reports
  `"policy_mode": "performance"` (JSON) / `Governor mode:` (human) /
  TUI-ignored extra field.
- BD_PROCHOT: unchanged from upstream-fork design — the guard is cleared only
  inside a verified arm after RAPL/HWP readbacks pass, restored **first** on
  disarm/heartbeat-timeout/driver-stop, re-verified while passive, and
  observable via `apple_guard_enabled` in status.
- No undervolting; no throttle-patch of Apple thermal extensions; Intel TCC,
  thermal trip, and automatic shutdown remain untouched.

Battery/input handling per the handoff: discharge during load is not an
intervention trigger; the bands remain visible telemetry. Any future
automatic input-power behavior must be a separately designed and tested
change (not part of this adaptation).

## 4. Before first actuation — measurements still required (first boot)

1. `hwp_enabled` from passive `turbomacctl status` (no arm): decides whether
   arm needs the boot-scoped 5 W HWP bootstrap or only maximum-release.
2. Captured Apple PL1/PL2 (`driver_ceiling_pl1_w/_pl2_w`) — expected ≈ 45/56 W.
3. SMC key readability of `PDTR`/`ACPW` through the KEXT path (the external
   user-client probe being rejected does not decide this; if SMC sampling
   fails the daemon stays passive — checks that gate arm exist already).
4. A short, bounded baseline benchmark before arming (below).

## 5. Staged rollout (explicit human consent at each gate)

0. **Package**: `script/build_and_run.sh` (tests + verify + package).
1. **Install (reduced security)**: `Deploy/install-passive.sh` stages the
   KEXT; approve the removal in System Settings when the installer stops with
   return code 27; then `sudo turbomac-finalize-passive`; reboot; verify
   over all-SSH that status shows passive/armed=off, guard on.
2. **Baseline**: record status JSON at idle, 2 min pinned 1 thread, and
   2 min 12-thread `openssl speed` (or `turbomac-avx2-load`) — clocks, package
   watts, temps, fan RPM, band telemetry.
3. **Profile**: build a schema-3 profile bound to this identity (targets
   inside the *captured* ceilings; first target PL1 = captured PL1, PL2 =
   captured PL2). No auto-arm; `calibrate` from the 14,3 path is not needed
   for schema 3.
4. **Arm (supervised)**: `sudo turbomacctl arm` with `turbomactop` running.
   Confirm: requested = read-back PL1/PL2, HWP release verified, guard
   cleared only at the verified end step, clocks rise toward the power/
   thermal-defined limit (~3.3–3.5 GHz all-core expected at 45 W).
5. **Stress + fault tests**: sustained run (plan §6 metrics); then verify the
   fail-safe — stop the daemon (SIGKILL), let the heartbeat expire, `disarm`
   — each must restore the guard first and the captured RAPL limits.

## 6. Test metrics and failure definition

Log: completed work/throughput and duration; package power; PL1/PL2
(requested + read-back); effective clocks (powermetrics); TCC/throttle flags
and PROCHOT count; sustained/peak CPU temps; fan RPM; battery current/temp/
charge state; input watts and connection resets; governor transitions.
**Failure** = kernel panic, unexpected shutdown, charger disconnect/reset,
abnormal battery temperature/behavior, proven broken rollback, or unstable
charging. Battery drain alone is not a failure. A single surviving run is not
proof of safety.

## 7. Verification done so far

- `make test`: all 8 suites pass, including the new schema-3 profiles and
  fixed-target policy cases (bands never move limits; emergency is
  telemetry-only; out-of-bounds targets clamped; PL2 never below PL1).
- `make build`: daemon + CLI + AVX2 load + TUI + placeholder bounds build
  clean x86_64 with `-Werror`.
- Not yet executed on hardware: install, load, arm, and any stress test
  (deliberately requires explicit consent and reduced security).

## 7b. Battery-buffered allowance (added 2026-10-20)

Operator request implemented in the daemon policy: while armed and the battery
is above 50% state of charge, the input-power bands no longer cut the CPU
limit — desired PL1 holds the cruise target minus reserve without subtracting
the non-CPU estimate, so total demand may exceed the DC input rail and the
battery covers the deficit. Guards: limits never exceed the verified KEXT
ceilings; SoC ≤ 50%, an invalid/missing reading, or disarm hand authority
straight back to the normal bands; the reading refreshes once per second
(IOPowerSources) and is exposed as `battery_percent`/`battery_buffered` in
status. Battery-buffered behaviour is disabled in fixed-target performance
mode, which already ignores the bands. First-boot caveat documented: an
elevated non-CPU estimate (observed ≈59 W before calibration) still produces
emergency-band telemetry, so expect emergency annunciators while buffered.

## 8. Rollback

```sh
sudo /usr/local/libexec/turbomac-rollback \
  "/Library/Application Support/TurboMac/rollback/TIMESTAMP"
```

The rollback script restores the captured pre-install KEXT, rebuilds the
kernel collections, and refuses to proceed if rebuilding fails. Removing
`calibration.conf` (or any mismatch after an OS update) forces the daemon
passive. A reboot restores native non-HWP state if TurboMac enabled HWP.
The guard bit (BD_PROCHOT) is restored on every disarm path and re-verified
by the passive watchdog every second.

## 9. Known limitations

- No claims about hardware damage are nullified by tests; this tool writes
  CPU power-management registers unprotected.
- Surviving a load run is not proof that external component protections
  remain adequate; with BD_PROCHOT cleared, external throttling requests
  (VRM/charger/GPU) are ignored until disarm.
- The 100 °C acceptance never disables Intel TCC/thermal trip/shutdown.
- HWP reconciliation with a future macOS update can change the `validated`
  conclusion at any time (identity binding will reject the stale profile).
