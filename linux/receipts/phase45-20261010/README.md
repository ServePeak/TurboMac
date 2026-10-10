# 45 W ceiling retest with the fan floor (2026-10-10, owner's go)

Question: is the 40 W production ceiling leaving compute on the table now that
the GPU is off and the fan floor is in?

## Input model refit (deployed 14:57 UTC)

`INPUT_MODEL` was `(20.9, 1.24)`, fitted in Phase 0 with the GPU on and the
firmware fan curve, so the TUI showed "PL1 40 W ≈ 70.5 W". With the GPU off and
the fan floor, 40 W measures 55.5–56.1 W mean input. The slope is kept and the
intercept refit: `(8.0, 1.24)` → 40 W ≈ 57.6 W, 45 W ≈ 63.8 W, 50 W ≈ 70 W
(the shed band). Display only; the governor acts on measured input.

## Runs

`phase3_run.py --seconds 60 --abort-input-w 70 --abort-temp-c 95 -- stress-ng
--cpu 8 --cpu-method matrixprod --timeout 60s --metrics-brief`, dynamic mode,
both started near 50 °C. Raw rows in `base40.json` and `ceil45.json`.

| | ceiling 40 W | ceiling 45 W |
|---|---|---|
| Package W, mean / peak | 39.9 / 40.0 | 41.7 / 44.0 |
| Input W, mean / peak | 56.1 / 57.7 | 58.1 / 61.3 |
| Busy-core clock, mean | 3467 MHz | 3500 MHz |
| Power-limited samples | 18 % | 0 % |
| stress-ng bogo ops/s | 4993 | 5138 (+2.9 %) |
| Peak package °C | 88 at 51 s | 93 at 51 s (90 at end) |
| Fans max | 4511 rpm | 5510 rpm |
| Cuts / emergencies | 0 / 0 | 0 / 0 |

## Finding

At 45 W the CPU never hit the power limit: all 4 cores sat at 3.5 GHz, the
4-core turbo ratio (MSR 0x1AD = 0x23242527 → 39/37/36/35 for 1/2/3/4 active
cores). Package power for that is ~42–44 W at these temperatures, so a 45 W
ceiling is effectively uncapped for CPU-only work. 40 W costs about 1 % clock
and 3 % throughput on all-core loads, and less on lightly threaded ones. The
2-thread `llama-server` OCR job seen this morning was power-limited in only
5.5 % of its samples, so the cap barely affected it.

## 50 W (owner asked, same session)

A probe (`probe.py`, 12 s per load at the 45 W ceiling) looked for loads that
still hit 45 W: only `fft` and `vecwide` did (25 % power-limited, ~3480 MHz).
`matrixprod`, `fma`, `vecfp` and `matrix` sat at 3.5 GHz under 45 W.

A temporary build with `HARD_MAX_W = 50` and the ceiling range raised to 50
was deployed for the test only (bands unchanged), then the normal build was
reinstalled. 60 s runs, guard 72 W input / 95 °C:

| | fft @ 45 W | fft @ 50 W | matrixprod @ 50 W |
|---|---|---|---|
| Package W, mean / peak | 44.9 / 45.1 | 47.4 / 48.2 | 42.3 / 45.1 |
| Input W, mean / peak | 63.0 / 63.7 | 65.6 / 67.1 | 58.9 / 61.6 |
| Clock, mean | 3460 MHz | 3500 MHz | 3500 MHz |
| Power-limited | 26 % | 0 % | 0 % |
| Reached 95 °C | at 59.3 s | at 28.6 s | no (92 peak) |
| bogo ops/s | 7128 | 7401 (first 29 s only, cooler) | 5120 (45 W: 5138) |
| Cuts / emergencies | 0 / 0 | 0 / 0 | 0 / 0 |

Even the heaviest load stays at the 3.5 GHz turbo ratio with 48.2 W, so 50 W is
never used in full. Going from 45 W to 50 W buys about 1 % clock on the heaviest
vector loads only, halves the time to 95 °C, and brings the peak input to 3 W
under the 70 W shed band. 50 W is not worth it. The useful choice is 40 W
or 45 W.

Production was restored to the normal build (hard max 45 W) with the ceiling at
40 W, pending the owner's call.

## 50 W, 3-minute fft (owner asked after questioning the verdict above)

The 95 °C stop above was this test's own guard, not a hardware event. Rerun of
`probe.py 50 fft 180 99` (temporary 50 W build again, guards 72 W input /
99 °C), raw rows in `probe-fft-50w-180s.json`:

| t | 5 s | 15 s | 25 s | 35 s | 41 s | 47 s | 53.6 s |
|---|---|---|---|---|---|---|---|
| Package °C | 83 | 89 | 92 | 95 | 97 | 98 | 99 → stopped |
| Package W | 46.3 | 47.2 | 48.0 | 48.1 | 48.5 | 48.8 | 48.6 (peak 49.0) |
| Input W | 63.1 | 64.7 | 65.9 | 67.1 | 67.1 | 67.6 | 67.9 (peak 68.4) |

Clock held at 3.5 GHz throughout; MSR thermal and PROCHOT flags never set; the
throttle counters did not move; no cuts or emergencies; fans at the 5500 rpm
floor top from 20 s. The temperature had not levelled off: about 1 °C every
4–6 s at the end. TjMax is 100 °C with a TCC offset of 0 (MSR 0x1A2 = 0x640000),
so the CPU would have started throttling itself seconds later. Package power
for the same 3.5 GHz rose from 46 to 49 W as the die heated (leakage), and input
rose with it to 68.4 W, 1.6 W under the shed band.

