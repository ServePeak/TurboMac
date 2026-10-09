"""Socket protocol round trip, and the daemon wired up on a fake sysroot (no SMC, no MSR)."""
import io
import json
import os
import socket
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

import helpers
from turbomac import cap, config, protocol
from turbomac.daemon.logger import EventLog
from turbomac.daemon.paths import Paths
from turbomac.daemon import server as server_mod
from turbomac.daemon.sampler import Sampler
from turbomac.daemon.server import ControlServer


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "t.sock")
        self.n = 0

        def dispatch(cmd, args):
            if cmd == "status":
                self.n += 1
                return {"n": self.n}
            if cmd == "echo":
                return args
            raise ValueError(f"unknown command {cmd!r}")
        self.srv = ControlServer(self.path, dispatch)
        self.srv.start()

    def tearDown(self):
        self.srv.stop()
        self.tmp.cleanup()

    def test_call_error_subscribe_and_size_limit(self):
        c = protocol.Client(self.path, timeout=5)
        self.assertEqual(c.call("echo", a=1), {"a": 1})
        with self.assertRaises(protocol.ProtocolError):
            c.call("nope")
        stream = c.stream("subscribe")
        got = [next(stream)["n"] for _ in range(3)]
        stream.close()
        self.assertEqual(got, sorted(got))
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o660)
        with socket.socket(socket.AF_UNIX) as s:
            s.connect(self.path)
            s.sendall(b"x" * (protocol.MAX_REQUEST + 10) + b"\n")
            reply = protocol.decode(s.makefile("rb").readline())
        self.assertFalse(reply["ok"])
        self.assertIn("exceeds", reply["error"])


    def test_idle_connection_is_closed(self):
        with mock.patch.object(server_mod._Conn, "timeout", 0.2), socket.socket(socket.AF_UNIX) as s:
            s.connect(self.path)
            s.settimeout(3)
            t = time.monotonic()
            self.assertEqual(s.recv(10), b"")
            self.assertLess(time.monotonic() - t, 2.0)


class SamplerTest(unittest.TestCase):
    def test_acpw_outside_30_to_100_w_is_invalid(self):
        class FakeSmc:
            ok = True

            def __init__(self, acpw):
                self.vals = {"PDTR": 30.5, "VD0R": 20.2, "ID0R": 1.5, "ACPW": acpw}

            def has(self, key):
                return False

            def read_many(self, keys):
                return {k: self.vals.get(k) for k in keys}
        for acpw, want in ((79496, 79.496), (150000, None), (12000, None), (0, None)):
            s = Sampler(smc=FakeSmc(acpw)).fast(1.0, 1.0)
            self.assertEqual((s["acpw_w"], s["valid"]), (want, want is not None))


