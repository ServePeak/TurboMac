"""Socket protocol round trip, and the daemon wired up on a fake sysroot (no SMC, no MSR)."""
import glob
import io
import json
import os
import socket
import struct
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

import helpers
from turbomac import cap, config, ctl, protocol
from turbomac.daemon import main as main_mod
from turbomac.daemon import smc as smc_mod
from turbomac.daemon.cpu import CpuReader
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


    def wait_clients(self, n):
        end = time.monotonic() + 3
        while self.srv.clients != n and time.monotonic() < end:
            time.sleep(0.01)
        return self.srv.clients

    def test_clients_count_follows_connections(self):
        self.assertEqual(self.srv.clients, 0)
        with socket.socket(socket.AF_UNIX) as s:
            s.connect(self.path)
            s.sendall(protocol.encode({"cmd": "echo", "args": {"x": 1}}))
            self.assertTrue(protocol.decode(s.makefile("rb").readline())["ok"])   # handle() is running
            self.assertEqual(self.srv.clients, 1)
            with socket.socket(socket.AF_UNIX) as s2:
                s2.connect(self.path)
                self.assertEqual(self.wait_clients(2), 2)
            self.assertEqual(self.wait_clients(1), 1)
        self.assertEqual(self.wait_clients(0), 0)

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


def write_proc(root, pid, name, utime, stime):
    """A /proc/<pid>/stat whose fields 14/15 (utime/stime) are as given, plus comm."""
    helpers.write(os.path.join(root, "proc", str(pid), "stat"),
                  f"{pid} ({name}) S " + " ".join(["0"] * 10) + f" {utime} {stime} 0 0 20 0 1\n")
    helpers.write(os.path.join(root, "proc", str(pid), "comm"), name + "\n")


class CpuReaderCacheTest(unittest.TestCase):
    def setUp(self):
        self.fs = helpers.FakeSys()
        self.paths = Paths(sysroot=self.fs.root)

    def tearDown(self):
        self.fs.cleanup()

    def test_top_scan_runs_every_5_s_and_rates_over_the_scan_interval(self):
        write_proc(self.fs.root, 100, "whisper", 1000, 0)
        write_proc(self.fs.root, 200, "idle", 50, 0)
        r = CpuReader(self.paths)
        r.hz = 100
        with mock.patch("turbomac.daemon.cpu.os.scandir", wraps=os.scandir) as scans:
            self.assertEqual(r._top(0.0), [])                        # first scan: no interval yet
            write_proc(self.fs.root, 100, "whisper", 1300, 200)      # +500 ticks = 5 s of one core
            write_proc(self.fs.root, 200, "idle", 60, 0)
            self.assertEqual(r.read(2.0)["top"], [])                 # cached, stale: no rescan at t=2
            self.assertEqual(scans.call_count, 1)
            top = r._top(5.0)                                        # rescanned at t=5
            self.assertEqual(scans.call_count, 2)
            self.assertEqual(top, [{"pid": 100, "name": "whisper", "cpu_pct": 100.0},   # 500 ticks / 5 s
                                   {"pid": 200, "name": "idle", "cpu_pct": 2.0}])
            write_proc(self.fs.root, 100, "whisper", 9999, 0)
            self.assertIs(r.read(9.9)["top"], top)                  # cached until t=10
            self.assertEqual(scans.call_count, 2)

    def test_top_cadence_zero_scans_every_read(self):
        write_proc(self.fs.root, 100, "whisper", 1000, 0)
        r = CpuReader(self.paths, top_every_s=0)
        r.hz = 100
        r._top(0.0)
        write_proc(self.fs.root, 100, "whisper", 1100, 0)
        self.assertEqual(r._top(1.0), [{"pid": 100, "name": "whisper", "cpu_pct": 100.0}])
        write_proc(self.fs.root, 100, "whisper", 1150, 0)
        self.assertEqual(r._top(2.0), [{"pid": 100, "name": "whisper", "cpu_pct": 50.0}])

    def test_coretemp_is_globbed_once_and_read_fresh(self):
        temp = helpers.add_coretemp(self.fs.root, 61.0)
        helpers.write(temp.replace("temp1", "temp2"), "55000\n")
        helpers.write(temp.replace("temp1_input", "temp2_label"), "Core 0\n")
        cpu0 = os.path.join(self.fs.root, "sys/devices/system/cpu/cpu0")
        helpers.write(os.path.join(cpu0, "cpufreq/scaling_cur_freq"), "2900000\n")
        helpers.write(os.path.join(cpu0, "thermal_throttle/core_throttle_count"), "7\n")
        r = CpuReader(self.paths)
        with mock.patch("glob.glob", wraps=glob.glob) as g:
            self.assertEqual(r.temps(), {"cores": {"c0": 55.0}, "pkg_c": 61.0})
            helpers.write(temp, "88000\n")
            helpers.write(os.path.join(cpu0, "cpufreq/scaling_cur_freq"), "800000\n")
            out = r.read(1.0)
            self.assertEqual((out["temps"]["pkg_c"], out["mhz"], out["throttle_core"]), (88.0, [800], 7))
            self.assertEqual(g.call_count, 1)


class SmcSensorCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        smc_mod.clear_caches()

    def tearDown(self):
        smc_mod.clear_caches()
        self.tmp.cleanup()

    def test_sensor_files_are_globbed_once_values_read_fresh(self):
        d = self.tmp.name
        for n, (label, val) in enumerate([("TC0P", 61500), ("TA0P", -127000), ("TG0D", 57000)], 1):
            helpers.write(f"{d}/temp{n}_input", f"{val}\n")
            helpers.write(f"{d}/temp{n}_label", f"{label}\n")
        base = helpers.add_applesmc_fans(d)
        with mock.patch("glob.glob", wraps=glob.glob) as g:
            self.assertEqual(smc_mod.read_temps(d), {"TC0P": 61.5, "TG0D": 57.0})
            self.assertEqual([f["rpm"] for f in smc_mod.read_fans(base, 0.0)], [2161, 1999])
            self.assertEqual(g.call_count, 2)
            helpers.write(f"{d}/temp1_input", "70000\n")
            helpers.write(f"{d}/temp2_input", "31000\n")            # TA0P becomes valid: shown
            helpers.write(f"{d}/temp3_input", "151000\n")           # TG0D becomes invalid: dropped
            helpers.write(f"{base}/fan1_input", "3500\n")
            helpers.write(f"{base}/fan1_min", "3400\n")
            helpers.write(f"{base}/fan2_manual", "1\n")
            self.assertEqual(smc_mod.read_temps(d), {"TC0P": 70.0, "TA0P": 31.0})
            fans = smc_mod.read_fans(base, 2.0)                     # rpm every call, min/max/manual cached
            self.assertEqual((fans[0]["label"], fans[0]["rpm"], fans[0]["min"], fans[1]["manual"]),
                             ("Left side", 3500, 2160, 0))
            helpers.write(f"{base}/fan1_input", "3600\n")
            fans = smc_mod.read_fans(base, 3.0)                     # 3 s later: re-read
            self.assertEqual((fans[0]["rpm"], fans[0]["min"], fans[1]["max"], fans[1]["manual"]),
                             (3600, 3400, 5489, 1))
            helpers.write(f"{base}/fan1_min", "2160\n")
            self.assertEqual(smc_mod.read_fans(base, 5.0)[0]["min"], 3400)
            self.assertEqual(smc_mod.read_fans(base, 6.0)[0]["min"], 2160)
            self.assertEqual(smc_mod.read_fans(base, 6.5, every_s=0)[0]["min"], 2160)
            self.assertEqual(g.call_count, 2)
            smc_mod.clear_caches()
            smc_mod.read_temps(d)
            self.assertEqual(g.call_count, 3)


def flt(v):
    return struct.pack("<f", v)


