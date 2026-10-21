# TurboMac Dynamic RAPL Governor

This fork replaces TurboMac's one-shot HWP and XOR writes with a passive-first,
package-scoped Intel RAPL governor and an explicit, boot-scoped HWP switchover.
It is adapted for a 2018 MacBook Pro 15 (MacBookPro15,1): Intel Core
i7-8750H, Radeon Pro 555X, stock 87 W USB-C adapter, battery installed, so a
USB-C input-power deficit is buffered by the battery instead of dropping the
power. The upstream `parhamfa/TurboMac` line targeted a 2017 MacBook Pro 14,3
(i7-7820HQ, no battery). Both CPUs are Intel family 6, model `0x9E`, 45 W
class, and the RAPL and HWP handling is the same.

This is not a general-purpose thermal utility. The KEXT refuses to arm on a CPU
family/model other than Intel family 6, model `0x9e`. The daemon additionally
binds a calibration profile to the machine UUID, model, CPU brand, SMC key
types, macOS version, and build.

## What it controls

Package RAPL PL1/PL2 provide the actual power limit. The limit applies to the
whole Intel CPU package, so native processes and VM workloads receive the same
package budget. This Mac boots with HWP disabled and a legacy XCPM ceiling. To
escape that ceiling safely, the KEXT can enable HWP only after it has installed
and read back a package limit no higher than 5 W, while Apple's guard is still
enabled. It then installs a conservative HWP request on every logical CPU,
restores the requested governed RAPL limit, releases the HWP maximum, and only
then clears the guard.

`IA32_PM_ENABLE.HWP_ENABLE` cannot be cleared without a processor reset. Thus,
disarm cannot return this boot to its original non-HWP state; it means “Apple
guard on, conservative HWP fail-safe active, captured Apple RAPL restored.” A
reboot is required for native non-HWP restoration. The KEXT never writes
`IA32_PERF_CTL`, changes per-process controls, writes arbitrary MSRs, sets the
RAPL lock bit, or raises a RAPL limit above Apple's captured value.

Discrete GPU, display, USB devices, storage, conversion losses, and other power
rails remain outside CPU RAPL. This can reduce CPU-caused input surges; it cannot
guarantee that the Mac will not lose power.

## Control bands

The observed `45/52/58 W` values are controller bands, not proven shutdown-safe
limits. They are the schema-v1/default policy and scale continuously from the
median live AppleSMC `ACPW` value:

| Band | Ratio of ACPW | At ACPW 79.496 W |
|---|---:|---:|
| Guard | 56.61% | 45.003 W |
| Shed | 65.41% | 51.998 W |
| Emergency | 72.96% | 58.000 W |

Schema-v2 profiles can override all three ratios and independently set the
cruise, guard, and shed target ratios. Keeping transition thresholds separate
from state targets matters for aggressive experiments: a `55/60/65 W` profile
with targets derived only from its 5 W gap would barely reduce CPU power in
guard or shed. The supervised aggressive profile for this host instead uses
`55/60/65 W` thresholds, `55/48/42 W` state targets, and the calibrated RAPL
floor for emergency. Profile validation requires ordered thresholds, ordered
targets no higher than guard, and at least 5% ACPW below negotiated capacity at
the emergency boundary.

## Battery-buffered allowance

While the battery sits above 50% state of charge and the governor is armed, the
battery absorbs adapter deficits by design, so the input-power bands do not
cut the CPU limit. Desired PL1 rides at the profile's cruise target minus the
calibrated reserve, without subtracting the non-CPU estimate, and total demand
can exceed the DC input rail. The bands keep reporting as telemetry, the live
limit never goes above the verified ceilings, and the mode fails safe: an
unknown, invalid or missing reading, or a state of charge of 50% or less, hands
authority back to the normal bands right away. The daemon refreshes the reading
once per second from IOPowerSources and reports `battery_percent` and
`battery_buffered` in status. This is operator-approved spending of battery
capacity. Deep cycling near the gate wears the battery faster, and the
allowance does not make overdraw safe; it only makes the battery the deficit's
first victim.

## Performance mode

A schema-3 profile replaces the input-power bands with fixed RAPL targets. The
profile carries `performance_mode`, `performance_pl1_w` and `performance_pl2_w`,
and the daemon holds PL1 and PL2 at those values, clamped to the ceilings
captured from Apple firmware. The bands keep running as advisory telemetry,
but they no longer move a limit: on this host the adapter budget is not a CPU
limit, because the battery absorbs the difference. PL2 is never installed below
PL1.

Validation refuses targets below the calibrated RAPL floor, above the captured
ceilings, or with PL2 under PL1, and it refuses `auto_arm` outright: a
performance hand-off is supervised, never automatic. `turbomacctl status`
reports the active loop as `policy_mode`, either `performance` or
`input_governed`. The audit of this host, the staged rollout and the rollback
path are in [PERFORMANCE.md](PERFORMANCE.md).

