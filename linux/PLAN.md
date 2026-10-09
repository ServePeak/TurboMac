# TurboMac for Linux — plan

Status: proposal, 2026-10-09. Nothing in this directory is deployed yet.

The macOS KEXT governor in the rest of this repo is dormant: old-mac runs Ubuntu
Server 26.04 now. This plan replaces it with three small Linux pieces that keep
the TurboMac names:

| Piece | What it is | Runs as |
|---|---|---|
| `turbomacd` | background daemon: samples everything, logs 1 Hz, enforces the power cap, owns GPU power mode, serves a control socket | root, systemd |
| `turbomactop` | full-screen TUI (bpytop style): live wattage, rails, temps, fans, governor state, settings editor, GPU toggle, incident viewer | any user in group `turbomac`, over the socket |
| `turbomacctl` | thin CLI over the same socket for SSH and scripts | any user in group `turbomac` |

## 1. Goal and non-goals

Goal: no more unexpected power losses on old-mac, with the simplest machinery
that can be verified, plus a cockpit to watch it and tune it.

Non-goals: thermal management (hardware handles it at 100 °C), per-process
scheduling, anything that writes MSRs directly, anything that needs a kernel
module. Everything goes through sysfs that Linux already exposes to root.

## 2. Facts the design rests on (verified 2026-10-09, read-only)

| Fact | Value | Consequence |
|---|---|---|
| Firmware RAPL on Linux | PL1 100 W / 28 s, PL2 125 W / 2.44 ms, unlocked, TDP 45 W | CPU is uncapped today; cap must be re-applied every boot |
| powercap sysfs | `/sys/class/powercap/intel-rapl:0/constraint_{0,1}_power_limit_uw`, root-writable; subdomains core/uncore/dram exist with energy counters | daemon writes PL1/PL2 directly, no KEXT |
| MSR access | `/dev/cpu/0/msr` present, `msr` and `intel_rapl_msr` loaded | daemon can read back 0x610 (limits + clamp bits) and 0x1B1 (power-limited-now bits) |
| Input telemetry | applesmc keys PDTR (input W), ACPW (negotiated W), VD0R (V), ID0R (A) via `key_at_index*`; 911 keys total; no hwmon power attributes | daemon is the single owner of the selector; TUI never touches sysfs |
| Input model (Ubuntu staircase) | input ≈ 20.9 W + 1.24 × package W | 35 W cap ≈ 64 W input, 40 W ≈ 71 W, 45 W ≈ 77 W |
| Adapter | ACPW 79.496 W at ~20.2 V ⇒ 4 A contract (80 W class, not the stock 87 W) | hard ceiling; worth checking adapter and cable |
| dGPU | Radeon Pro 560, always on, 7 W idle, 29 W loaded, drives eDP-1, "Runtime PM not available", hwmon `power1_cap` writable (max 35 W), vga_switcheroo present (`2:DIS:+:Pwr`, `0:IGD: :Pwr`) | cap it; optionally power it off via switcheroo |
| cpufreq | intel_pstate active, HWP on, EPP balance_performance, turbo on | leave alone by default; expose as settings |
| thermald | running, no config, inert, but has rewritten PL1 once | mask it |
| Shutdown classes | load-driven (two Sept 29 tests at 78 W + something); and the Oct 5 cluster at idle with no whisper/GPU/Docker job activity | cap fixes the first; the 1 Hz log is the diagnostic for the second |

## 3. Architecture

```
            sysfs / msr / applesmc / amdgpu / coretemp / proc
                              │  (read 4 Hz fast, 1 Hz slow)
                              ▼
   ┌──────────────────── turbomacd (root) ────────────────────┐
   │ sampler ─► ring buffer (10 min) ─► governor ─► RAPL writes │
   │                 │                      │                    │
   │                 ├─► 1 Hz JSONL log     ├─► GPU mode control │
   │                 │   + incident capture │                    │
   │                 ▼                      ▼                    │
   │        unix socket /run/turbomac/turbomacd.sock (0660 root:turbomac)
   └──────────────────────────────────────────────────────────────┘
                   ▲                          ▲
        turbomactop (TUI)               turbomacctl (CLI)
```

Rules:

- `turbomacd` is stdlib-only Python (3.14 on old-mac). The safety-critical part
  must not depend on a venv. `turbomactop` uses Textual in its own venv.
- The static cap is applied by a shell `ExecStartPre` before Python even
  starts, and re-applied by `ExecStopPost`. A Python crash can only ever leave
  the static cap in place, never the firmware's 100 W.
