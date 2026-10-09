"""turbomactop backends: status shape equals Daemon.build_status's, replay mapping, config round trips."""
import importlib.util
import io
import os
import tempfile
import threading
import time
import unittest

import helpers

HAVE_TEXTUAL = importlib.util.find_spec("textual") is not None
FIXTURE = os.path.join(helpers.ROOT, "fixtures", "samples-oldmac-20261009.jsonl.gz")
NESTED = ("latest", "derived", "governor", "limits", "gpu", "fan", "cpu")


def daemon_status():
    """A real build_status() from turbomacd on a fake sysroot (RAPL + amdgpu, no SMC)."""
    from turbomac.daemon.logger import EventLog
    from turbomac.daemon.paths import Paths
    fs = helpers.FakeSys()
    helpers.add_amdgpu(fs.root)
    d = helpers.make_daemon(Paths(sysroot=fs.root), EventLog(stream=io.StringIO()))
    try:
        d.setup(start_server=False)
        d.gpu_thread.join(5)
        t = time.monotonic()
        for i in range(8):
            helpers.write(os.path.join(fs.zone, "energy_uj"), f"{1_000_000 + (i + 1) * 5_000_000}\n")
            d.tick(t + i * 0.25, slow=i % 4 == 0)
        return d.dispatch("status", {})
    finally:
        d.shutdown()
        fs.cleanup()


@unittest.skipUnless(HAVE_TEXTUAL, "textual not installed (TUI venv)")
class ShapeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ref = daemon_status()

    def assert_shape(self, st, exact):
        self.assertEqual(set(st), set(self.ref), "top-level keys")
        for sec in NESTED:
            want, got = set(self.ref[sec] or {}), set(st[sec] or {})
            if exact:
                self.assertEqual(got, want, sec)
            else:
                self.assertLessEqual(want - {"mode"} if sec == "gpu" else want, got, sec)
        self.assertEqual(set(st["thresholds"]), {"shed_w", "emergency_w", "recover_w"})
        self.assertEqual(set(st["governor"]["counters"]), set(self.ref["governor"]["counters"]))
        fan_keys = {"label", "rpm", "min_written", "hw_min", "max"}
        self.assertTrue(st["fan"]["fans"] and all(set(f) == fan_keys for f in st["fan"]["fans"]))

    def test_mock_status_has_every_build_status_key(self):
        from turbomac.tui.mock import MockBackend
        self.assert_shape(MockBackend(seed=3, warmup_s=30).status(), exact=True)

    def test_replay_status_has_every_build_status_key(self):
        from turbomac.tui.replay import ReplayBackend
        r = ReplayBackend(FIXTURE)
        self.assert_shape(r.status(), exact=False)
        r.advance(120)
        self.assert_shape(r.status(), exact=False)