The daemon samples package energy at 10 Hz and AppleSMC `PDTR`/`ACPW` at 4 Hz.
It estimates non-CPU input from filtered input minus the one-second median CPU
package power, with fast-rise/slow-fall filtering applied once per new SMC
sample. The median rejects short CPU spikes that would otherwise erode the idle
baseline. Because SMC input telemetry can trail the package energy counter at a
CPU load edge, a bounded two-second causal package envelope blocks only an
impossible upward attribution while package power is collapsing; downward
recovery remains available. Direct measured input still owns the immediate
emergency path. The independent fast prediction path uses the calibrated
package-to-input mapping without reusing the slow non-CPU estimate.

PL1 is the active state's input target minus the non-CPU estimate and calibrated
reserve. Decreases apply immediately. Recovery is headroom-aware: at least 10%
ACPW below guard permits 1 W/s, 5-10% permits one watt every two seconds, and
near guard retains the conservative 1 W/5 s rate. PL2 is equal to PL1 outside
cruise, where a separately tested burst allowance may apply. Emergency
prediction takes the more conservative of the coherent live-rail estimate and
the independent calibrated package-to-input mapping.

Transitions use the hysteresis defined in `Daemon/Policy.cpp`: guard requires
two seconds, shed one second, and emergency is immediate from measured or
predicted input. Recovery takes 5/10/15 seconds. If non-CPU load remains above
the emergency band while RAPL is already at its calibrated floor, the daemon
logs `non_cpu_over_budget` and keeps the CPU at that floor.

## Fail-safe boundary

The KEXT starts passive, restores Apple's bidirectional PROCHOT guard if an old
bypass or late macOS power-management initialization leaves it disabled, and
rechecks every second while passive. It permits one root client. If HWP is still
disabled, the first arm follows this order:

1. capture Apple's package limits and verify the guard on every logical CPU;
2. install and read back a RAPL bootstrap limit at or below 5 W;
3. enable HWP and verify `IA32_PM_ENABLE` on every logical CPU, read its
   capabilities and each CPU's default request, then install and verify a
   conservative request everywhere;
4. install and verify the daemon's requested RAPL limits while the conservative
   HWP request and Apple guard are still active;
5. install a balanced autonomous HWP request using Intel's default EPP of 128,
   release its maximum, and verify every logical CPU;
6. only then clear bidirectional PROCHOT enable bit 0 on every logical CPU.

When HWP was already enabled before this driver enabled it, arm instead requires
every logical CPU to have a readable restricted maximum and captures those
requests exactly. On daemon disconnect, malformed commands, unsafe readback,
explicit disarm, driver stop, or a five-second heartbeat timeout, restoration
always enables Apple's guard first. For HWP enabled by TurboMac it then installs
the conservative per-CPU request before restoring Apple's RAPL limits. If that
request cannot be verified, the low RAPL limit is retained rather than restoring
Apple's higher limit. While passive, the watchdog re-verifies both the guard and
the conservative request every second. Status reports whether TurboMac enabled
HWP, whether the fail-safe has exact readback, and whether native restoration
requires a reboot.

Every per-CPU HWP request access first checks that CPU's live
`IA32_PM_ENABLE.HWP_ENABLE` bit. A missing bit or a newly appearing logical CPU
fails the broadcast safely: Apple's guard remains enabled and the low RAPL limit
is retained instead of touching an unavailable request MSR.

Auto-arm requires ten continuous seconds of valid telemetry, a protected
root-owned profile, exact identity matching, and a prior successful fixed
Whisper validation. On a later boot, auto-arm still passes through the same
verified 5 W HWP bootstrap stage.

## Cockpit stress dashboard

`turbomactop` is a full-screen ANSI terminal dashboard for supervised live
experiments. It reads the protected daemon status socket, samples real CPU MHz
through `powermetrics`, and displays total input, adapter capacity, package and
estimated non-CPU power, temperature, governor band, PL1/PL2, HWP/guard health,
headroom, session peaks, transitions, and a 60-second input-power trend. The
dashboard redraws in place on the alternate screen. It homes the cursor and
overwrites every line instead of clearing the terminal on each frame, so a
shrinking frame no longer leaves stale rows behind.

Run it in a terminal on the governed Mac:

```sh
sudo /usr/local/bin/turbomactop
```

The integrated AVX2/FMA load is manual and begins stopped at zero duty. Up/down
(or `W`/`S`) changes each active worker's duty by 5%, left/right (or `A`/`D`)
selects 1-8 workers, Space or Enter starts or stops, `M` selects the 100% x 8
maximum preset without starting it, `0` or `X` immediately returns the generator
to idle, `R` resets session peaks, and `Q` exits. The dashboard's keyboard RX
counter and last-action field confirm that the controlling terminal delivered
the command. `--no-frequency` disables the `powermetrics` subprocess if only the
TurboMac socket metrics are wanted.

There is deliberately no automatic watt, temperature, telemetry, or governor-
state cutoff. Amber/red cockpit annunciators and the terminal bell are warnings
only; the selected load continues until the operator stops it or the process is
terminated. At 100% x 8 the workload continuously drives all logical CPUs with
AVX2/FMA and on a batteryless machine they can cause abrupt power loss; on this
machine (battery installed) the battery buffers such deficits. Save work
before testing and do not mistake a surviving run for proof of a safe input
boundary.

## Build and test