- Only the daemon touches `key_at_index`, RAPL, amdgpu, switcheroo. Old test
  scripts (`stress-pilot.py` and friends) must not run alongside it; they get
  ported to read from the socket in Phase 4.

## 4. turbomacd

### 4.1 Sampler

| Source | Fields | Rate |
|---|---|---|
| RAPL `energy_uj` for package, core, uncore, dram | W per domain from deltas, wrap-safe via `max_energy_range_uj` | 4 Hz |
| applesmc keys | PDTR, ACPW, VD0R, ID0R; plus any of PSTR, PCPC, PCPG, PCPT, PG0R, PHPC that exist (discovered once from the key table) | 4 Hz for PDTR/V/I, 1 Hz for the rest |
| MSR 0x1B1 (package therm status) | power-limit-now, thermal-throttle-now, PROCHOT bits | 1 Hz |
| MSR 0x610 | current PL1/PL2/clamp/enable as the hardware sees them | on every write + 1 Hz |
| coretemp, x86_pkg_temp, nvme | package and per-core °C | 1 Hz |
| applesmc temps | all `temp*_input` with labels, invalid values (-127/-128, > 150) dropped | 1 Hz |
| applesmc fans | fan1/fan2 input, min, max, manual flag | 1 Hz |
| amdgpu hwmon + sysfs | `power1_input`, `power1_cap`, temp, `gpu_busy_percent`, `pp_dpm_sclk` current, runtime status, switcheroo state | 1 Hz, skipped entirely while the GPU is off |
| cpufreq / proc | per-CPU `scaling_cur_freq`, per-CPU busy % from `/proc/stat`, load, memory, uptime; intel_pstate `no_turbo`, EPP, min/max perf pct; throttle counters | 1 Hz |
| top processes | top 8 by CPU from `/proc`, names only | 1 Hz |

Derived: 1 s means of input and package, non-CPU estimate = mean input − median
package over 1 s (input lags package by ~1 s, lesson from the macOS daemon),
headroom to ACPW, predicted input at the configured ceiling using the fitted
model, session peaks.

### 4.2 Governor

Modes: `off` (telemetry only, TUI shows a warning), `static` (PL1 = PL2 =
`static_cap_w`, re-asserted every 10 s), `dynamic` (static cap as floor of
safety plus trimming on measured input). Default after install: `static`.
Switch to `dynamic` after Phase 3 validation.

Defaults and validation bounds:

| Key | Default | Bounds | Meaning |
|---|---|---|---|
| `static_cap_w` | 35 | 10–45 | boot and fallback cap, Intel's cTDP-down level for this part |
| `ceiling_w` | 40 | static_cap–45 | highest PL1 the dynamic loop may recover to |
| `floor_w` | 10 | 9–20 | emergency target; below ~9 W the package does not respond |
| `burst_w` | 0 | 0–5 | PL2 = PL1 + burst; 0 means no burst, which is the point |
| `shed_ratio` | 0.88 | 0.70–0.95 | of live ACPW (70.0 W): cut |
| `emergency_ratio` | 0.94 | shed+0.03–0.98 | of live ACPW (74.7 W): drop to floor |
| `recover_ratio` | 0.78 | 0.50–shed−0.05 | of live ACPW (62.0 W): allow recovery |
| `cut_step_w` | 5 | 1–15 | per shed event, at most one per 0.5 s |
| `recover_step_w` | 1 | 1–5 | per recovery interval |
| `recover_interval_s` | 3 | 1–30 | time below recover_ratio per step |
| `stale_timeout_s` | 2 | 1–10 | missing SMC data ⇒ fallback |
| `hard_max_w` | 45 | fixed | never write above TDP, whatever the config says |

Loop (dynamic mode, evaluated on every SMC sample):

1. `acpw` = 10 s median of ACPW; thresholds are ratios of it (adapter swaps are
   picked up automatically).
2. latest PDTR ≥ emergency ⇒ PL1 = PL2 = floor now, state EMERGENCY.
3. else 1 s mean PDTR ≥ shed ⇒ PL1 −= cut_step (not below floor), state TRIM.
4. else 1 s mean PDTR < recover for ≥ recover_interval ⇒ PL1 += recover_step
   (not above ceiling). State NORMAL once PL1 == ceiling.
5. EMERGENCY leaves to TRIM after 5 s below shed; then normal recovery rules.
6. Every write is read back from sysfs and from MSR 0x610. Mismatch ⇒ FALLBACK
   (static cap) and retry with backoff.
