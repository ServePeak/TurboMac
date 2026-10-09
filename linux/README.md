# TurboMac for Linux

Power governor and telemetry for old-mac, a batteryless 2017 MacBook Pro 14,3
(i7-7820HQ, Radeon Pro 560, 80 W-class adapter) running Ubuntu Server. Design
and rationale: [PLAN.md](PLAN.md).

| Piece | What it does |
|---|---|
| `turbomac-cap.service` | early boot, shell + minimal Python: PL1 = PL2 = static cap, GPU mode/cap, readback |
| `turbomacd` | root daemon: samples at 4 Hz, logs 1 Hz JSONL, runs the governor, owns the GPU mode and the fan floor, serves `/run/turbomac/turbomacd.sock` |
| `turbomacctl` | CLI over the socket, for users in group `turbomac` |
| `turbomactop` | full-screen TUI over the socket (Textual, own venv): dashboard, settings, GPU mode, incidents, event log |

The daemon and CLI use only the Python standard library (Python 3.10+, 3.14 on old-mac).
`turbomactop` needs Textual, pinned in `requirements-tui.txt` and installed into its own venv.

## Install

From the Mac:

```sh
rsync -a --delete linux/ old-mac:/tmp/turbomac-src/
ssh old-mac 'sudo sh /tmp/turbomac-src/deploy/install.sh'
```

`install.sh` is idempotent and refuses to run unless the CPU is an
`Intel(R) Core(TM) i7-7820HQ` (`--force` overrides). It copies `turbomac/` to
`/opt/turbomac/src`, writes `/opt/turbomac/bin/{turbomacd,turbomacctl,turbomac-cap.sh}`
(symlinked into `/usr/local/bin`), creates group `turbomac` and adds `parham`,
loads `msr` and `intel_rapl_msr` (and persists them in `/etc/modules-load.d`),
writes `/etc/turbomac/config.json` from defaults if absent, captures the
firmware RAPL limits, installs the units, masks `thermald`, enables and starts
both units, and runs `turbomacctl status` as a smoke test.

Firmware limits are captured once, before TurboMac writes any limit, into
`/var/lib/turbomac/firmware-limits.json`; the daemon never writes above them or
above 45 W, and `rollback.sh` restores them. If a manual cap is still active
(e.g. from Phase 0) when you install, the live values are not the firmware's.
Either reboot before installing, or pre-seed the true values:

```sh
sudo sh deploy/install.sh --firmware-limits /path/to/firmware-limits.json
```

The file must be JSON with numeric `pl1_w`, `pl2_w`, `pl1_uw`, `pl2_uw` (other
keys are kept); the installer refuses anything else. It is copied (mode 0644)
before the capture step and only if `/var/lib/turbomac/firmware-limits.json`
does not exist yet. For this machine the verified firmware values are:

```json
{"pl1_w": 100.0, "pl2_w": 125.0, "pl1_uw": 100000000, "pl2_uw": 125000000,
 "pl1_window_us": 27983872, "pl2_window_us": 2440, "enabled": 1}
```

Without a seed, a live capture with PL1 below 45 W (a cap is already active) is
never saved and never used as a clamp: the daemon clamps to 45 W only and warns,
and the next boot captures the true values before the cap service writes. A
saved file marked `suspect` or with non-numeric limits is treated as missing,
and `rollback.sh` ignores it in favour of 100 W / 125 W.

## Verify

```sh
turbomacctl status            # one screen: input, bands, RAPL domains, PL1/PL2 (commanded, sysfs, MSR), GPU, temps, counters
turbomacctl watch             # one line per 250 ms update
turbomacctl events 50         # writes, cuts, state changes, fallbacks, config changes
sudo /opt/turbomac/bin/turbomac-cap.sh show    # raw sysfs + MSR 0x610/0x1B1 decode, no daemon needed
journalctl -u turbomac-cap -u turbomacd -b
```