class DaemonTest(unittest.TestCase):
    def setUp(self):
        self.fs = helpers.FakeSys()
        self.paths = Paths(sysroot=self.fs.root)
        self.events = EventLog(stream=io.StringIO())

    def tearDown(self):
        self.fs.cleanup()

    def test_cap_apply_captures_firmware_then_caps(self):
        os.environ["TURBOMAC_SYSROOT"] = self.fs.root
        try:
            out = io.StringIO()
            with redirect_stdout(out):
                rc = cap.main(["apply"])
        finally:
            del os.environ["TURBOMAC_SYSROOT"]
        self.assertEqual(rc, 0, out.getvalue())
        self.assertEqual(helpers.read(os.path.join(self.fs.zone, "constraint_0_power_limit_uw")), "35000000")
        with open(self.paths.firmware_limits) as fh:
            self.assertEqual(json.load(fh)["pl1_w"], 100.0)
        self.assertIn("GPU mode auto", out.getvalue())

    def test_static_mode_ticks_log_and_config_set(self):
        d = helpers.make_daemon(self.paths, self.events)
        d.setup(start_server=False)
        d.gpu_thread.join(5)
        t = time.monotonic()
        for i in range(8):
            helpers.write(os.path.join(self.fs.zone, "energy_uj"), f"{1_000_000 + (i + 1) * 5_000_000}\n")
            d.tick(t + i * 0.25, slow=i % 4 == 0)
        st = d.dispatch("status", {})
        self.assertEqual(st["governor"]["state"], "STATIC")
        self.assertEqual(st["governor"]["pl1_w"], 35.0)
        self.assertEqual(st["governor"]["counters"]["readback_failures"], 0)
        self.assertAlmostEqual(st["latest"]["package_w"], 20.0, places=3)
        self.assertEqual(helpers.read(os.path.join(self.fs.zone, "constraint_1_power_limit_uw")), "35000000")
        self.assertEqual(st["limits"]["firmware"]["pl2_w"], 125.0)
        self.assertEqual(st["incident_check"]["verdict"], "unknown")    # stub journal: hermetic on any host
        json.dumps(st, default=str)
        self.assertTrue(d.logger.drain(5))
        files = os.listdir(self.paths.samples_dir)
        with open(os.path.join(self.paths.samples_dir, files[0])) as fh:
            lines = [json.loads(x) for x in fh]
        self.assertEqual((lines[0]["kind"], len(lines)), ("header", 3))
        self.assertEqual(lines[1]["state"], "STATIC")
        self.assertEqual(len(d.dispatch("history", {"seconds": 60})), 2)
        r = d.dispatch("config.set", {"governor.mode": "dynamic", "governor.ceiling_w": 50, "gpu.mode": "low"})
        self.assertEqual(r["accepted"]["governor.mode"], "dynamic")
        self.assertIn("governor.ceiling_w", r["rejected"])
        self.assertFalse(r["gpu"]["ok"])                                  # no amdgpu in the fake tree
        saved, _ = config.load(self.paths.config)
        self.assertEqual(saved["governor"]["mode"], "dynamic")
        d.tick(t + 2.25, slow=False)                                      # no SMC: dynamic goes stale
        d.tick(t + 2.5, slow=False)
        self.assertEqual(d.gov.state, "FALLBACK")
        types = {e["type"] for e in d.dispatch("events", {"n": 500})}
        self.assertTrue({"started", "write", "config_change", "fallback"} <= types, types)
        d.dispatch("config.reset", {})
        self.assertEqual(config.load(self.paths.config)[0]["governor"]["mode"], "static")
        d.shutdown()
        self.assertTrue(os.path.exists(self.paths.clean_marker))

    def test_watchdog_needs_a_recent_successful_governor_step(self):
        d = helpers.make_daemon(self.paths, self.events)
        d.setup(start_server=False)
        sent = []
        d.notify = lambda msg: sent.append((t, msg))
        t0 = time.monotonic()
        for i in range(13):                                    # 3 s of healthy loop
            t = t0 + i * 0.25
            d.loop_once(t, i)
        last_ok = d.last_step_ok

        def broken(now, slow):
            raise RuntimeError("governor wedged")
        d.tick = broken
        for i in range(13, 60):                                # every tick fails from here on
            t = t0 + i * 0.25
            d.loop_once(t, i)
        pings = [ts for ts, m in sent if m == "WATCHDOG=1"]
        self.assertTrue(any(ts <= last_ok for ts in pings))
        self.assertTrue(all(ts - last_ok <= 5.0 for ts in pings), pings)
        self.assertIn(("READY=1"), [m for _, m in sent])
        self.assertTrue(any(e["type"] == "tick_error" for e in self.events.recent(500)))
        d.shutdown()

    def test_telemetry_and_write_exceptions_never_skip_or_fake_the_governor(self):
        d = helpers.make_daemon(self.paths, self.events)
        d.setup(start_server=False)
        t = time.monotonic()

        def boom(*a, **k):
            raise OSError("sysfs went away")
        d.sampler.slow = boom
        d.sampler.fast = boom
        with mock.patch("turbomac.daemon.main.apply_and_verify", side_effect=RuntimeError("EIO")):
            d.tick(t, slow=True)
        self.assertEqual(d.last_step_ok, t)                     # governor stepped anyway
        self.assertFalse(d.readback["ok"])
        self.assertTrue(d.readback["reason"].startswith("exception: RuntimeError"))
        self.assertFalse(d.sampler.latest["valid"])
        d.tick(t + 0.25, slow=False)
        self.assertEqual(d.gov.state, "FALLBACK")               # the failed write was not believed
        d.shutdown()

    def gpu_events(self, typ):
        return [e for e in self.events.recent(1000) if e["type"] == typ]

    def test_startup_gpu_apply_is_retried_until_amdgpu_appears(self):
        d = helpers.make_daemon(self.paths, self.events)
        d.setup(start_server=False)
        d.gpu_thread.join(5)
        self.assertIn("amdgpu device not found", d.gpu_result["reason"])
        dev, hw = helpers.add_amdgpu(self.fs.root)             # amdgpu finishes probing late
        t = time.monotonic()
        d.tick(t, slow=False)                                  # schedules the first retry
        d.tick(t + 14.75, slow=False)
        self.assertEqual(self.gpu_events("gpu_retry"), [])
        d.tick(t + 15.0, slow=False)
        d.gpu_thread.join(5)
        self.assertTrue(d.gpu_result["ok"], d.gpu_result)
        self.assertEqual(helpers.read(os.path.join(hw, "power1_cap")), "15000000")
        self.assertEqual(len(self.gpu_events("gpu_retry")), 1)
        self.assertEqual(self.gpu_events("gpu_retry_succeeded")[0]["attempt"], 1)
        d.tick(t + 30.0, slow=False)
        d.tick(t + 45.0, slow=False)
        self.assertEqual(len(self.gpu_events("gpu_retry")), 1)
        d.shutdown()

    def test_startup_gpu_retries_stop_after_12(self):
        d = helpers.make_daemon(self.paths, self.events)
        d.setup(start_server=False)
        d.gpu_thread.join(5)
        t = time.monotonic()
        d.tick(t, slow=False)
        for k in range(1, 16):
            d.tick(t + 15.0 * k, slow=False)
            d.gpu_thread.join(5)
        self.assertEqual(len(self.gpu_events("gpu_retry")), 12)
        self.assertEqual(self.gpu_events("gpu_retry_gave_up")[0]["attempt"], 12)
        d.shutdown()


if __name__ == "__main__":
    unittest.main()