7. No valid SMC sample for `stale_timeout_s` ⇒ FALLBACK. Fallback is always
   the static cap, never "release to firmware".

Things kept from the macOS daemon: cut immediately, recover slowly; bands as
ratios of live ACPW; 1 s windows; a 9–10 W effective floor; stale telemetry is
a safety event. Things dropped: non-CPU estimator as a control input, the
calibrated prediction path, state targets, HWP bootstrap, PROCHOT handling,
heartbeat-to-kernel, the whisper validation gate.

### 4.3 CPU extras (settings, default untouched)

`cpu.no_turbo` (intel_pstate `no_turbo`), `cpu.epp` (one of the listed
preferences), `cpu.max_perf_pct`. Exposed in Settings as "advanced"; the
governor does not need them.

### 4.4 GPU control

`gpu.mode`:

| Mode | What the daemon does | Expected draw |
|---|---|---|
| `auto` (default) | `power1_cap` = `gpu.cap_w` (default 15 W), DPM level `auto` | 7 W idle, ≤ 15 W |
| `low` | `power1_cap` = max(`power1_cap_min`, 10 W), `power_dpm_force_performance_level` = `low` | 7 W idle, ≤ 10 W |
| `off` | vga_switcheroo: unbind fbcon from the dGPU, switch the panel to `IGD`, then `OFF`; stop polling amdgpu | 0 W; the internal panel may stay black until reboot |

Persistence: the mode lives in the config file and is applied by
`turbomac-cap.service` early in boot and by the daemon at start. `off` is
gated: selecting it from the TUI makes the daemon try it live, verify
(switcheroo reports `DIS: :Off`, `/sys/bus/pci/devices/0000:01:00.0/power/runtime_status`
not `active`, the system still answers after 10 s), and only then persist it.
If verification fails it reverts to `low` and reports why. Turning the GPU back
on writes `ON` then `DIS`; a reboot always restores the firmware state.

### 4.5 Logger and incidents

- `/var/lib/turbomac/samples/samples-YYYYMMDDTHHMMSSZ.jsonl`, one line per
  second with every field above, fsync each line, hourly rotation, 14 days or
  256 MiB (same policy as the macOS observer).
- `/var/lib/turbomac/clean-shutdown` is written by `ExecStop`. At start, if the
  marker is absent and the journal of the previous boot does not end in
  "Journal stopped", the daemon records an incident: boot id, time of the last
  sample, the last 60 samples, and the last 20 journal lines, into
  `/var/lib/turbomac/incidents/`. The TUI shows the latest one on its Incidents
  screen. This is how the Oct 5 class gets explained.
- Band transitions, cuts, writes, fallbacks and config changes go to the
  journal as structured entries (`TURBOMAC_EVENT=...`).

### 4.6 Control socket

Path `/run/turbomac/turbomacd.sock`, mode 0660, owner root, group `turbomac`
(user `parham` is added to it). Newline-delimited JSON.

| Request | Reply |
|---|---|
| `status` | latest sample, derived values, governor state, applied limits, GPU state, config digest |
| `history {"seconds": 600}` | ring buffer for sparklines |
| `subscribe` | a status every 250 ms until the client disconnects |
| `config.get` | full config with defaults filled in |
| `config.set {"governor.ceiling_w": 38}` | validated, applied live, written atomically to `/etc/turbomac/config.json`; reply lists accepted/rejected keys with reasons |
| `config.reset` | back to defaults |
| `gpu.set {"mode": "off"}` | runs the gated procedure, replies with the outcome |
| `incidents` / `incident {"id": ...}` | list / one record |
| `events {"n": 100}` | recent events |
| `smc.keys` | the discovered key table (name, type, length) |

### 4.7 Config file

`/etc/turbomac/config.json`, written only by the daemon (atomic rename), readable
by humans. Defaults:

```json
{
  "schema": 1,
  "governor": {
    "mode": "static",
    "static_cap_w": 35, "ceiling_w": 40, "floor_w": 10, "burst_w": 0,
    "shed_ratio": 0.88, "emergency_ratio": 0.94, "recover_ratio": 0.78,
    "cut_step_w": 5, "recover_step_w": 1, "recover_interval_s": 3,
    "stale_timeout_s": 2
  },
  "gpu": { "mode": "auto", "cap_w": 15 },
  "cpu": { "no_turbo": false, "epp": null, "max_perf_pct": null },
  "logging": { "retention_days": 14, "max_mib": 256 }
}
```

