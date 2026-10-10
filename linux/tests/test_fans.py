"""Fan floor: the pure decide() rules, FanController on a fake applesmc tree, and the daemon wiring."""
import io
import json
import os
import time
import unittest
from unittest import mock

import helpers
from turbomac import config
from turbomac.daemon.fans import FanController, FanState, SysfsFanIO, decide
from turbomac.daemon.logger import EventLog
from turbomac.daemon.paths import Paths

CFG = {**config.defaults()["fan"], "t2_c": 86, "t3_c": 92}   # explicit ladder: decide() tests never depend on defaults
DEFAULT = config.defaults()["fan"]                            # the shipped config: controller and daemon tests


def run(series, cfg=CFG, state=None):
    """Feed (t, temp_c, pkg_w) rows through decide(); return [(t, target, reason)]."""
    state, out = state or FanState(), []
    for t, temp, pkg in series:
        state, target, reason = decide(state, temp, pkg, cfg, t)
        out.append((t, target, reason))
    return out


class DecideTest(unittest.TestCase):
    def test_power_rule_engages_immediately_and_releases_after_20_s_below_20_w(self):
        self.assertEqual(run([(0, None, 24.9)])[-1][1], 0)
        t, target, reason = run([(0, None, 25.0)])[-1]
        self.assertEqual((target, reason), (3500, "power 25 W ≥ 25 W"))
        rows = run([(0, 60, 31.0)] + [(t, 60, 19.0) for t in range(1, 30)])
        held = [t for t, target, _ in rows if target == 3500]
        self.assertEqual(held[-1], 20)                      # below 20 W since t=1: released at t=21
        self.assertEqual(rows[21][1:], (0, "below thresholds"))
        self.assertIn("cooling: 3500 rpm until power < 20 W for 20 s", rows[5][2])
        rows = run([(0, 60, 31.0)] + [(t, 60, 22.0) for t in range(1, 120)])   # inside the 20..25 W band
        self.assertTrue(all(target == 3500 for _, target, _ in rows))
        rows = run([(0, 60, 31.0)] + [(t, 60, 21.0 if t == 15 else 19.0) for t in range(1, 40)])
        self.assertEqual([t for t, target, _ in rows if target == 3500][-1], 35)  # a 21 W blip at 15 restarts it at 16

    def test_ladder_steps_up_immediately(self):
        rows = run([(0, 77.9, 5.0), (1, 78.0, 5.0), (2, 86.0, 5.0), (3, 92.0, 5.0)])
        self.assertEqual([r[1] for r in rows], [0, 3500, 4500, 5900])
        self.assertEqual(rows[2][2], "pkg 86 °C ≥ 86 °C")
        self.assertEqual(run([(0, 95.0, 5.0)])[-1][1], 5900)            # straight to the top step

    def test_ladder_steps_down_one_step_per_interval_below_step_minus_6(self):
        rows = run([(0, 93.0, 5.0)] + [(t, 87.0, 5.0) for t in range(1, 100)])
        self.assertTrue(all(r[1] == 5900 for r in rows))                 # 87 >= 92 - 6: held
        rows = run([(0, 93.0, 5.0)] + [(t, 60.0, 5.0) for t in range(1, 80)])
        steps = {t: target for t, target, _ in rows}
        self.assertEqual((steps[20], steps[21]), (5900, 4500))           # below 86 since t=1
        self.assertEqual((steps[40], steps[41]), (4500, 3500))           # one step per interval
        self.assertEqual((steps[60], steps[61]), (3500, 0))
        self.assertIn("cooling: 4500 rpm until pkg < 80 °C for 20 s", rows[30][2])
        rows = run([(0, 93.0, 5.0)] + [(t, 85.0, 5.0) for t in range(1, 100)])
        self.assertEqual(rows[-1][1], 4500)                              # 85 >= 86 - 6: step 2 holds
        rows = run([(0, 87.0, 5.0)] + [(t, 79.0 if t != 10 else 81.0, 5.0) for t in range(1, 40)])
        self.assertEqual([t for t, target, _ in rows if target == 4500][-1], 30)  # 81 °C blip restarts

    def test_max_of_both_rules_and_max_rpm(self):
        self.assertEqual(run([(0, 87.0, 31.0)])[-1][1:], (4500, "pkg 87 °C ≥ 86 °C"))
        self.assertEqual(run([(0, 70.0, 31.0)])[-1][1:], (3500, "power 31 W ≥ 25 W"))
        cfg = {**CFG, "power_floor_rpm": 5000}
        self.assertEqual(run([(0, 87.0, 31.0)], cfg)[-1][1:], (5000, "power 31 W ≥ 25 W"))
        cfg = {**CFG, "t3_rpm": 5900, "max_rpm": 5000}
        self.assertEqual(run([(0, 95.0, 5.0)], cfg)[-1][1:], (5000, "pkg 95 °C ≥ 92 °C (max 5000 rpm)"))

    def test_missing_inputs_contribute_nothing(self):
        self.assertEqual(run([(0, None, 30.0)])[-1][1], 3500)
        self.assertEqual(run([(0, 90.0, None)])[-1][1], 4500)
        self.assertEqual(run([(0, None, None)])[-1][1:], (0, "no temperature or power data"))
        self.assertEqual(run([(0, float("nan"), None)])[-1][1], 0)
        rows = run([(0, 93.0, 31.0), (1, None, 31.0), (2, 93.0, None), (3, None, None)])
        self.assertEqual([r[1] for r in rows], [5900, 3500, 5900, 0])


