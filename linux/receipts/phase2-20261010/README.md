# Phase 2 receipt — turbomactop installed 2026-10-09 22:11 UTC

`turbomactop` (Textual 8.2.8) is installed on old-mac in `/opt/turbomac/venv`
and runs unprivileged over the daemon socket. Verified end to end: a headless
session started as user `parham` connected to `/run/turbomac/turbomacd.sock`
and captured every screen with live data (`live-*.svg` in `../phase2-screens/`
alongside the mock and replay captures).

## Run it

| Where | Command |
|---|---|
| On old-mac over SSH | `ssh -t old-mac turbomactop` (as `parham`, group `turbomac`; root works too) |
| On the Mac, synthetic data | `cd linux && .venv-tui/bin/python -m turbomac.tui --mock [--seed N]` |
| On the Mac, real old-mac data | `cd linux && .venv-tui/bin/python -m turbomac.tui --replay fixtures/samples-oldmac-20261009.jsonl.gz --speed 10` |
| Screenshots (SVG) | `cd linux && .venv-tui/bin/python -m turbomac.tui.screenshots` |

Keys: `1`–`5` screens, `r` reset peaks, `p` pause, `?` help, `q` quit. Mouse
works on tabs, fields and the GPU selector.

## What each screen does

1. Dashboard: input gauge against ACPW with recover/shed/emergency markers and the
   predicted input at the current PL1; V × A and the inferred PD contract; 60 s
   and 10 min history bars on fixed physical scales; CPU (RAPL domains, PL1/PL2
   commanded/sysfs/MSR, MHz, busy, °C, power-limited/thermal/PROCHOT flags,
   per-core bars); GPU (W, cap, mode, °C, busy, clock, DPM, runtime, PCI power
   state, switcheroo, panel owner); governor (state badge, counters, non-CPU
   estimate, last action, fallback reason); all SMC temps, fans, the SMC rails,
   top processes, session peaks; an incident banner after an abrupt boot.
2. Settings: every `config.SPEC` key with value, default, bounds, meaning and
   cross-field rule; Enter applies via `config.set` (daemon validates, persists
   to `/etc/turbomac/config.json`); rejected values show the daemon's reason;
   `d` resets to defaults; live preview of the ceiling and static cap in input
   watts and percent of ACPW.
3. GPU: auto / low / off selector; `off` opens a modal that requires typing OFF
   and then shows the daemon's step list; the mode the hardware reached is what
   gets persisted.
4. Incidents: list and detail with sparklines from the saved samples and the
   journal tail.
5. Log: daemon events with text and type filters.

## Build facts

- Package `turbomac/tui/`: about 2,800 lines including CSS; backends `LiveBackend`
  (socket, subscribe stream on a worker thread, reconnect with backoff),
  `MockBackend` (deterministic, honours config and GPU changes, fake incidents),
  `ReplayBackend` (daemon sample logs, plain or gzipped; mapping documented in
  `replay.py`).
- Tests: 101 with the venv, 82 without it (the 19 TUI tests skip when Textual
  is absent, so the daemon suite stays stdlib-only). One pilot test,
  `test_every_screen_mounts_and_keys_switch`, failed twice while the machine was
  under heavy load (screenshot rendering and the remote capture) and passed in
  every one of the next seven full runs, two of them under six busy-loop
  processes. It is treated as a load-sensitive pilot timing flake; no code was
  changed for it. The logger test that depended on the real clock was pinned to
  a fixed timestamp instead.
- `deploy/install.sh`: the TUI section is appended after the daemon smoke test
  and never fails the install; it needs `python3-venv` on the host (installed on
  old-mac 2026-10-09). `rollback.sh --purge` removes the venv.
- Fixture `fixtures/samples-oldmac-20261009.jsonl.gz`: 587 real 1 Hz records
  from old-mac in static mode, used by replay mode and the tests.

## Daemon-side gaps the TUI works around (candidates for a later daemon change)

- No OS release in `status`: live mode reads the local `/etc/os-release`.
- `governor.since` is monotonic: the TUI shows time in state, not a wall-clock since.
- No last-change time or persisted flag in `status`: taken from the last
  config/GPU event and the last `config.set` reply.
- `burst_w` is not in `status`: PL2 is shown instead.
- The PD contract line is inferred from ACPW and voltage.