### 4.8 systemd

- `/etc/modules-load.d/turbomac.conf`: `msr`, `intel_rapl_msr`.
- `turbomac-cap.service` (oneshot, `Before=docker.service systemd-user-sessions.service`,
  `WantedBy=multi-user.target`): shell only; writes the static cap and GPU
  mode from the config, verifies readback, logs. Runs before any workload
  (Docker stack, Hermes under the lingering user session).
- `turbomacd.service` (`Requires=turbomac-cap.service`, `After=` it,
  `Restart=always`, `WatchdogSec=10`, `ExecStartPre=` static cap,
  `ExecStop=` marker, `ExecStopPost=` static cap, `RuntimeDirectory=turbomac`,
  `StateDirectory=turbomac`).
- `thermald.service` masked by the installer; unmasked by rollback.

## 5. turbomactop (TUI)

Stack: Python + Textual (Rich under it), in `/opt/turbomac/venv`, pinned
version. Dark theme, mouse and keyboard, braille sparklines, gradient bars.
Runs unprivileged; everything comes from the socket. Also runs on the Mac
against `--mock` (synthetic) or `--replay <jsonl>` (the Sept stress-test logs)
for development without touching old-mac.

Screens, switched with `1`–`5`:

```
┌ TurboMac · old-mac · Ubuntu 26.04 · up 4d 01:27 · governor DYNAMIC · NORMAL ──────── 16:59:44 ┐
│ INPUT  62.4 W ▕█████████████████████████░░░░░░░░▏ 79.5 W   20.23 V × 3.08 A   headroom 17.1 W │
│        shed 70.0 ▲    emergency 74.7 ▲      ▂▃▃▅▆▇▇▆▅▅▆▇█▇▆▅▄▃▃▂▂▂▃▄  60 s   peak 71.2 W     │
├ CPU ───────────────────────────────┬ GPU · Radeon Pro 560 ───────┬ GOVERNOR ─────────────────┤
│ package 31.9 W   PL1 35 · PL2 35   │ 7.0 W   cap 15 W   AUTO     │ NORMAL  since 00:12:31    │
│ core 27.1  uncore 1.2  dram 2.6    │ 50 °C   busy 0 %  214 MHz   │ PL1 35 → ceiling 40       │
│ 3.21 GHz  busy 68 %  61 °C  PL✓    │ rt active · panel: dGPU     │ cuts 3  emerg 0  fallbk 0 │
│ c0 ▇▇▇▇▇▇ 3.3  c1 ▇▇▇ 3.1 …        │                             │ non-CPU est 30.5 W        │
├ TEMPS ─────────────────────────────┴ FANS ──────────────────────┴ RAILS (SMC) ───────────────┤
│ pkg 61  TC0E 67  TC0F 71  TCGC 58  TG0D 57  TM0P 45  NVMe 41 │ L 2167  R 2001 rpm │ PSTR … │
└ [1] Dashboard  [2] Settings  [3] GPU  [4] Incidents  [5] Log     r reset peaks   q quit ─────┘
```

1. **Dashboard**: as above. Input gauge against ACPW with band markers; 60 s and
   10 min sparklines for input, package, GPU, package temp; CPU breakdown by
   RAPL domain; per-core MHz/busy bars; "power-limited now" and "thermal
   throttle" flags from MSR 0x1B1; adapter line with inferred PD contract;
   governor panel with state, limits, counters, last action; session peaks.
2. **Settings**: every key from §4.7 as an editable field with its bounds and a
   one-line meaning; live preview "ceiling 40 W ≈ 71 W input (89 % of adapter)"
   from the fitted model; `Enter` applies (daemon validates), `s` saves,
   `d` resets to defaults; rejected values show the daemon's reason inline;
   a badge shows applied/persisted state and the last change time.
3. **GPU**: three-state selector auto / low / off, current draw, cap, DPM
   level, runtime status, switcheroo line, panel owner; selecting `off` shows
   the gated procedure's progress and outcome.
4. **Incidents**: list of abrupt boots found; for the selected one, the last
   60 s of input/package/GPU as sparklines, last values, top processes, and
   the last journal lines.
5. **Log**: the daemon's event stream (band changes, cuts, writes, fallbacks,
   config changes) with filters.

Keys: `1`–`5` screens, `r` reset peaks, `p` pause sparklines, `?` help, `q`
quit. Mouse works on tabs and fields.

## 6. turbomacctl