Check that `limits` shows the same PL1/PL2 three times (commanded, sysfs, MSR)
and `readback failures 0`. `boot` reports whether the previous boot ended
cleanly.

## Modes and tuning

`governor.mode`:

- `off`: telemetry and logging only. Switching into `off` writes the static cap
  once; after that the daemon writes no limits (at startup in `off`, the cap
  service has already applied the static cap).
- `static` (default): PL1 = PL2 = `static_cap_w` (35 W), re-asserted every 10 s.
- `dynamic`: starts at the static cap, recovers 1 W per 3 s up to `ceiling_w`
  while input is below `recover_ratio` x ACPW, cuts `cut_step_w` (at most every
  0.5 s) when the 1 s mean input reaches `shed_ratio` x ACPW, drops to `floor_w`
  when one PDTR sample reaches `emergency_ratio` x ACPW. Stale SMC data (2 s),
  a readback mismatch or a write error means FALLBACK to the static cap (never
  above the limit already in force), retried with 1/2/4/8 s backoff; the
  emergency rule keeps working while it waits. Limits are rounded down to the
  1/8 W RAPL unit before writing. ACPW outside 30..100 W counts as invalid.

```sh
turbomacctl config                                   # show everything
turbomacctl config governor.mode=dynamic governor.ceiling_w=38
turbomacctl config cpu.epp=balance_power cpu.max_perf_pct=80  # JSON values; bare words are strings
turbomacctl config cpu.epp=null                     # null = leave the kernel value alone (default for
                                                    # cpu.no_turbo, cpu.epp and cpu.max_perf_pct)
turbomacctl reset                                   # back to defaults
turbomacctl gpu low                                 # auto | low | off (off asks you to type OFF)
```

Every change is validated (bounds and cross-field rules from PLAN 4.2; the reply
names each rejected key and why), applied live, and written atomically to
`/etc/turbomac/config.json`. Edit the file by hand only with the daemon stopped,
or send `SIGHUP` to reload it.

`gpu off` unbinds fbcon, switches the panel to the iGPU and powers the dGPU off
through vga_switcheroo, verifies (switcheroo shows `DIS` `Off` and, where the
attribute exists, the PCI `power_state` is no longer `D0`; `runtime_status` is
not used because this dGPU has no runtime PM), waits 10 s, verifies again, and
only then persists `off`; on any failure it reverts to `low` and tells you why.
The internal panel may stay black until reboot. Do it over SSH, supervised.

amdgpu can still be probing when the cap service and the daemon start. If the
daemon's startup GPU apply fails with "amdgpu device not found" or "amdgpu
hwmon not found", the control loop retries it every 15 s, up to 12 times
(`gpu_retry`, `gpu_retry_succeeded`, `gpu_retry_gave_up` events).

## Fan floor

What: the daemon raises applesmc's `fan1_min` / `fan2_min` (the floor the SMC's
own fan curve respects) when the CPU is working hard, and lowers it again when
it cools down. The SMC stays in automatic control; TurboMac never writes
`fan*_manual` or `fan*_output`.

Why: the SMC curve lags the load by 20-30 s. In the Phase 3 runs a sustained
40 W package reached the CPU's 97-100 °C limit in about 30 s while the fans were
still at ~3,900 of 5,927 rpm (`receipts/phase3-20261010/README.md`), so the
hardware throttled. The floor spins the fans up as soon as the power arrives.

