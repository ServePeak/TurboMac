"""Replay the Sept 29 near-full-memory run (ended in a physical power-off at 78.3 W
input, firmware limits 100/125 W) through the dynamic governor, open loop."""
import json
import os
import unittest

import helpers
from turbomac import config
from turbomac.daemon import governor as gv
from turbomac.daemon.sampler import Window

FIXTURE = os.path.join(helpers.FIXTURES, "replay-20260929.jsonl")


def load_rows():
    with open(FIXTURE) as fh:
        rows = [json.loads(line) for line in fh if line.strip()]
    return [r for r in rows if "t" in r]


def replay(rows, c):
    inp, acpw = Window(12.0), Window(12.0)
    t0 = rows[0]["t"]
    s = gv.GovState(state=gv.NORMAL, pl1_w=40.0, pl2_w=40.0, since=t0, started=t0, last_valid=t0,
                    last_step=t0, last_write=t0)
    trace, pending = [], False
    for r in rows:
        now = r["t"]
        inp.push(now, r["input_w"])
        acpw.push(now, r["acpw_w"])
        sample = {"pdtr_w": r["input_w"], "input_mean_1s": inp.mean(now, 1.0), "acpw_w": acpw.median(now, 10.0),
                  "readback": {"ok": True, "reason": "replay"} if pending else None}
        pl1_before = s.pl1_w
        s, acts = gv.step(s, sample, c, now)
        pending = any(a["op"] == "set_limits" for a in acts)
        trace.append({"row": r, "pl1_in_effect": pl1_before, "state": s.state, "pl1": s.pl1_w, "acts": acts})
    return s, trace


class ReplayTest(unittest.TestCase):
    def setUp(self):
        self.rows = load_rows()
        self.c = config.defaults()
        self.c["governor"]["mode"] = "dynamic"
        self.c["governor"]["ceiling_w"] = 40   # the fixture's assertions were written for a 40 W ceiling
        self.s, self.trace = replay(self.rows, self.c)

    def test_fixture_shape(self):
        self.assertLessEqual(len(self.rows), 200)
        self.assertTrue(all(r["acpw_w"] == 79.496 for r in self.rows))
        self.assertGreaterEqual(max(r["input_w"] for r in self.rows), 78.0)

    def test_cuts_before_input_reaches_78w(self):
        first_78 = next(r["t"] for r in self.rows if r["input_w"] >= 78.0)
        cuts = [x["row"]["t"] for x in self.trace for a in x["acts"] if a["op"] == "event" and a["type"] == "cut"]
        self.assertTrue(cuts, "governor never cut")
        self.assertLess(cuts[0], first_78)
        # It reacts within ~1 s of the 1 s mean first crossing the shed band (69.96 W).
        shed = 79.496 * self.c["governor"]["shed_ratio"]
        first_shed = next(x["row"]["t"] for x in self.trace if x["row"]["input_w"] >= shed)
        self.assertLess(cuts[0] - first_shed, 1.5)
        # The emergency band (74.7 W) drops straight to the floor.
        first_emerg = next(r["t"] for r in self.rows if r["input_w"] >= 79.496 * 0.94)
        at_emerg = next(x for x in self.trace if x["row"]["t"] == first_emerg)
        self.assertEqual((at_emerg["state"], at_emerg["pl1"]), (gv.EMERGENCY, 10.0))
        self.assertTrue(all(x["pl1"] <= 10.0 for x in self.trace if first_emerg <= x["row"]["t"] < first_78 + 0.01))

    def test_modeled_input_under_commanded_cap_stays_below_78w(self):
        a, b = gv.INPUT_MODEL
        for x in self.trace:
            r, pl1 = x["row"], x["pl1_in_effect"]
            if r.get("pkg_w") is None or pl1 is None:
                continue
            modeled = r["input_w"] - b * max(0.0, r["pkg_w"] - pl1)
            self.assertLess(modeled, 78.0, r)

    def test_recovers_after_load_ends(self):
        cool = [x for x in self.trace if x["row"]["src"] == "run3-cooldown"]
        self.assertEqual(cool[0]["state"], gv.EMERGENCY)
        trim_at = next(x["row"]["t"] for x in cool if x["state"] == gv.TRIM)
        self.assertGreaterEqual(trim_at - cool[0]["row"]["t"], 4.5)
        ups = [a for x in cool for a in x["acts"] if a["op"] == "event" and a["type"] == "recover"]
        self.assertGreaterEqual(len(ups), 3)
        self.assertGreater(self.s.pl1_w, 10.0)
        self.assertLessEqual(max(x["pl1"] for x in self.trace), 40.0)
        self.assertEqual(self.s.counters["fallbacks"], 0)


if __name__ == "__main__":
    unittest.main()