@unittest.skipUnless(HAVE_TEXTUAL, "textual not installed (TUI venv)")
class ReplayTest(unittest.TestCase):
    def setUp(self):
        from turbomac.tui.replay import ReplayBackend, load_records
        self.header, self.recs = load_records(FIXTURE)
        self.r = ReplayBackend(FIXTURE, speed=1.0)

    def test_mapping_of_the_first_record_tolerates_null_rapl_fields(self):
        rec, st = self.recs[0], self.r.status()
        self.assertEqual(self.header["kind"], "header")
        self.assertEqual(st["boot_id"], self.header["boot_id"])
        self.assertIsNone(rec["pkg_w"])
        self.assertIsNone(st["latest"]["package_w"])
        self.assertEqual(st["latest"]["pdtr_w"], rec["input_w"])
        self.assertEqual(st["derived"]["headroom_w"], rec["headroom_w"])
        self.assertEqual(st["derived"]["acpw_median_10s"], rec["acpw_w"])
        self.assertAlmostEqual(st["thresholds"]["shed_w"], 0.88 * rec["acpw_w"])
        self.assertEqual((st["governor"]["state"], st["governor"]["mode"]), ("STATIC", "static"))
        self.assertEqual(st["limits"]["sysfs"], rec["sysfs_limits"])
        self.assertEqual(st["fans"], rec["fans"])
        self.assertEqual(st["gpu"]["config_mode"], "auto")
        self.assertNotIn("fan", rec)                                  # logged before the fan floor existed
        self.assertEqual((st["fan"]["mode"], st["fan"]["target_rpm"]), ("auto", 0))
        self.assertEqual(st["fan"]["fans"][1]["hw_min"], rec["fans"][1]["min"])
        rec2 = dict(rec, fan={"mode": "floor", "target_rpm": 5500, "reason": "pkg 93 °C ≥ 92 °C",
                              "min_written": [5500, 5489], "hw_min": [2160, 2000], "write_errors": 0})
        fan = self.r.map_fan(rec2)
        self.assertEqual((fan["mode"], fan["target_rpm"], fan["fans"][1]["min_written"], fan["fans"][1]["label"]),
                         ("floor", 5500, 5489, rec["fans"][1]["label"]))

    def test_plays_at_speed_keeps_history_and_loops_forward(self):
        self.r.advance(300)
        st = self.r.status()
        self.assertEqual(st["ts"], self.recs[300]["ts"])
        hist = self.r.history(600)
        self.assertEqual(len(hist), 301)
        self.assertEqual(set(hist[0]), {"ts", "input_w", "pkg_w", "gpu_w", "pkg_c", "pl1_w", "state"})
        self.assertEqual(st["peaks"]["input_w"]["value"], max(r["input_max_w"] for r in self.recs[:301]))
        self.assertEqual(len(self.r.history(600, full=True)[0]), len(self.recs[0]))
        self.r.advance(len(self.recs))           # wraps around
        st2 = self.r.status()
        self.assertGreater(st2["ts"], st["ts"])
        self.assertEqual(len(self.r.history(600)), 600)
        self.assertIn("replay_loop", {e["type"] for e in self.r.events(1000)})

    def test_config_is_validated_reflected_but_not_persisted(self):
        res = self.r.config_set({"governor.shed_ratio": 0.85, "governor.ceiling_w": 50})
        self.assertEqual(res["accepted"], {"governor.shed_ratio": 0.85})
        self.assertIn("out of range", res["rejected"]["governor.ceiling_w"])
        self.assertFalse(res["persisted"])
        st = self.r.status()
        self.assertAlmostEqual(st["thresholds"]["shed_w"], 0.85 * self.recs[0]["acpw_w"])
        self.assertEqual(self.r.config_get()["config"]["governor"]["shed_ratio"], 0.85)
        self.assertEqual(st["config_digest"], res["digest"])
        res = self.r.config_set({"gpu.mode": "low"})
        self.assertEqual(res["accepted"]["gpu.mode"], "low")
        self.assertEqual(self.r.status()["gpu"]["config_mode"], "low")
        self.r.config_reset()
        self.assertEqual(self.r.config_get()["config"]["governor"]["shed_ratio"], 0.88)


