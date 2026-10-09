import unittest

import helpers  # noqa: F401
from turbomac import config
from turbomac.daemon import governor as gv
from turbomac.daemon.governor import EMERGENCY, FALLBACK, NORMAL, OFF, STATIC, TRIM, GovState

ACPW = 79.496  # shed 69.96, emergency 74.73, recover 62.01


def cfg(mode="dynamic", **over):
    c = config.defaults()
    c["governor"]["mode"] = mode
    c["governor"].update(over)
    return c


def smp(pdtr, mean=None, acpw=ACPW, readback=None):
    return {"pdtr_w": pdtr, "input_mean_1s": pdtr if mean is None else mean, "acpw_w": acpw, "readback": readback}


def writes(actions):
    return [(a["pl1_w"], a["pl2_w"]) for a in actions if a["op"] == "set_limits"]


def events(actions, typ):
    return [a for a in actions if a["op"] == "event" and a["type"] == typ]


def at(state=NORMAL, pl1=40.0, now=100.0, **kw):
    return GovState(state=state, pl1_w=pl1, pl2_w=pl1, since=now, started=0.0, last_valid=now,
                    last_step=now, last_write=now, **kw)


class GovernorTest(unittest.TestCase):
    def run_steps(self, s, c, t0, t1, sample, dt=0.25):
        out, t = [], t0
        while t <= t1 + 1e-9:
            s, acts = gv.step(s, sample, c, t)
            out.append((t, s, acts))
            t += dt
        return s, out

    def test_off_mode_never_writes(self):
        s = gv.initial(0.0)
        for i in range(40):
            s, acts = gv.step(s, smp(78.0), cfg("off"), i * 0.25)
            self.assertEqual(writes(acts), [])
        self.assertEqual(s.state, OFF)

    def test_static_mode_writes_and_reasserts_every_10s(self):
        c = cfg("static")
        s, acts = gv.step(gv.initial(0.0), smp(30.0), c, 0.0)
        self.assertEqual(s.state, STATIC)
        self.assertEqual(writes(acts), [(35.0, 35.0)])
        s, out = self.run_steps(s, c, 0.25, 9.75, smp(30.0))
        self.assertEqual(sum(len(writes(a)) for _, _, a in out), 0)
        s, acts = gv.step(s, smp(30.0), c, 10.0)
        self.assertEqual(writes(acts), [(35.0, 35.0)])
        self.assertEqual(acts[-1]["reason"], "reassert")
        # Static ignores input entirely, even emergency-level input.
        s, acts = gv.step(s, smp(78.0), c, 10.25)
        self.assertEqual((s.state, writes(acts)), (STATIC, []))

    def test_dynamic_starts_at_static_cap_and_recovers_slowly_to_ceiling(self):
        c = cfg()
        s, acts = gv.step(gv.initial(0.0), smp(40.0), c, 0.0)
        self.assertEqual((s.state, writes(acts)), (TRIM, [(35.0, 35.0)]))
        s, out = self.run_steps(s, c, 0.25, 20.0, smp(40.0))
        ups = [(t, w) for t, _, a in out for w in writes(a)]
        self.assertEqual([w[0] for _, w in ups], [36.0, 37.0, 38.0, 39.0, 40.0])
        gaps = [b[0] - a[0] for a, b in zip(ups, ups[1:])]
        self.assertTrue(all(g >= 3.0 - 1e-9 for g in gaps), gaps)
        self.assertEqual(s.state, NORMAL)
        self.assertEqual(s.counters["recoveries"], 5)

    def test_shed_cuts_on_1s_mean_and_rate_limits_to_one_per_half_second(self):
        c = cfg()
        s, acts = gv.step(at(), smp(72.0, mean=71.0), c, 100.0)
        self.assertEqual((s.state, writes(acts)), (TRIM, [(35.0, 35.0)]))
        self.assertEqual(len(events(acts, "cut")), 1)
        s, acts = gv.step(s, smp(72.0, mean=71.0), c, 100.25)
        self.assertEqual(writes(acts), [])
        s, acts = gv.step(s, smp(72.0, mean=71.0), c, 100.5)
        self.assertEqual(writes(acts), [(30.0, 30.0)])
        self.assertEqual(s.counters["cuts"], 2)

    def test_latest_spike_alone_does_not_cut_when_mean_is_below_shed(self):
        s, acts = gv.step(at(), smp(72.0, mean=65.0), cfg(), 100.0)
        self.assertEqual((s.state, writes(acts)), (NORMAL, []))  # held at ceiling: between recover and shed

    def test_cuts_stop_at_floor(self):
        c = cfg()
        s, out = self.run_steps(at(), c, 100.0, 110.0, smp(72.0, mean=71.0))
        pl1s = [w[0] for _, _, a in out for w in writes(a)]
        self.assertEqual(pl1s[:7], [35.0, 30.0, 25.0, 20.0, 15.0, 10.0, 10.0][: len(pl1s[:7])])
        self.assertEqual(min(pl1s), 10.0)
        self.assertEqual(s.pl1_w, 10.0)

    def test_emergency_on_latest_pdtr_is_immediate_and_ignores_rate_limit(self):
        c = cfg()
        s, _ = gv.step(at(), smp(72.0, mean=71.0), c, 100.0)        # cut to 35
        s, acts = gv.step(s, smp(75.0, mean=66.0), c, 100.1)          # 0.1 s later
        self.assertEqual(s.state, EMERGENCY)
        self.assertEqual(writes(acts), [(10.0, 10.0)])
        self.assertEqual(s.counters["emergencies"], 1)
        s, acts = gv.step(s, smp(76.0), c, 100.35)
        self.assertEqual((writes(acts), s.counters["emergencies"]), ([], 1))

    def test_emergency_holds_above_shed_and_leaves_to_trim_after_5s_below(self):
        c = cfg()
        s, _ = gv.step(at(), smp(75.0), c, 100.0)
        s, out = self.run_steps(s, c, 100.25, 109.75, smp(71.0, mean=71.0))  # above shed: hold
        self.assertEqual(s.state, EMERGENCY)
        self.assertEqual(sum(len(writes(a)) for _, _, a in out), 0)
        s, _ = gv.step(s, smp(40.0), c, 111.0)                        # first sample below shed
        s, _ = gv.step(s, smp(40.0), c, 115.9)
        self.assertEqual(s.state, EMERGENCY)
        s, acts = gv.step(s, smp(40.0), c, 116.0)
        self.assertEqual((s.state, writes(acts)), (TRIM, []))
        s, out = self.run_steps(s, c, 116.25, 122.0, smp(40.0))
        ups = [(t, w[0]) for t, _, a in out for w in writes(a)]
        self.assertEqual(ups[0][1], 11.0)
        self.assertGreaterEqual(ups[0][0], 119.0)

    def test_stale_smc_goes_to_fallback_then_resumes_from_static(self):
        c = cfg()
        s, out = self.run_steps(at(), c, 100.25, 101.75, smp(None, mean=None, acpw=None))
        self.assertEqual(s.state, NORMAL)
        s, acts = gv.step(s, smp(None, mean=None, acpw=None), c, 102.0)
        self.assertEqual((s.state, writes(acts)), (FALLBACK, [(35.0, 35.0)]))
        self.assertEqual(s.counters["fallbacks"], 1)
        s, out = self.run_steps(s, c, 102.25, 105.0, smp(None, mean=None, acpw=None))
        self.assertEqual(sum(len(writes(a)) for _, _, a in out), 0)
        s, acts = gv.step(s, smp(40.0), c, 105.25)
        self.assertEqual((s.state, writes(acts)), (TRIM, [(35.0, 35.0)]))

    def test_no_valid_sample_since_start_is_stale(self):
        s = gv.initial(0.0)
        s, _ = gv.step(s, smp(None, acpw=None), cfg(), 1.0)
        self.assertNotEqual(s.state, FALLBACK)
        s, acts = gv.step(s, smp(None, acpw=None), cfg(), 2.0)
        self.assertEqual((s.state, writes(acts)), (FALLBACK, [(35.0, 35.0)]))

    def test_readback_mismatch_falls_back_with_exponential_backoff(self):
        c = cfg()
        s, t, pending, backoffs, writes_log = at(state=TRIM, pl1=35.0), 100.0, False, [], []
        bad = {"ok": False, "reason": "MSR 0x610 reads PL1 100 W"}
        s, _ = gv.step(s, smp(40.0, readback=bad), c, t)
        self.assertEqual(s.state, FALLBACK)
        backoffs.append(s.backoff_s)
        pending = True
        while t < 140.0:
            t += 0.25
            prev_retry = s.retry_at
            s, acts = gv.step(s, smp(40.0, readback=bad if pending else None), c, t)
            pending = bool(writes(acts))
            writes_log += [(t, w) for w in writes(acts)]
            if s.backoff_s != backoffs[-1] or (s.retry_at != prev_retry and s.retry_at and s.backoff_s == 8.0):
                backoffs.append(s.backoff_s)
        self.assertEqual(backoffs[:6], [1.0, 2.0, 4.0, 8.0, 8.0, 8.0])
        self.assertLessEqual(max(backoffs), gv.BACKOFF_MAX_S)
        self.assertTrue(all(w[0] <= 35.0 for _, w in writes_log))
        self.assertLess(len(writes_log), 25)  # writes only on fallback entry and retries, not every tick
        # Hardware recovers: the next retry's write reads back fine and control resumes.
        good = {"ok": True, "reason": "ok"}
        while s.state == FALLBACK:
            t += 0.25
            s, acts = gv.step(s, smp(40.0, readback=good if pending else None), c, t)
            pending = bool(writes(acts))
        s, _ = gv.step(s, smp(40.0, readback=good), c, t + 0.25)
        self.assertEqual((s.state, s.backoff_s), (TRIM, 0.0))

    def test_fallback_never_raises_the_limit(self):
        c, none = cfg(), smp(None, mean=None, acpw=None)
        s, _ = gv.step(at(), smp(75.0), c, 100.0)                     # EMERGENCY at the floor
        s, _ = self.run_steps(s, c, 100.25, 101.75, none)
        s, acts = gv.step(s, none, c, 102.25)                          # stale: FALLBACK
        self.assertEqual((s.state, writes(acts)), (FALLBACK, [(10.0, 10.0)]))
        s, acts = gv.step(s, smp(40.0), c, 102.5)                      # resumes at the floor, not 35
        self.assertEqual((s.state, s.pl1_w), (TRIM, 10.0))
        s, out = self.run_steps(s, c, 102.75, 106.0, smp(40.0))
        self.assertLessEqual(max([w[0] for _, _, a in out for w in writes(a)] or [10.0]), 11.0)
        bad = {"ok": False, "reason": "mismatch"}
        s, acts = gv.step(at(state=TRIM, pl1=20.0), smp(40.0, readback=bad), c, 100.0)
        self.assertEqual((s.state, writes(acts)), (FALLBACK, [(20.0, 20.0)]))

    def test_emergency_still_cuts_while_waiting_to_retry(self):
        c, bad = cfg(), {"ok": False, "reason": "mismatch"}
        s, acts = gv.step(at(state=TRIM, pl1=35.0), smp(40.0, readback=bad), c, 100.0)
        self.assertEqual((s.state, s.retry_at), (FALLBACK, 101.0))
        s, acts = gv.step(s, smp(76.0), c, 100.25)
        self.assertEqual((s.state, writes(acts)), (FALLBACK, [(10.0, 10.0)]))
        self.assertEqual(s.counters["emergencies"], 1)
        s, acts = gv.step(s, smp(76.0), c, 100.5)                      # already at the floor
        self.assertEqual((writes(acts), s.counters["emergencies"]), ([], 1))
        s, acts = gv.step(s, smp(40.0), c, 101.0)                      # retry resumes at the floor
        self.assertEqual(writes(acts), [(10.0, 10.0)])

    def test_never_above_hard_max_or_firmware(self):
        c = cfg(static_cap_w=45, ceiling_w=45, burst_w=5)
        s = gv.initial(0.0, firmware={"pl1_w": 40.0, "pl2_w": 42.0})
        s, acts = gv.step(s, smp(30.0), c, 0.0)
        self.assertEqual(writes(acts), [(40.0, 42.0)])
        bogus = cfg(static_cap_w=80, ceiling_w=90, burst_w=20)   # bypasses validation on purpose
        s, acts = gv.step(gv.initial(0.0), smp(30.0), bogus, 0.0)
        self.assertEqual(writes(acts), [(45.0, 45.0)])
        s, out = self.run_steps(s, bogus, 0.25, 60.0, smp(30.0))
        self.assertTrue(all(p1 <= 45.0 and p2 <= 45.0 for _, _, a in out for p1, p2 in writes(a)))
        s, acts = gv.step(gv.initial(0.0), smp(30.0), cfg("static", static_cap_w=99), 0.0)
        self.assertEqual(writes(acts), [(45.0, 45.0)])

    def test_burst_applies_in_trim_but_not_emergency(self):
        c = cfg(burst_w=3)
        s, acts = gv.step(gv.initial(0.0), smp(40.0), c, 0.0)
        self.assertEqual(writes(acts), [(35.0, 38.0)])
        s, acts = gv.step(s, smp(76.0), c, 0.25)
        self.assertEqual(writes(acts), [(10.0, 10.0)])

    def test_mode_switches_and_time_in_state(self):
        s, _ = gv.step(gv.initial(0.0), smp(40.0), cfg(), 0.0)
        s, _ = gv.step(s, smp(40.0), cfg(), 2.0)
        s, acts = gv.step(s, smp(40.0), cfg("static"), 4.0)
        self.assertEqual((s.state, writes(acts)), (STATIC, [(35.0, 35.0)]))
        s, acts = gv.step(s, smp(40.0), cfg("off"), 5.0)
        self.assertEqual((s.state, writes(acts)), (OFF, [(35.0, 35.0)]))   # entering OFF: static cap once
        s, acts = gv.step(s, smp(78.0), cfg("off"), 5.5)
        self.assertEqual(writes(acts), [])
        s, acts = gv.step(s, smp(40.0), cfg("static"), 6.0)
        self.assertEqual(writes(acts), [(35.0, 35.0)])   # re-entering from OFF always writes
        self.assertAlmostEqual(s.time_in_state[TRIM], 4.0)
        self.assertAlmostEqual(s.time_in_state[STATIC], 1.0)
        self.assertEqual(gv.summary(s)["state"], STATIC)

    def test_input_state_is_not_mutated(self):
        s0 = at()
        before = (s0.pl1_w, dict(s0.counters), s0.state)
        gv.step(s0, smp(75.0), cfg(), 100.0)
        self.assertEqual((s0.pl1_w, dict(s0.counters), s0.state), before)


if __name__ == "__main__":
    unittest.main()
