# Phase 4 receipt — fan floor, 2026-10-09 23:10–23:20 UTC

## What was added

`turbomacd` now raises the applesmc fan minimums (`fan{N}_min`) ahead of the
SMC's lagging fan curve. The SMC keeps automatic control; the daemon only sets
a floor it respects. `fan*_manual` and `fan*_output` are never written.
Controller in `turbomac/daemon/fans.py`, run from the 1 Hz slow tick after the
governor step; hardware minimums captured once into
`/var/lib/turbomac/fan-defaults.json` (2160 / 2000 rpm on old-mac) and restored
on clean stop, on `fan.mode=auto`, and by `rollback.sh`.

Rule: floor = max(power rule, temperature ladder), capped at `fan.max_rpm`.

| Trigger | Engages | Releases (one step per `step_down_interval_s` = 20 s) |
|---|---|---|
| package ≥ 25 W (1 s median) | 3500 rpm at once | after 20 s below 20 W |
| package ≥ 78 °C | 3500 rpm at once | after 20 s below 72 °C |
| ≥ 84 °C (was 86 in the first run) | 4500 rpm | after 20 s below 78 °C |
| ≥ 90 °C (was 92) | 5500 rpm | after 20 s below 84 °C |

All keys live under `fan.*` in the config and in the TUI Settings screen;
`turbomacctl config fan.mode=auto` disables it.

## Hardware probe (before the code)

Writing 3000 to `fan1_min`/`fan2_min`: both fans at 3000 rpm within 2 s,
readback held, `fan*_manual` stayed 0; restoring 2160/2000 brought them back in
~3 s.

## Validation (owner present)

`phase3_run.py --stress 8 --seconds 60`, dynamic mode, ceiling 40 W, cold start
at 48 °C, raw rows in `fanfloor60.json`. Compared with the Phase 3 `dyn40` run
(same load, no floor):

| | Phase 3 dyn40 (no floor) | With fan floor |
|---|---|---|
| Fans at 4 s | 2150 / 2000 rpm (SMC minimum) | 3364 / 3460 rpm (power rule at 2 s) |
| Package °C at 30 s | 97 (thermal guard tripped at 29 s) | 80 |
| Peak °C | 97 at 29 s | 87 at 58 s (4500 rpm step at 86 °C, 55 s) |
| Input at 40 W package | 60.5 W mean / 61.8 peak | 55.5 W mean / 56.6 peak |
| Governor | NORMAL, 0 cuts | NORMAL, 0 cuts |
| Fan write errors | – | 0 |

The cooler die leaks less: the same 40 W package costs about 6 W less at the
adapter. After the load: 4500 → 3500 rpm 27 s later (20 s below 78 °C), back to
the hardware minimum 20 s after that. Events seen in order: `fan_defaults`
(captured), `fan_floor 3500 "power 38 W ≥ 25 W"`, `fan_floor 4500 "pkg 86 °C ≥
86 °C"`, `fan_floor 3500 "cooling …"`, `fan_floor 0 "below thresholds"`.

After the run the ladder was tightened to 84 / 90 °C (defaults in `config.py`
and applied on the host), since noise does not matter on a closed-lid server.

## Other changes in this phase

- `rollback.sh` restores fan minimums from the defaults file (fallback 2160 /
  2000 with a plausibility check).
- 1 Hz sample lines carry a small `fan` record; the TUI FANS panel shows the
  floor and reason; Settings has the `fan` section; the mock backend simulates
  the floor.
- `turbomacctl events` prints non-ASCII reason text unescaped.
- Two TUI timer callbacks were made safe during shutdown (a pre-existing race
  that the larger Settings screen made visible).

# Phase 4 item 2 — daemon CPU tidy-up, 2026-10-09 23:30–23:55 UTC

Baseline on old-mac: 5.13 % of one core over 120 s with no client attached
(user 1.2 %, system 2.4 %). The system share is the applesmc driver busy-waiting
on the SMC port: one sysfs read of an SMC sensor costs 0.23 ms of CPU, and a
selector key read is two transactions (name + data). The daemon was doing about
93 SMC transactions per second.

Changes (no governor change; PDTR still 4 Hz through the selector with the name
check on every read):

| | Before | After |
|---|---|---|
| /proc process scan for the top-8 list | every 1 s | every 5 s |
| sysfs discovery of sensors (glob + labels) | every 1 s | once |
| `derived()` per tick | up to 3× | 1× |
| status snapshot | every 250 ms | every 250 ms only while a client is connected, else 1 Hz |
| ACPW / VD0R / ID0R reads | 4 Hz | 1 Hz (PDTR stays 4 Hz) |
| applesmc temperatures, SMC rails, fan min/max/manual | 1 Hz | every 3 s (fan rpm and coretemp stay 1 Hz) |
| SMC transactions per second | ≈ 93 | ≈ 37 |

Measured with the same 120 s method:

| | CPU of one core | user / system |
|---|---|---|
| baseline | 5.13 % | 1.2 / 2.4 |
| round 1 (scan, caches, snapshot) | 3.54 % | – |
| round 2 (+ SMC diet) | **2.09 %** | 0.85 / 1.23 |
| round 2 with `turbomacctl watch` attached | 2.33 % | – |

RSS 26 MiB. The daemon now reports its own CPU and memory in `status`
(`self.cpu_pct`, `self.rss_mib`), on the first line of `turbomacctl status`,
and in the TUI header on wide terminals. `turbomacctl status` with no other
client connected can be up to 1 s old by design.