@unittest.skipUnless(HAVE_TEXTUAL, "textual not installed (TUI venv)")
class MockTest(unittest.TestCase):
    def setUp(self):
        from turbomac.tui.mock import MockBackend
        self.Mock = MockBackend

    def test_same_seed_same_samples(self):
        a, b = self.Mock(seed=7, warmup_s=200), self.Mock(seed=7, warmup_s=200)
        self.assertEqual([r["input_w"] for r in a.history(600)], [r["input_w"] for r in b.history(600)])
        self.assertNotEqual([r["input_w"] for r in a.history(600)],
                            [r["input_w"] for r in self.Mock(seed=8, warmup_s=200).history(600)])

    def test_idle_and_burst_levels(self):
        m = self.Mock(seed=1, warmup_s=600)
        rows = m.history(600)
        idle = [r["input_w"] for r in rows if r["pkg_w"] is not None and r["pkg_w"] < 9]
        burst = [r for r in rows if r["pkg_w"] is not None and r["pkg_w"] > 34]
        self.assertTrue(19 < sum(idle) / len(idle) < 24)
        self.assertTrue(burst and max(r["input_w"] for r in burst) > 54)
        self.assertTrue(all(r["pl1_w"] == 35 and r["state"] == "STATIC" for r in rows))

    def test_dynamic_mode_cuts_when_a_gpu_burst_crosses_shed(self):
        m = self.Mock(seed=1, warmup_s=60)
        res = m.config_set({"governor.mode": "dynamic", "governor.ceiling_w": 40})
        self.assertEqual(set(res["accepted"]), {"governor.mode", "governor.ceiling_w"})
        self.assertTrue(res["persisted"])
        m.advance(105)                              # t = 165 s: 15 s into the first GPU-loaded burst
        types = [e["type"] for e in m.events(1000)]
        self.assertIn("recover", types)
        self.assertIn("cut", types)
        st = m.status()
        self.assertEqual((st["governor"]["mode"], st["governor"]["state"]), ("dynamic", "TRIM"))
        self.assertEqual(st["governor"]["pl1_w"], 35.0)
        self.assertGreaterEqual(st["governor"]["counters"]["cuts"], 1)
        self.assertEqual(m.config_get()["config"]["governor"]["mode"], "dynamic")
        r = m.config_reset()
        self.assertEqual(r["accepted"]["governor.mode"], "static")
        m.advance(1)
        self.assertEqual(m.status()["governor"]["state"], "STATIC")

    def test_fan_floor_ramps_the_fans_during_bursts(self):
        m = self.Mock(seed=1, warmup_s=25)                # idle, 5 s before the first burst
        st = m.status()
        self.assertEqual((st["fan"]["mode"], st["fan"]["target_rpm"]), ("floor", 0))
        self.assertEqual([f["rpm"] for f in st["fans"]], [2160, 2000])
        m.advance(25)                                     # 20 s into the burst (package at PL1 = 35 W)
        st = m.status()
        self.assertEqual(st["fan"]["target_rpm"], 3500)
        self.assertTrue(st["fan"]["reason"].startswith(("power", "pkg")), st["fan"]["reason"])
        self.assertTrue(all(f["rpm"] >= 3400 and f["min"] == 3500 for f in st["fans"]), st["fans"])
        self.assertIn("fan_floor", {e["type"] for e in m.events(1000)})
        self.assertEqual(m.history(5, full=True)[-1]["fan"]["target_rpm"], 3500)
        res = m.config_set({"fan.mode": "auto"})
        self.assertEqual(res["accepted"], {"fan.mode": "auto"})
        self.assertEqual(m.sim.fan_min, [2160, 2000])     # restored at once
        m.advance(10)
        st = m.status()
        self.assertEqual((st["fan"]["mode"], st["fan"]["target_rpm"], m.sim.fan_min), ("auto", 0, [2160, 2000]))
        self.assertIn("fan.t1_c", m.config_set({"fan.t1_c": 90})["rejected"])   # t1 must stay below t2 (84)

    def test_gpu_modes_change_what_is_reported(self):
        m = self.Mock(seed=2, warmup_s=10, gpu_delay_s=0)
        res = m.gpu_set("off")
        self.assertEqual((res["ok"], res["mode"]), (True, "off"))
        self.assertTrue(any("switcheroo OFF" in s for s in res["steps"]))
        m.advance(1)
        gpu = m.status()["gpu"]
        self.assertEqual((gpu["mode"], gpu["config_mode"], gpu["switcheroo"]["DIS"]), ("off", "off", " :Off"))
        self.assertNotIn("power_w", gpu)
        self.assertEqual(gpu["last_result"]["mode"], "off")
        res = m.gpu_set("low")
        self.assertEqual(res["steps"][0], "switcheroo ON")
        m.advance(1)
        self.assertEqual(m.status()["gpu"]["cap_w"], 10.0)
        from turbomac.tui.backend import BackendError
        with self.assertRaises(BackendError):
            m.gpu_set("max")
        self.assertIn("must be", m.config_set({"cpu.epp": "turbo"})["rejected"]["cpu.epp"])

    def test_incidents_events_and_smc_keys(self):
        from turbomac.tui.backend import BackendError
        m = self.Mock(seed=1, warmup_s=30)
        incs = m.incidents()
        self.assertEqual(len(incs), 2)
        self.assertEqual([i["n_samples"] for i in incs], [60, 60])
        inc = m.incident(incs[0]["id"])
        self.assertEqual(inc["last_sample_time"], inc["samples"][-1]["time"])
        self.assertTrue(inc["journal_tail"])
        self.assertEqual(m.status()["incident_check"]["incident_id"], incs[0]["id"])
        with self.assertRaises(BackendError):
            m.incident("nope")
        self.assertTrue({"started", "write", "state"} <= {e["type"] for e in m.events(500)})
        self.assertTrue(all({"name", "type", "len"} <= set(k) for k in m.smc_keys()))

    def test_subscribe_delivers_about_four_per_second(self):
        m = self.Mock(seed=1, warmup_s=5)
        got, done = [], threading.Event()

        def cb(st):
            got.append(st["ts"])
            if len(got) >= 3:
                done.set()
        sub = m.subscribe(cb)
        try:
            self.assertTrue(done.wait(3))
        finally:
            sub.stop()
        self.assertTrue(m.connected)
        self.assertEqual(got, sorted(got))


