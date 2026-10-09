# TurboMac for Linux

Power governor and telemetry for old-mac, a batteryless 2017 MacBook Pro 14,3
(i7-7820HQ, Radeon Pro 560, 80 W-class adapter) running Ubuntu Server. Design
and rationale: [PLAN.md](PLAN.md).

| Piece | What it does |
|---|---|
| `turbomac-cap.service` | early boot, shell + minimal Python: PL1 = PL2 = static cap, GPU mode/cap, readback |
| `turbomacd` | root daemon: samples at 4 Hz, logs 1 Hz JSONL, runs the governor, owns the GPU mode, serves `/run/turbomac/turbomacd.sock` |
| `turbomacctl` | CLI over the socket, for users in group `turbomac` |
| `turbomactop` | TUI, Phase 2 (not in this tree yet) |

The daemon and CLI use only the Python standard library (Python 3.10+, 3.14 on old-mac).

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

## Rollback

```sh
sudo sh /opt/turbomac/src/deploy/rollback.sh
```

Stops and disables both units, unmasks and starts `thermald`, restores the
captured firmware limits (100 W / 125 W on this machine; a suspect or invalid
capture is ignored in favour of those), powers the dGPU back on if needed and
restores its cap to max with DPM `auto`. Kept: logs, incidents, config, the
disabled unit files and `/opt/turbomac`, plus `/etc/modules-load.d/turbomac.conf`,
`/etc/sysusers.d/turbomac.conf`, the `/usr/local/bin` symlinks and group
`turbomac`. `rollback.sh --purge` also removes those last four. A reboot also
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
cd linux && python3 -m unittest discover -s tests -v
```

Paths come from `TURBOMAC_SYSROOT` (default `/`), with `TURBOMAC_SOCKET`,
`TURBOMAC_CONFIG` and `TURBOMAC_STATE` overrides, so the daemon itself can run
against a fake tree: `TURBOMAC_SYSROOT=/path/to/tree TURBOMAC_SOCKET=t.sock python3 -m turbomac.daemon`.
`tests/fixtures/replay-20260929.jsonl` is the Sept 29 near-full-memory run that
ended in a power-off at 78.3 W input, plus a spliced cooldown.