class ControllerTest(unittest.TestCase):
    def setUp(self):
        self.fs = helpers.FakeSys()
        self.base = helpers.add_applesmc_fans(self.fs.root)
        self.paths = Paths(sysroot=self.fs.root)
        self.events = EventLog(stream=io.StringIO())
        self.now = 1000.0

    def tearDown(self):
        self.fs.cleanup()

    def f(self, n, attr):
        return helpers.read(os.path.join(self.base, f"fan{n}_{attr}"))

    def ctl(self):
        c = FanController(self.paths, self.events, clock=lambda: self.now)
        self.writes = []
        real = c.io.write_min

        def counting(n, rpm):
            self.writes.append((n, rpm))
            return real(n, rpm)
        c.io.write_min = counting
        return c

    def types(self, typ):
        return [e for e in self.events.recent(500) if e["type"] == typ]

    def test_defaults_captured_once(self):
        c = self.ctl()
        with open(self.paths.fan_defaults) as fh:
            saved = json.load(fh)["fans"]
        self.assertEqual((saved["1"]["min"], saved["2"]["min"], saved["2"]["label"]), (2160, 2000, "Right side"))
        self.assertEqual(c.defaults, {1: 2160, 2: 2000})
        c.apply(4500, 5500)
        c2 = self.ctl()                                   # restart with a floor still applied
        self.assertEqual(c2.defaults, {1: 2160, 2: 2000})  # from the file, not the live 4500
        self.assertEqual(c2.written, {1: 4500, 2: 4500})
        self.assertEqual(len(self.types("fan_defaults")), 1)

    def test_floor_already_applied_is_not_captured(self):
        helpers.write(os.path.join(self.base, "fan1_min"), "3500\n")
        c = self.ctl()
        self.assertFalse(os.path.exists(self.paths.fan_defaults))
        self.assertEqual(c.defaults, {1: 2160, 2: 2000})     # known hardware minimum for fan1, live 2000 for fan2
        self.assertIn("not saved", self.types("fan_defaults_warning")[0]["message"])
        c.restore()
        self.assertEqual((self.f(1, "min"), self.f(2, "min")), ("2160", "2000"))
        self.assertTrue(self.ctl() and os.path.exists(self.paths.fan_defaults))  # plausible now: captured

    def test_apply_clamps_per_fan_writes_only_on_change_and_reads_back(self):
        c = self.ctl()
        self.assertFalse(c.apply(0, 5500))                  # hardware minimum is already there: no write
        self.assertEqual(self.writes, [])
        self.assertTrue(c.apply(5800, 5900, "pkg 95 °C ≥ 92 °C", 95.0, 30.0))
        self.assertEqual((self.f(1, "min"), self.f(2, "min")), ("5800", "5489"))   # right fan max 5489
        self.assertEqual(self.writes, [(1, 5800), (2, 5489)])
        self.assertFalse(c.apply(5800, 5900))
        self.assertEqual(len(self.writes), 2)
        c.apply(6000, 5500)
        self.assertEqual((self.f(1, "min"), self.f(2, "min")), ("5500", "5489"))   # max_rpm, fan2 unchanged
        self.assertEqual(self.writes[2:], [(1, 5500)])
        c.apply(1000, 5500)
        self.assertEqual((self.f(1, "min"), self.f(2, "min")), ("2160", "2000"))   # never below hw minimum
        ev = self.types("fan_floor")
        self.assertEqual(len(ev), 3)
        self.assertEqual((ev[0]["fans"], ev[0]["temp_c"], ev[0]["reason"]),
                         ({"Left side": 5800, "Right side": 5489}, 95.0, "pkg 95 °C ≥ 92 °C"))
        st = c.status()
        self.assertEqual(st["fans"][1], {"label": "Right side", "rpm": 1999, "min_written": 2000, "hw_min": 2000,
                                         "max": 5489})
        self.assertEqual(st["last_change_ts"], self.now)
        for n in (1, 2):                                   # never manual control
            self.assertEqual(self.f(n, "manual"), "0")
            self.assertNotEqual(self.f(n, "output"), "5800")

    def test_readback_mismatch_is_an_error_and_retried_after_30_s(self):
        c = self.ctl()
        real = c.io.read_int
        c.io.read_int = lambda n, attr: 2160 if (n, attr) == (1, "min") else real(n, attr)   # SMC ignores fan1
        c.apply(3500, 5500)
        err = self.types("fan_floor_error")
        self.assertEqual(len(err), 1)
        self.assertIn("fan1_min reads 2160, wanted 3500", err[0]["error"])
        self.assertEqual((c.written[1], c.written[2], c.status()["write_errors"]), (None, 3500, 1))
        c.apply(3500, 5500)
        self.now += 29
        c.apply(3500, 5500)
        self.assertEqual(len(self.types("fan_floor_error")), 1)
        self.now += 1
        c.apply(3500, 5500)
        self.assertEqual(len(self.types("fan_floor_error")), 2)
        with mock.patch.object(SysfsFanIO, "write_min", return_value="write fan1_min: Permission denied"):
            c2 = FanController(self.paths, self.events, clock=lambda: self.now)
            c2.apply(4500, 5500)
        self.assertEqual(c2.write_errors, 2)

    def test_restore_writes_defaults_back(self):
        c = self.ctl()
        self.assertTrue(c.restore())
        self.assertEqual(self.writes, [])                  # nothing to restore
        c.apply(4500, 5500)
        self.assertTrue(c.restore())
        self.assertEqual((self.f(1, "min"), self.f(2, "min")), ("2160", "2000"))
        self.assertEqual(self.types("fan_restore")[0]["fans"], {"Left side": 2160, "Right side": 2000})

    def test_tick_follows_the_mode(self):
        c = self.ctl()
        c.start(DEFAULT)
        c.tick(0.0, 90.0, 10.0, DEFAULT)
        self.assertEqual((self.f(1, "min"), self.f(2, "min"), c.status()["target_rpm"]), ("5900", "5489", 5900))
        self.assertEqual(c.status()["reason"], "pkg 90 °C ≥ 90 °C")
        c.on_config({**DEFAULT, "mode": "auto"})           # restores immediately
        self.assertEqual((self.f(1, "min"), self.f(2, "min")), ("2160", "2000"))
        n = len(self.writes)
        c.tick(1.0, 95.0, 40.0, {**DEFAULT, "mode": "auto"})
        self.assertEqual((len(self.writes), c.status()["mode"]), (n, "auto"))   # auto never writes
        c.tick(2.0, 95.0, 40.0, DEFAULT)                   # back to floor (e.g. SIGHUP reload)
        self.assertEqual(self.f(1, "min"), "5900")
        self.assertEqual([e["mode"] for e in self.types("fan_mode")], ["auto", "floor"])
        helpers.write(os.path.join(self.base, "fan1_min"), "5000\n")   # left over by a crash
        c3 = self.ctl()
        c3.start({**DEFAULT, "mode": "auto"})              # auto at start clears it
        self.assertEqual(self.f(1, "min"), "2160")

    def test_no_applesmc_means_no_fans_and_no_writes(self):
        c = FanController(Paths(sysroot=self.fs.p("empty")), self.events)
        c.start(DEFAULT)
        c.tick(0.0, 99.0, 45.0, DEFAULT)
        self.assertEqual((c.status()["fans"], c.status()["target_rpm"], c.restore()), ([], 0, True))