@unittest.skipUnless(HAVE_TEXTUAL, "textual not installed (TUI venv)")
class LiveTest(unittest.TestCase):
    """LiveBackend against a real turbomacd on a fake sysroot, over its socket."""

    def setUp(self):
        from turbomac.daemon.logger import EventLog
        from turbomac.daemon.paths import Paths
        self.tmp = tempfile.TemporaryDirectory()
        self.fs = helpers.FakeSys()
        helpers.add_amdgpu(self.fs.root)
        self.sock = os.path.join(self.tmp.name, "t.sock")
        self.paths = Paths(sysroot=self.fs.root, socket=self.sock)
        self.d = helpers.make_daemon(self.paths, EventLog(stream=io.StringIO()))
        self.d.setup(start_server=True)
        self.d.gpu_thread.join(5)
        t = time.monotonic()
        for i in range(8):
            helpers.write(os.path.join(self.fs.zone, "energy_uj"), f"{1_000_000 + (i + 1) * 5_000_000}\n")
            self.d.tick(t + i * 0.25, slow=i % 4 == 0)

    def tearDown(self):
        self.d.shutdown()
        self.fs.cleanup()
        self.tmp.cleanup()

    def test_round_trips_over_the_socket(self):
        from turbomac import config
        from turbomac.tui.backend import LiveBackend
        be = LiveBackend(self.sock)
        st = be.status()
        self.assertEqual(st["governor"]["state"], "STATIC")
        self.assertEqual(len(be.history(60)), 2)
        res = be.config_set({"governor.ceiling_w": 38, "governor.floor_w": 99})
        self.assertEqual(res["accepted"], {"governor.ceiling_w": 38})
        self.assertIn("governor.floor_w", res["rejected"])
        self.assertEqual(config.load(self.paths.config)[0]["governor"]["ceiling_w"], 38)
        self.assertEqual(be.config_get()["config"]["governor"]["ceiling_w"], 38)
        self.assertIn("config_change", {e["type"] for e in be.events(100)})
        self.assertEqual(be.incidents(), [])
        got = threading.Event()
        sub = be.subscribe(lambda s: got.set())
        try:
            self.assertTrue(got.wait(3))
            self.assertTrue(be.connected)
        finally:
            sub.stop()

    def test_unreachable_socket_raises_and_subscription_reports_it(self):
        from turbomac.tui.backend import BackendError, LiveBackend
        be = LiveBackend(os.path.join(self.tmp.name, "missing.sock"))
        with self.assertRaises(BackendError):
            be.status()
        changed = threading.Event()
        sub = be.subscribe(lambda s: None, lambda ok, err: changed.set())
        try:
            self.assertTrue(changed.wait(3))
        finally:
            sub.stop()
        self.assertFalse(be.connected)
        self.assertIn("cannot connect", be.last_error)


if __name__ == "__main__":
    unittest.main()
