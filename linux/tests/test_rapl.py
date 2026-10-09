import json
import os
import unittest

import helpers
from turbomac.daemon.msr import Msr
from turbomac.daemon.rapl import (Rapl, apply_and_verify, clamp_limits, compare_limits, energy_delta_w,
                                  load_or_capture_firmware, valid_firmware, w_to_uw)

UNITS, LIMIT = 0xA0E03, 0x004283E800DD8320


def msr_value(pl1_w, pl2_w):
    return (LIMIT & ~0x7FFF & ~(0x7FFF << 32)) | int(pl1_w * 8) | (int(pl2_w * 8) << 32)


class EnergyTest(unittest.TestCase):
    def test_delta_and_wrap(self):
        self.assertAlmostEqual(energy_delta_w(1_000_000, 11_000_000, 262_143_328_850, 0.25), 40.0)
        mx = 262_143_328_850
        self.assertAlmostEqual(energy_delta_w(mx - 4_000_000, 6_000_000, mx, 0.25), 40.0)
        self.assertIsNone(energy_delta_w(5, 1, 0, 0.25))
        self.assertIsNone(energy_delta_w(1, 5, mx, 0.0))

    def test_uw_encoding(self):
        self.assertEqual(w_to_uw(35), 35_000_000)
        self.assertEqual(w_to_uw(37.5), 37_500_000)
        self.assertEqual(w_to_uw(0.1 + 0.2), 300_000)

    def test_compare_tolerance(self):
        sysfs = {"pl1_uw": 35_000_000, "pl2_uw": 35_000_000, "enabled": 1}
        m = {"pl1_w": 35.125, "pl2_w": 34.875, "pl1_enable": True}
        self.assertTrue(compare_limits(35, 35, sysfs, m)[0])
        self.assertFalse(compare_limits(35, 35, sysfs, {**m, "pl1_w": 35.25})[0])
        self.assertFalse(compare_limits(35, 35, {**sysfs, "pl2_uw": 125_000_000}, m)[0])
        self.assertFalse(compare_limits(35, 35, {**sysfs, "enabled": 0}, m)[0])
        self.assertFalse(compare_limits(35, 35, sysfs, {**m, "pl1_enable": False})[0])
        ok, why = compare_limits(35, 35, sysfs, None)
        self.assertTrue(ok)
        self.assertIn("MSR unavailable", why)

    def test_clamp(self):
        self.assertEqual(clamp_limits(60, 70, 45, None), (45, 45))
        self.assertEqual(clamp_limits(40, 44, 45, {"pl1_w": 38.0, "pl2_w": 42.0}), (38.0, 42.0))
        self.assertEqual(clamp_limits(40, 44, 45, {"pl1_w": 35.0, "pl2_w": 35.0, "suspect": True}), (40, 44))
        self.assertEqual(clamp_limits(40, 44, 45, {"pl1_w": "100", "pl2_w": None}), (40, 44))
        self.assertEqual(clamp_limits(37.3, 37.99, 45, None), (37.25, 37.875))   # down to 1/8 W
        self.assertFalse(valid_firmware({"pl1_w": True, "pl2_w": 125.0}))