class DaemonFanTest(unittest.TestCase):
    def setUp(self):
        self.fs = helpers.FakeSys()
        self.base = helpers.add_applesmc_fans(self.fs.root)
        self.temp = helpers.add_coretemp(self.fs.root, 90.0)
        self.paths = Paths(sysroot=self.fs.root)
        self.events = EventLog(stream=io.StringIO())

    def tearDown(self):
        self.fs.cleanup()

    def f(self, n):
        return helpers.read(os.path.join(self.base, f"fan{n}_min"))

    def test_slow_tick_runs_the_controller_and_shutdown_restores(self):
        d = helpers.make_daemon(self.paths, self.events)
        d.setup(start_server=False)
        d.gpu_thread.join(5)
        t = time.monotonic()
        d.tick(t, slow=False)
        self.assertEqual(self.f(1), "2160")                       # fast ticks never touch the fans
        d.tick(t + 0.25, slow=True)
        self.assertEqual((self.f(1), self.f(2)), ("5900", "5489"))   # right fan clamped to its max
        st = d.dispatch("status", {})
        self.assertEqual((st["fan"]["mode"], st["fan"]["target_rpm"], st["fan"]["reason"]),
                         ("floor", 5900, "pkg 90 °C ≥ 90 °C"))
        self.assertEqual(st["governor"]["state"], "STATIC")
        r = d.dispatch("config.set", {"fan.mode": "auto"})
        self.assertEqual(r["accepted"], {"fan.mode": "auto"})
        self.assertEqual(self.f(1), "2160")                       # restored immediately
        r = d.dispatch("config.set", {"fan.mode": "floor", "fan.t2_c": 91, "fan.t3_c": 95})
        self.assertEqual(r["rejected"], {})
        d.tick(t + 0.5, slow=False)
        self.assertEqual(self.f(1), "2160")
        d.tick(t + 1.25, slow=True)
        self.assertEqual(self.f(1), "3500")                       # 90 °C is now step 1 only
        with mock.patch.object(d.fanctl, "tick", side_effect=OSError("applesmc went away")):
            d.tick(t + 2.25, slow=True)
        self.assertEqual(d.last_step_ok, t + 2.25)                # the governor stepped anyway
        self.assertIn("fan_error", {e["type"] for e in self.events.recent(500)})
        self.assertTrue(d.logger.drain(5))
        files = os.listdir(self.paths.samples_dir)
        with open(os.path.join(self.paths.samples_dir, files[0])) as fh:
            rec = [json.loads(x) for x in fh][-1]
        self.assertEqual((rec["fan"]["target_rpm"], rec["fan"]["min_written"]), (3500, [3500, 3500]))
        d.shutdown()
        self.assertEqual((self.f(1), self.f(2)), ("2160", "2000"))
        self.assertIn("fan_restore", {e["type"] for e in self.events.recent(500)})


if __name__ == "__main__":
    unittest.main()