Xcode 26's toolchain and SDK are used directly.

```sh
./script/build_and_run.sh
```

This runs unit tests, builds all deployment artifacts as `x86_64`, ad-hoc signs
them, verifies signatures and plists, and creates `build/package`. On an x86_64
target it also runs `kmutil print-diagnostics -z` for this explicitly
SIP-disabled, ad-hoc-signed installation. Tests cover RAPL encoding, HWP field
preservation and package/local source selection, 32-bit energy wraparound,
malformed requests, watchdog timing, profile policy bounds, controller scaling,
independent state targets, transitions, hysteresis, slew limiting, the fixed
performance targets of a schema-3 profile, the battery-buffered allowance and
its hand-back at the 50% gate, and both the balanced and aggressive paths.

Historical observer logs can be replayed without actuation:

```sh
make replay FILES="/path/to/samples-*.jsonl"
```

Historical `smc_pcpc_w` is only a package-power proxy; calibration uses real
RAPL energy telemetry.

## Supervised rollout

`Deploy/install-passive.sh` backs up the existing KEXT and live kernel state,
installs only under `/Library`, `/usr/local`, and the TurboMac application-
support directory, validates the bundle, and rebuilds the AuxKC. It never edits
`/System/Library` and never reboots automatically. If a rebuild fails, it puts
the original files back and attempts to rebuild their collections before
refusing to proceed. macOS return code 27 is handled separately because it is
the expected user-approval gate, not a linker failure: the new files remain
staged and the installer tells the operator to approve TurboMac and run
`sudo /usr/local/libexec/turbomac-finalize-passive` before rebooting.
`Deploy/rollback.sh` moves
new files into a recoverable directory, restores the captured KEXT, and rebuilds
the AuxKC. The scripts use the documented `kmutil load` staging flow so
`kernelmanagerd` rebuilds only the auxiliary collection; they do not invoke the
system-only `kmutil install --update-all` path or attempt to write the sealed
boot/system collections.

The packaged rollback command is:

```sh
sudo /usr/local/libexec/turbomac-rollback \
  "/Library/Application Support/TurboMac/rollback/TIMESTAMP"
```

After the passive reboot and SSH verification, `status` should show HWP
supported but disabled, Apple guard enabled, and the governor disarmed. Then:

```sh
sudo turbomacctl status
sudo turbomacctl calibrate
sudo turbomacctl validate
sudo turbomacctl arm
sudo turbomacctl disarm
sudo turbomacctl logs
```

Calibration requires an interactive confirmation and physical supervision. Its
first 5 W arm may perform the reset-only HWP transition described above. It uses
an eight-thread deterministic AVX2 load, three 30-second runs per 2 W PL1 step,
and repeated five-second PL2 bursts. Progress is streamed as each run is
measured. It aborts on `PDTR >= 52 W`, CPU temperature `>= 95 C`, ACPW change
over 5%, a read failure, or a limit mismatch; it discards a non-responsive RAPL
tier after responsiveness has been established and conservatively stops a sweep
at 50 W. Initial requests below the CPU package's observed loaded floor are
recorded and skipped until the first responsive tier. PL2 testing selects its
cruise base from the completed input/package map plus reserve; it never uses the
temporarily inflated post-load non-CPU filter. Its absolute PL2 ceiling is also
capped at the largest value actually tested from that base. Calibration finishes disarmed,
which leaves HWP enabled under its conservative fail-safe until reboot. It also
leaves auto-arm disabled. Only a successful protected, fixed Whisper workload
enables auto-arm. Validation aborts at the active profile's live shed threshold
(52 W for the balanced profile and 60 W for the 55/60/65 W profile), so adapter
capacity scaling and the configured controller bands remain authoritative.

`Deploy/build-whisper-validation-runtime.sh` reproducibly builds the pinned GGML
0.17.0 and whisper.cpp 1.9.1 sources with GGML's default backend directory set
to the protected validation bundle. `Deploy/configure-whisper-validation.sh`
then copies the model, audio fixture, binary, and all non-system libraries into
that root-owned, hash-pinned bundle. Configuration refuses a runtime that loads
a mutable Homebrew backend. The runtime helper drops to the nominated
unprivileged account before running two eight-thread CPU-only transcriptions.

Keep an independent observer running during rollout. Do not treat a completed
calibration or a surviving excursion as proof of a universal shutdown boundary.

## Linux port

The same host runs a second implementation under Ubuntu Server, in `linux/`:
`turbomacd` and `turbomacctl` in stdlib-only Python, and `turbomactop` as a
Textual TUI. It carries the rollout plan, the fan floor, the incident log and
the validation receipts of the reference machine, plus the phase-by-phase plan
for bringing it up. See [linux/README.md](linux/README.md).

## License and provenance

Forked from [mndhvn/TurboMac](https://github.com/mndhvn/TurboMac) through
[parhamfa/TurboMac](https://github.com/parhamfa/TurboMac), which replaced the
one-shot writes with the passive-first RAPL governor described here. Original
work copyright Marko Calasan, 2022. Modifications are distributed under the GNU
GPL v3.0; see `LICENSE`. There is no warranty.