class SysfsTest(unittest.TestCase):
    def setUp(self):
        self.fs = helpers.FakeSys(msr={0x606: UNITS, 0x610: LIMIT})
        self.rapl = Rapl(self.fs.zone)
        self.msr = Msr(self.fs.p("dev/cpu/0/msr"))

    def tearDown(self):
        self.fs.cleanup()

    def test_zones_and_power(self):
        self.assertEqual(sorted(self.rapl.zones), ["core", "dram", "package", "uncore"])
        self.assertEqual(self.rapl.power(10.0)["package"], None)
        helpers.write(os.path.join(self.fs.zone, "energy_uj"), "11000000\n")
        helpers.write(self.fs.zone + ":0/energy_uj", "3000000\n")
        p = self.rapl.power(10.25)
        self.assertAlmostEqual(p["package"], 40.0)
        self.assertAlmostEqual(p["core"], 10.0)

    def test_write_then_verify_against_sysfs_and_msr(self):
        self.fs.set_msr(0x610, msr_value(35, 35))
        rb = apply_and_verify(self.rapl, self.msr, 35, 35, 45, {"pl1_w": 100.0, "pl2_w": 125.0})
        self.assertTrue(rb["ok"], rb["reason"])
        self.assertEqual(helpers.read(os.path.join(self.fs.zone, "constraint_0_power_limit_uw")), "35000000")
        self.assertEqual(helpers.read(os.path.join(self.fs.zone, "constraint_1_power_limit_uw")), "35000000")
        self.assertEqual(helpers.read(os.path.join(self.fs.zone, "constraint_0_time_window_us")), "27983872")
        # The MSR still says 35 W: asking for 40 W must be reported as a mismatch.
        rb = apply_and_verify(self.rapl, self.msr, 40, 40, 45, None)
        self.assertFalse(rb["ok"])
        self.assertIn("MSR 0x610", rb["reason"])

    def test_rounds_down_to_eighth_watt_and_compares_exactly(self):
        self.fs.set_msr(0x610, msr_value(37.25, 37.25))
        rb = apply_and_verify(self.rapl, self.msr, 37.3, 37.3, 45, None)
        self.assertTrue(rb["ok"], rb["reason"])
        self.assertEqual((rb["pl1_w"], rb["write_errors"]), (37.25, []))
        self.assertEqual(helpers.read(os.path.join(self.fs.zone, "constraint_0_power_limit_uw")), "37250000")

    def test_never_writes_above_hard_max(self):
        rb = apply_and_verify(self.rapl, None, 100, 125, 45, None)
        self.assertEqual((rb["pl1_w"], rb["pl2_w"]), (45, 45))
        self.assertEqual(helpers.read(os.path.join(self.fs.zone, "constraint_1_power_limit_uw")), "45000000")

    def test_firmware_capture_once(self):
        path = self.fs.p("var/lib/turbomac/firmware-limits.json")
        fw, warn = load_or_capture_firmware(path, self.rapl, self.msr, "boot-1")
        self.assertIsNone(warn)
        self.assertEqual((fw["pl1_w"], fw["pl2_w"], fw["msr"]["pl1_clamp"]), (100.0, 125.0, True))
        self.rapl.write_limits(35, 35)
        fw2, _ = load_or_capture_firmware(path, self.rapl, self.msr)
        self.assertEqual(fw2["pl1_w"], 100.0)      # not recaptured after a cap was written
        fw3, warn = load_or_capture_firmware(path, self.rapl, self.msr, force=True)
        self.assertTrue(fw3["suspect"])            # returned in memory only
        self.assertIn("not saved", warn)
        with open(path) as fh:
            self.assertEqual(json.load(fh)["pl1_uw"], 100_000_000)

    def test_suspect_capture_is_never_saved(self):
        path = self.fs.p("var/lib/turbomac/firmware-limits.json")
        self.rapl.write_limits(35, 35)
        fw, warn = load_or_capture_firmware(path, self.rapl, self.msr)
        self.assertTrue(fw["suspect"] and not valid_firmware(fw))
        self.assertFalse(os.path.exists(path))

    def test_suspect_or_invalid_file_is_treated_as_missing(self):
        path = self.fs.p("var/lib/turbomac/firmware-limits.json")
        for bad in ({"pl1_w": 35.0, "pl2_w": 35.0, "pl1_uw": 35000000, "pl2_uw": 35000000, "suspect": True},
                    {"pl1_w": "100", "pl2_w": 125.0}):
            helpers.write(path, json.dumps(bad))
            self.rapl.write_limits(35, 35)        # cap active: ignored, nothing saved
            fw, warn = load_or_capture_firmware(path, self.rapl, self.msr)
            self.assertFalse(valid_firmware(fw))
            self.assertIn("ignored", warn)
            with open(path) as fh:
                self.assertEqual(json.load(fh), bad)
            self.rapl.write_limits(100, 125)      # e.g. next boot, before the cap: replaced
            fw, warn = load_or_capture_firmware(path, self.rapl, self.msr)
            self.assertTrue(valid_firmware(fw))
            self.assertIn("replaced", warn)
            with open(path) as fh:
                self.assertEqual(json.load(fh)["pl1_w"], 100.0)


if __name__ == "__main__":
    unittest.main()
