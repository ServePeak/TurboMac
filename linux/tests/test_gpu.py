import os
import unittest
from unittest import mock

import helpers
from turbomac.daemon import gpu as gpumod
from turbomac.daemon.gpu import Gpu, parse_sclk, parse_switcheroo
from turbomac.daemon.paths import Paths

ON = helpers.SWITCHEROO_ON
OFF = "0:IGD:+:Pwr:0000:00:02.0\n2:DIS: :Off:0000:01:00.0\n"


class GpuTest(unittest.TestCase):
    def setUp(self):
        self.fs = helpers.FakeSys()
        self.dev, self.hw = helpers.add_amdgpu(self.fs.root)
        self.paths = Paths(sysroot=self.fs.root)
        self.real_write = gpumod.write_text

    def tearDown(self):
        self.fs.cleanup()

    def switcheroo_emulator(self, works=True, d3=True):
        """works: switcheroo reports DIS Off after OFF; d3: PCI power_state leaves D0."""
        def fake(path, value):
            if path == self.paths.switcheroo:
                if value == "OFF" and works:
                    helpers.write(path, OFF)
                    if d3:
                        helpers.write(os.path.join(self.dev, "power_state"), "D3cold\n")
                elif value in ("ON", "DIS"):
                    helpers.write(path, ON)
                    helpers.write(os.path.join(self.dev, "power_state"), "D0\n")
                return None
            return self.real_write(path, value)
        return fake

    def guard_reads(self):
        """Fail if anything under the amdgpu device except power_state is read."""
        real = gpumod.read_text

        def guarded(path, *a, **kw):
            if path.startswith(self.dev) and not path.endswith("/power_state"):
                raise AssertionError(f"touched amdgpu file {path}")
            return real(path, *a, **kw)
        return mock.patch.multiple(gpumod, read_text=guarded,
                                   read_int=mock.Mock(side_effect=AssertionError("touched amdgpu")))

    def test_parsers(self):
        sw = parse_switcheroo(OFF)
        self.assertEqual((sw["DIS"]["power"], sw["DIS"]["active"], sw["IGD"]["active"]), ("Off", False, True))
        self.assertEqual(parse_sclk("0: 214Mhz\n1: 481Mhz *\n"), 481)

    def test_finds_amd_card_and_reads(self):
        g = Gpu(self.paths)
        self.assertTrue(g.dev.endswith("card2/device"))
        r = g.read()
        self.assertEqual((r["power_w"], r["cap_w"], r["sclk_mhz"], r["temp_c"]), (7.02, 35.0, 214, 50.0))

    def test_auto_and_low_set_cap_and_dpm(self):
        g = Gpu(self.paths)
        self.assertTrue(g.apply_mode("auto", 15)["ok"])
        self.assertEqual(helpers.read(os.path.join(self.hw, "power1_cap")), "15000000")
        res = g.apply_mode("low", 15)
        self.assertTrue(res["ok"], res)
        self.assertEqual(helpers.read(os.path.join(self.hw, "power1_cap")), "10000000")
        self.assertEqual(helpers.read(os.path.join(self.dev, "power_dpm_force_performance_level")), "low")

    def test_off_procedure_verifies_waits_and_stops_reading_amdgpu(self):
        g, slept = Gpu(self.paths), []
        with mock.patch.object(gpumod, "write_text", self.switcheroo_emulator()):
            res = g.apply_mode("off", 15, wait_s=10, sleep=slept.append)
        self.assertTrue(res["ok"], res)
        self.assertEqual(slept, [10])
        self.assertIn("power_state D3cold", res["steps"][-1])
        self.assertEqual(helpers.read(self.fs.p("sys/class/vtconsole/vtcon1/bind")), "0")
        with self.guard_reads():
            r = g.read()
        self.assertNotIn("power_w", r)
        self.assertEqual((r["switcheroo"]["DIS"], r["power_state"]), (" :Off", "D3cold"))
        self.assertEqual(Gpu(self.paths).mode, "off")    # a fresh reader sees it is already off

    def test_off_passes_on_switcheroo_alone_when_power_state_is_absent(self):
        os.unlink(os.path.join(self.dev, "power_state"))
        g = Gpu(self.paths)
        with mock.patch.object(gpumod, "write_text", self.switcheroo_emulator(d3=False)):
            res = g.apply_mode("off", 15, wait_s=10, sleep=lambda s: None)
        self.assertTrue(res["ok"], res)
        self.assertIn("power_state n/a", res["steps"][-1])

    def test_failed_off_reverts_to_low(self):
        for kw, why in (({"works": False}, "DIS Off"), ({"d3": False}, "power_state still D0")):
            helpers.write(self.paths.switcheroo, ON)
            g = Gpu(self.paths)
            with mock.patch.object(gpumod, "write_text", self.switcheroo_emulator(**kw)):
                res = g.apply_mode("off", 15, wait_s=10, sleep=lambda s: None)
            self.assertFalse(res["ok"])
            self.assertEqual(res["mode"], "low")
            self.assertIn(why, res["reason"])
            self.assertEqual(helpers.read(self.fs.p("sys/class/vtconsole/vtcon1/bind")), "1")
            self.assertEqual(helpers.read(self.paths.switcheroo), ON.strip())
            self.assertEqual(helpers.read(os.path.join(self.hw, "power1_cap")), "10000000")


if __name__ == "__main__":
    unittest.main()