How (once a second, from the daemon's 1 Hz tick, after the governor step):

- power rule: 1 s median package power ≥ `fan.power_floor_w` (25 W) ⇒ at least
  `fan.power_floor_rpm` (3500 rpm), engaged immediately;
- temperature ladder on the coretemp package sensor: ≥ `t1_c` 78 °C ⇒ 3500 rpm,
  ≥ `t2_c` 86 °C ⇒ 4500 rpm, ≥ `t3_c` 92 °C ⇒ 5500 rpm, stepping up immediately;
- floor = the higher of the two, at most `fan.max_rpm` (5500 rpm) and each fan's
  own `fan*_max` (the right fan tops out at 5489 rpm), never below the captured
  hardware minimum;
- release is slow: a rule lets go only after its input has been below
  threshold − hysteresis (`fan.hysteresis_c` 6 °C for a ladder step, 5 W for the
  power rule) continuously for `fan.step_down_interval_s` (20 s), and the ladder
  steps down one step per interval;
- a missing temperature or power reading makes that rule contribute nothing
  (both missing ⇒ no floor; the SMC curve still runs).

`fan*_min` is written only when the per-fan value changes, then read back
(±50 rpm); events `fan_floor`, `fan_floor_error` (a failed value is retried at
most every 30 s), `fan_restore`, `fan_mode`. The hardware minimums are captured
once, before the first floor is written, into `/var/lib/turbomac/fan-defaults.json`
(same idea as `firmware-limits.json`). If a start finds a floor already applied
(more than 500 rpm above the known 2160 / 2000 rpm), nothing is saved and the
known minimums are used until a later start captures plausible values.

A clean daemon stop writes the hardware minimums back; a crash leaves the floor
in place (more cooling, the safe side) until the daemon starts again. Status:
`turbomacctl status` (fans line: `floor 3500 rpm (power 31 W ≥ 25 W)` or
`floor off`), `turbomacctl watch` (` fan 3500` while a floor is active), the
FANS panel in `turbomactop` (target, with the reason in the panel's bottom
border), and the `fan` field of every sample-log line.

```sh
turbomacctl config fan.mode=auto        # disable: restores the hardware minimums now, SMC curve only
turbomacctl config fan.mode=floor       # enable again (the default)
turbomacctl config fan.t1_c=80 fan.power_floor_rpm=3000
```

Bounds: `power_floor_w` 10-45 W, every `*_rpm` 2000-5900 (`max_rpm` 2500-5900),
`t*_c` 50-99 °C with `t1_c < t2_c < t3_c` and `t1_rpm ≤ t2_rpm ≤ t3_rpm`,
`hysteresis_c` 2-15, `step_down_interval_s` 5-120. A `fan.*` change applies on
the next 1 Hz tick; switching to `auto` restores immediately.

## turbomactop

A bpytop-style cockpit for the daemon. It runs unprivileged (any user in group
`turbomac`) and gets everything from the control socket: it never reads sysfs,
the SMC selector or RAPL, and never writes anything except through
`config.set`, `config.reset` and `gpu.set`.

`install.sh` sets it up after the daemon smoke test: if `python3 -c 'import ensurepip'`
works it creates `/opt/turbomac/venv`, installs `requirements-tui.txt` (Textual
8.2.8, idempotent) and writes `/opt/turbomac/bin/turbomactop` (symlinked into
`/usr/local/bin`). Without `python3-venv` it prints
`TUI skipped: run 'apt-get install -y python3-venv' and re-run install.sh` and
carries on; a failed pip install is a warning, never an install failure. The
daemon never uses the venv. `rollback.sh --purge` removes it.

```sh
ssh -t old-mac turbomactop                      # live, as parham (log in again after install for the group)
turbomactop --socket /run/turbomac/turbomacd.sock
```

On the Mac, without a daemon:

```sh
cd linux
python3 -m venv .venv-tui && .venv-tui/bin/pip install -r requirements-tui.txt
.venv-tui/bin/python -m turbomac.tui --mock [--seed N]
.venv-tui/bin/python -m turbomac.tui --replay fixtures/samples-oldmac-20261009.jsonl.gz [--speed 10]
```

- `--mock`: a deterministic simulation of old-mac (seeded). Idle is about 21 W
  input / 6 W package / 7 W GPU; every 120 s an 8-thread "whisper" burst holds
  the package at PL1 (about 56-58 W input at 35 W), and every other burst also
  loads the dGPU to its cap. The daemon's own sampler, governor step, config
  validation and `build_status` run on top, so settings changes behave like the
  real thing: in `dynamic` mode a GPU-loaded burst at the 40 W ceiling crosses
  the shed band and gets cut. It also has two fake incidents and an event log.
- `--replay FILE`: plays a turbomacd sample log (`.jsonl` or `.jsonl.gz`, e.g.
  from `/var/lib/turbomac/samples/`) at 1 record/s x `--speed`, looping. Settings
  changes are validated and shown (bands, ceiling, modes) but never saved. The
  record-to-status mapping is documented in `turbomac/tui/replay.py`.

| Screen | Key | What |
|---|---|---|
| Dashboard | `1` | input gauge against ACPW with recover/shed/emergency markers and the input predicted for the current PL1; V x A and inferred PD contract; 60 s and 10 min sparklines (input, package, GPU, package temp); CPU (RAPL domains, PL1/PL2 as sysfs and MSR see them, power-limited/thermal/PROCHOT, per-core GHz and busy); GPU; governor (state, time in state, PL1 to ceiling, counters, non-CPU estimate, last action, fallback reason); all temperatures; fans against their hardware range, the fan floor target and its reason; SMC rails; top processes; session peaks; a banner when the previous boot ended abruptly |
| Settings | `2` | every config key with its value, default, bounds and meaning; edit and press `Enter` (selects apply when chosen); turbomacd's verdict shows inline (`REJECTED: ...` with the reason); `d` resets to defaults after a y/n; live preview of ceiling to predicted input and the bands at the live ACPW; config digest, persisted state, last change time |
| GPU | `3` | auto / low / off, current draw, cap, DPM level, runtime status, PCI power state, switcheroo, panel owner, the steps of the last mode change. `off` (here or in Settings) opens a dialog that only proceeds when you type `OFF` |
| Incidents | `4` | abrupt boots; the selected one shows input/package/GPU sparklines of its last 60 s, the last values, top processes and the journal tail |
| Log | `5` | the daemon's events, newest last, with a text filter and a type filter |

Other keys: `r` resets the session peaks, `p` pauses and resumes the display,
`?` help, `q` quit, `Esc` leaves a text field (while a field has focus, keys go
to it). Tabs, fields, radio buttons and table rows are clickable. The layout is
made for 120x36; below 110 columns the rails, top-processes and 10 min
sparklines are hidden and the dashboard scrolls (80x24 works).

Screenshots of every screen (mock and replay) are in
`receipts/phase2-screens/`; regenerate them with
`.venv-tui/bin/python -m turbomac.tui.screenshots`.

## Rollback

```sh
sudo sh /opt/turbomac/src/deploy/rollback.sh
```

Stops and disables both units, unmasks and starts `thermald`, restores the
captured firmware limits (100 W / 125 W on this machine; a suspect or invalid
capture is ignored in favour of those), powers the dGPU back on if needed and
restores its cap to max with DPM `auto`, and writes the fan minimums back from
`/var/lib/turbomac/fan-defaults.json` (when that file is missing, or an entry is
missing or more than 500 rpm above the hardware minimum, it falls back to the
verified hardware minimums 2160 rpm for fan1 and 2000 rpm for fan2). Kept: logs, incidents, config, the
disabled unit files and `/opt/turbomac`, plus `/etc/modules-load.d/turbomac.conf`,
`/etc/sysusers.d/turbomac.conf`, the `/usr/local/bin` symlinks and group
`turbomac`. `rollback.sh --purge` also removes those last four, and the TUI venv
`/opt/turbomac/venv`. A reboot also
restores every hardware default.

## Logs and incidents

- `/var/lib/turbomac/samples/samples-YYYYMMDDTHHMMSSZ.jsonl`: one fsynced line
  per second (about 2.4 KB; first line of each file is a header with the boot
  id), new file every UTC hour. The control loop only enqueues; a writer thread
  writes and fsyncs each line (120 s buffer; if the disk stalls longer the
  oldest records are dropped and a `log_backlog` event is emitted). Finished
  files are gzipped to `.jsonl.gz` (at rotation and at startup, on a background
  thread; the current file stays plain). `samples/` and `incidents/` are mode
  0750, group `turbomac`. Retention: 14 days or 1024 MiB, whichever comes first
  (`logging.retention_days`, `logging.max_mib`, 16..8192). Read with
  `zcat samples-*.jsonl.gz | jq ...`.
- `/var/lib/turbomac/incidents/*.json`: written at daemon start when the
  previous boot's journal does not end in "Journal stopped": previous boot id,
  last sample time, last 60 samples and the last 20 journal lines.
  `turbomacctl incidents [id]`. The journal decides whenever it is available;
  the clean-shutdown marker only counts when the journal is unavailable and the
  marker was written during the previous boot (per `journalctl --list-boots`).
- `/var/lib/turbomac/clean-shutdown`: written on a clean stop (`ExecStop` and
  daemon exit), consumed at the next start; a fallback for the journal check.
- Journal: structured events as `TURBOMAC_EVENT {json}` lines
  (`journalctl -u turbomacd -g TURBOMAC_EVENT`).
- `/var/lib/turbomac/smc-keys.json`: SMC key table cache (rebuilt if the key
  count changes or a key moves).
- `/var/lib/turbomac/fan-defaults.json`: the fans' hardware minimums, captured
  once before the first fan floor (see Fan floor).

## Rules worth knowing

- **Single SMC reader.** applesmc's `key_at_index` is one global selector. Any
  other process writing it (old `stress-pilot.py`, `probe.py`, a stray `cat`
  loop) corrupts the daemon's samples and vice versa. Only `turbomacd` touches
  it; everything else reads from the socket. The daemon verifies the key name
  on every read (a mismatch triggers a key-table rescan at most once a minute;
  otherwise that sample is invalid) and restores the original selector on exit.
- **MSR reads must be unbuffered.** Use `os.open` + `os.pread(fd, 8, reg)`. A
  buffered Python file (`open(..., "rb")` + seek + read) returned a stale
  buffer slice during testing. TurboMac never writes an MSR.
- **The watchdog tracks the governor.** `WATCHDOG=1` is sent only while a
  governor step has succeeded within the last 5 s; telemetry, logging and status
  errors are caught separately and never skip the step. A wedged or failing
  governor stops the pings, systemd restarts the daemon, and `ExecStopPost`
  re-applies the static cap.
- **The cap survives crashes.** `ExecStartPre` and `ExecStopPost` re-apply the
  static cap from shell, so a Python crash leaves the static cap in place, never
  the firmware's 100 W. If the TurboMac Python code cannot run,
  `turbomac-cap.sh` echoes `governor.static_cap_w` from the config (read with a
  one-line plain `python3 -c`, accepted if 10..45 W) or else 35 W into both
  constraints directly.
- While `gpu.mode` is `off`, only the switcheroo file and the PCI `power_state`
  attribute are read; nothing else under the amdgpu device is touched (reading
  hwmon can wake a powered-down dGPU).

## Development

Everything runs on macOS against fake sysfs trees; no hardware needed:

```sh
cd linux && python3 -m unittest discover -s tests -v              # daemon + CLI; TUI tests skip
cd linux && .venv-tui/bin/python -m unittest discover -s tests    # everything, TUI included
```

Paths come from `TURBOMAC_SYSROOT` (default `/`), with `TURBOMAC_SOCKET`,
`TURBOMAC_CONFIG` and `TURBOMAC_STATE` overrides, so the daemon itself can run
against a fake tree: `TURBOMAC_SYSROOT=/path/to/tree TURBOMAC_SOCKET=t.sock python3 -m turbomac.daemon`.
`tests/fixtures/replay-20260929.jsonl` is the Sept 29 near-full-memory run that
ended in a power-off at 78.3 W input, plus a spliced cooldown.