Reading: at 50 W the heaviest vector loads run at the CPU's thermal limit
instead of the power limit, with input ~68 W at the end. Nothing unsafe
happened; what remains unobserved is steady state at 100 °C (the CPU's own
TCC throttling) and whether input then crosses the 70 W shed band.
Production restored to the normal build, ceiling 40 W.

## 50 W, 3-minute fft, no temperature stop (owner asked)

`probe.py 50 fft 180 101` (temporary 50 W build, input guard 72 W only), load
15:45:00–15:48:00 UTC; raw rows `probe-fft-50w-180s-noguard.json`, daemon-side
flags from the samples log via `win.py 15:45:00 15:48:10`.

| Phase | Time | Package °C | Package W | Input W | What happened |
|---|---|---|---|---|---|
| Heat-up | 0–55 s | 84 → 99 | 46–48.5 | 64–67.5 | 3.5 GHz, no flags |
| CPU self-throttle | 57 s – 2:10 | 100 | 48.7 → 45.8 | 68 → 64.7 | MSR thermal flag on and off, core throttle counter +7–9k per 5 s, clocks 3.4–3.5 GHz; SMC took the left fan to its 5928 rpm maximum at ~1:35 |
| SMC PROCHOT | 2:14 – 3:00 | 100 | 38–48 | 57–61 | 4 samples with external PROCHOT asserted: all 8 CPUs at 800 MHz for about a second each |

Peak input 69.0 W (1 W under shed); 0 cuts, 0 emergencies, 0 fallbacks. Average
6147 bogo ops/s over 180 s against about 7090 for the first 54 s, so the last
two minutes averaged about 5700 (estimate), roughly 19 % below the cool first
minute.

Reading: with the fans at maximum, this machine dissipates about 46 W at 100 °C
(the CPU self-limited to 45.8–46.5 W there, ambient ~30 °C, about 1.5 °C/W).
50 W is therefore only reachable for about a minute. After that the CPU's own
throttle takes over, then the SMC's PROCHOT, which drops all cores to 800 MHz.
Steady-state estimate at full fans: 40 W ≈ 90 °C, 42 W ≈ 93 °C, 45 W ≈ 97–98 °C
(unverified for a sustained 45 W load).
Production restored to the normal build, ceiling 40 W.

## 45 W, 3-minute fft, no temperature stop (owner asked)

`probe.py 45 fft 180 101` on the normal build (ceiling 45 W), load
15:53:47–15:56:47 UTC; raw rows `probe-fft-45w-180s-fan5500.json`, flags via
`win.py 15:53:32 15:56:52`.

| Time | Package °C | Package W | Input W | Clock | What happened |
|---|---|---|---|---|---|
| 0–25 s | 78 → 91 | 44.9 | 50–63 | 3.47–3.5 GHz | power-limited, fans step up to 5500 |
| 25 s – 1:55 | 92 → 99 | 44.9 | 63–64 | 3.35–3.46 GHz | power-limited, no thermal flags |
| 1:55 – 3:00 | 99–100 | 44.9 | 63.6–64.0 | 3.34–3.46 GHz | light CPU self-throttle (core counter +600 to +3500 per 5 s, against +7000 to +9000 at 50 W); SMC took the left fan to 5928 rpm at ~2:10 |

No external PROCHOT, no 800 MHz drops, input peak 64.4 W, 0 cuts. 7082 bogo
ops/s over 180 s, against 6147 for the same 3 minutes at 50 W (+15 %) and 7128
for the first minute at 45 W, so 45 W holds its throughput where 50 W loses it.
It is at the thermal edge on this heaviest load: 100 °C after ~2 minutes with
both fans at their limit; whether a run of 10 minutes or more stays clear of
SMC PROCHOT is not shown. Ceiling set back to 40 W after the run.

## 45 W live, fan top step 5500 → 5900 rpm, 3-minute fft again (owner asked)

`turbomacctl config fan.t3_rpm=5900 fan.max_rpm=5900 governor.ceiling_w=45`
(5900 is the config's upper bound; left fan hardware max 5927, the right fan
stops at its own 5489). Same `probe.py 45 fft 180 101`, load 16:19:44–16:22:44
UTC, raw rows `probe-fft-45w-180s-fan5900.json`.

| Reached | fan top 5500 (15:53) | fan top 5900 (16:19) |
|---|---|---|
| 91 °C | ~25 s | ~15 s |
| 95 °C | ~55 s | ~35 s |
| 97 °C | ~70 s | ~60 s |
| 99 °C | ~1:40 | ~1:35 |
| 100 °C | ~2:24 | ~1:50 |
| Left fan to 5928 (SMC) | ~2:20 | ~2:05 |
| Core throttle counter, last 5 s | +3500 | +10800 |
| External PROCHOT | none | none |
| Input W, mean / peak | 63.4 / 64.4 | 63.6 / 64.7 |
| bogo ops/s | 7082 | 5692 (not comparable, see below) |

The second run was not a clean comparison: it started right after ~5 minutes of
production load at 78–84 °C (heat-soaked chassis, package back at 60 °C only),
and a production `python` process used about 0.9 of a core throughout, which
also explains the lower bogo ops/s. Heat soak swamped the fan change; the faster
top step cannot be credited or blamed from this run.

Reading: at 45 W the heaviest sustained load reaches 100 °C within about 2
minutes either way, and on a warm chassis the CPU's own throttle at the end is
as busy as it was at 50 W. No external PROCHOT in either 3-minute run, no cuts,
input ~64 W. 45 W sustained is right at the machine's cooling capacity (~46 W at
100 °C), not below it. Left live: ceiling 45 W, fan t3/max 5900 rpm.