`turbomacctl status | watch | config [key=value ...] | reset | gpu auto|low|off |
incidents [id] | events [n] | smc-keys | log-tail`. Same socket, JSON with
`--json`. Used by the installer's smoke test and by future scripts.

## 7. Repo layout

```
linux/
  PLAN.md                     this file
  README.md                   install, use, rollback (written in Phase 1)
  turbomac/                   Python package
    daemon/   sampler.py smc.py rapl.py msr.py gpu.py governor.py logger.py incidents.py server.py main.py
    tui/      app.py screens/ widgets/ mock.py replay.py
    ctl.py    config.py protocol.py
  deploy/
    turbomac-cap.service turbomacd.service turbomac-cap.sh
    modules-load.conf sysusers.conf
    install.sh rollback.sh
  tests/
    test_governor.py test_smc_decode.py test_rapl.py test_config.py
    test_replay.py (drives the governor with power-test JSONL from the Sept tests)
    fixtures/
  requirements-tui.txt
```

## 8. Deployment and rollback

Install (from the Mac): `rsync linux/ old-mac:/opt/turbomac/src/`, then on
old-mac `install.sh`: create group `turbomac`, add `parham`, create
`/etc/turbomac`, `/var/lib/turbomac`, venv for the TUI with pinned Textual,
install units and modules-load, capture firmware limits to
`/var/lib/turbomac/firmware-limits.json`, mask thermald, `systemctl enable --now
turbomac-cap turbomacd`, run `turbomacctl status` as the smoke test.

Rollback: `rollback.sh` stops and disables both units, unmasks thermald,
restores the captured firmware limits and GPU defaults, leaves logs in place.
A reboot also restores every hardware default.

## 9. Testing

- Unit: governor step function (pure: state + sample + config ⇒ new state +
  writes), SMC byte decoding against recorded raw bytes, RAPL µW encoding and
  readback comparison, config validation bounds, incident detection logic.
- Replay: feed `power-test/cpu-ram-native-20260929` and the staircase logs
  through the governor and assert it would have cut before 78 W and recovered
  afterwards.
- TUI: Textual's pilot tests for screen switching and settings submission
  against the mock backend.
- On old-mac: Phase 1 runs a day in `off` mode (telemetry + logging only) to
  compare its numbers against turbostat and the Sept scripts, then `static`.

## 10. Phases and acceptance

| Phase | Deliverable | Done when |
|---|---|---|
| 0 | Manual, supervised, 10 min: mask thermald, PL1 = PL2 = 35 W, GPU cap 15 W, verify MSR 0x610 | input under load ≤ ~66 W on a short stress-ng run; nothing else changes |
| 1 | `turbomacd` + `turbomacctl` + units + installer + tests | a day of clean logs in `off`, then `static`; incident detection verified by a deliberate clean reboot and by reading the Oct 5 boots from the journal |
| 2 | `turbomactop` dashboard, settings, GPU (auto/low), incidents, log | runs on the Mac in mock/replay and on old-mac unprivileged; a settings change round-trips to disk and to the hardware |
| 3 | Supervised validation (owner present, SSH open) | staircase under cap; cold-start whisper large-v3 × 8 threads peaks ≤ ~72 W; GPU `off` procedure verified or rejected; then `dynamic` enabled and ceiling set from the data |
| 4 | Optional: built-in load generator panel (stress-ng wrapper, like the macOS cockpit), Telegram alerts via Hermes on incidents/emergencies, porting the old test scripts to the socket, EPP/turbo experiments | as wanted |

## 11. Risks and open questions

- The Oct 5 class is not a CPU-power event by the evidence; the cap will not
  fix it. The incident log is the way to learn what it is. Adapter, cable and
  mains deserve a look in parallel (an Apple 87 W or 96 W adapter would also
  lift ACPW).
- `key_at_index` is a global selector; any other reader corrupts samples. The
  daemon is the only reader; old scripts are retired.
- vga_switcheroo `OFF` on a 2017 MBP works in community reports but has not
  been tried on this machine. Hence the gating and SSH-only operation.
- Reading amdgpu hwmon while the device is suspended can wake it; the sampler
  skips the GPU entirely in `off` mode.
- Whisper throughput drops roughly 12–18 % at 35 W versus today's thermally
  throttled 45 W. Phase 3 picks the final ceiling.
- Python/Textual versus a single Go binary: Python chosen because the SMC
  reading code already exists in the owner's scripts, Python 3.14 is on the
  host, and iteration is faster. The daemon's stdlib-only rule keeps the
  safety path dependency-free. A Go rewrite of the TUI stays possible later.