class CountingSmcIO:
    """applesmc's key_at_index selector over a dict of keys; counts SMC transactions per kind."""

    def __init__(self, vals):
        self.keys = [(k, typ, raw) for k, (typ, raw) in sorted(vals.items())]
        self.sel = 0
        self.names = self.datas = 0

    def set(self, key, raw):
        self.keys = [(k, t, raw if k == key else r) for k, t, r in self.keys]

    def key_count(self):
        return len(self.keys)

    def get_selector(self):
        return str(self.sel)

    def select(self, i):
        self.sel = int(i)

    def name(self):
        self.names += 1
        return self.keys[self.sel][0]

    def type(self):
        return self.keys[self.sel][1]

    def length(self):
        return len(self.keys[self.sel][2])

    def data(self):
        self.datas += 1
        return self.keys[self.sel][2]


class SmcCadenceTest(unittest.TestCase):
    def setUp(self):
        smc_mod.clear_caches()
        self.tmp = tempfile.TemporaryDirectory()
        vals = {"PDTR": ("flt ", flt(30.0)), "VD0R": ("flt ", flt(20.25)), "ID0R": ("flt ", flt(1.5)),
                "ACPW": ("ui32", (79496).to_bytes(4, "big"))}
        vals.update({k: ("flt ", flt(1.0)) for k in ("PSTR", "PCPC", "PCPG", "PCPT", "PG0R", "PHPC")})
        self.io = CountingSmcIO(vals)
        self.smc = smc_mod.SMC(self.io)
        self.assertTrue(self.smc.open())
        self.io.names = self.io.datas = 0

    def tearDown(self):
        smc_mod.clear_caches()
        self.tmp.cleanup()

    def key_reads(self):
        self.assertEqual(self.io.names, self.io.datas)          # every key read verifies the selector name
        n = self.io.datas
        self.io.names = self.io.datas = 0
        return n

    def test_fast_reads_pdtr_every_tick_and_vd0r_id0r_acpw_on_the_slow_tick(self):
        s = Sampler(smc=self.smc)
        for i in range(12):
            t = i * 0.25
            slow = i % 4 == 0
            self.io.set("PDTR", flt(30.0 + i))
            self.io.set("VD0R", flt(20.0 + i))
            lat = s.fast(t, 1000.0 + t, full=slow)
            self.assertEqual(self.key_reads(), 4 if slow else 1, i)
            gov = s.gov_input(t)
            self.assertEqual((lat["pdtr_w"], gov["pdtr_w"]), (30.0 + i, 30.0 + i))   # 4 Hz to the governor
            self.assertEqual(gov["acpw_w"], 79.496)
            self.assertTrue(lat["valid"])
            self.assertEqual((lat["v"], lat["a"], lat["acpw_w"]), (20.0 + i // 4 * 4, 1.5, 79.496))   # held 1 Hz
        self.assertEqual(len(s.w["v"].d), 3)                      # one window value per slow tick
        self.assertEqual(s.w["v"].mean(2.0, 1.0), 28.0)           # the record's 1 s mean is the fresh read
        self.io.set("ACPW", (12000).to_bytes(4, "big"))           # out of 30..100 W: not pushed, not shown
        lat = s.fast(3.0, 1003.0, full=True)
        self.assertEqual((lat["acpw_w"], lat["valid"], s.gov_input(3.0)["acpw_w"]), (None, True, 79.496))
        self.assertFalse(Sampler(smc=self.smc).fast(0.0, 0.0, full=True)["valid"])   # no valid ACPW yet

    def test_temps_and_rails_every_3_s_fans_rpm_every_call(self):
        base = helpers.add_applesmc_fans(self.tmp.name)
        helpers.write(f"{base}/temp1_label", "TC0P\n")
        s = Sampler(smc=self.smc, smc_dir=base)
        got = []
        for t in range(7):                                         # slow ticks of an injected clock
            helpers.write(f"{base}/temp1_input", f"{60000 + t * 1000}\n")
            helpers.write(f"{base}/fan1_input", f"{3000 + t}\n")
            helpers.write(f"{base}/fan1_min", f"{2000 + t}\n")
            self.io.set("PSTR", flt(float(t)))
            sd = s.slow(float(t), 1000.0 + t)
            got.append((sd["smc_temps"]["TC0P"], sd["smc"]["PSTR"], self.key_reads(),
                        sd["fans"][0]["rpm"], sd["fans"][0]["min"]))
        self.assertEqual(got, [(60.0, 0.0, 6, 3000, 2000), (60.0, 0.0, 0, 3001, 2000), (60.0, 0.0, 0, 3002, 2000),
                               (63.0, 3.0, 6, 3003, 2003), (63.0, 3.0, 0, 3004, 2003), (63.0, 3.0, 0, 3005, 2003),
                               (66.0, 6.0, 6, 3006, 2006)])
        s0 = Sampler(smc=self.smc, smc_dir=base, smc_slow_every_s=0)   # settable: 0 reads every call
        for t in (0.0, 0.5):
            s0.slow(t, t)
            self.assertEqual(self.key_reads(), 6)


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

    def counting_daemon(self):
        d = helpers.make_daemon(self.paths, self.events)
        d.setup(start_server=False)
        d.gpu_thread.join(5)
        builds = mock.patch.object(d, "build_status", wraps=d.build_status).start()
        derived = mock.patch.object(d.sampler, "derived", wraps=d.sampler.derived).start()
        self.addCleanup(mock.patch.stopall)
        return d, builds, derived

    def test_status_snapshot_every_tick_only_while_a_client_is_connected(self):
        d, builds, derived = self.counting_daemon()
        t = time.monotonic()
        for i in range(8):                                      # no server: 1 Hz snapshots
            d.tick(t + i * 0.25, slow=i % 4 == 0)
        self.assertEqual(builds.call_count, 2)
        self.assertEqual(derived.call_count, 2)                 # fast ticks need no derived() at all
        d.server = mock.Mock(clients=1)
        for i in range(8, 16):                                  # a client: every tick
            d.tick(t + i * 0.25, slow=i % 4 == 0)
        self.assertEqual(builds.call_count, 10)
        self.assertEqual(derived.call_count, 10)
        d.server.clients = 0
        for i in range(16, 24):
            d.tick(t + i * 0.25, slow=i % 4 == 0)
        self.assertEqual(builds.call_count, 12)
        d.server = None
        d.shutdown()

    def test_derived_is_computed_once_per_slow_tick(self):
        helpers.add_applesmc_fans(self.fs.root)                 # fan step, log record and status all run
        helpers.add_coretemp(self.fs.root, 90.0)
        smc_mod.clear_caches()
        d, builds, derived = self.counting_daemon()
        self.assertIsNotNone(d.fanctl)
        t = time.monotonic()
        d.server = mock.Mock(clients=2)
        for k, slow in enumerate((True, False, True)):
            before = derived.call_count
            d.tick(t + k * 0.25, slow=slow)
            self.assertEqual(derived.call_count - before, 1, k)
        self.assertEqual(len(d.sampler.history), 2)
        self.assertEqual(d.fanctl.status()["reason"].split()[0], "pkg")    # the fan step used this tick's data
        d.server = None
        d.shutdown()

    def test_self_meter_in_status_record_and_ctl(self):
        helpers.write(os.path.join(self.fs.root, "proc/self/statm"), "90000 9984 2000 1 0 8000 0\n")
        d = helpers.make_daemon(self.paths, self.events)
        d.setup(start_server=False)
        d.gpu_thread.join(5)
        t = time.monotonic()
        for i in range(5):
            d.tick(t + i * 0.25, slow=i % 4 == 0)
        st = d.dispatch("status", {})
        cpu, rss = st["self"]["cpu_pct"], st["self"]["rss_mib"]
        self.assertIsInstance(cpu, float)
        self.assertGreaterEqual(cpu, 0.0)
        self.assertEqual(rss, round(9984 * main_mod.PAGE_SIZE / 2**20, 1))
        self.assertEqual(d.sampler.history[-1]["self"], st["self"])
        first = ctl.render_status(st).splitlines()[0]
        self.assertTrue(first.endswith(f"  daemon {cpu:.1f} % of a core, {rss:.1f} MiB"), first)
        self.assertNotIn("daemon", ctl.watch_line(st))
        self.assertTrue(d.logger.drain(5))
        with open(os.path.join(self.paths.samples_dir, os.listdir(self.paths.samples_dir)[0])) as fh:
            recs = [json.loads(x) for x in fh][1:]
        self.assertTrue(all(isinstance(r["self"]["cpu_pct"], float) for r in recs), recs)
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
