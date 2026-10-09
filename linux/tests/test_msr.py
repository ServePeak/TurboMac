import unittest

import helpers
from turbomac.daemon.msr import Msr, decode_pkg_limit, decode_therm_status, decode_units, read_msr

UNITS = 0xA0E03
LIMIT = 0x004283E800DD8320


class MsrTest(unittest.TestCase):
    def test_units(self):
        u = decode_units(UNITS)
        self.assertEqual(u["power_unit_w"], 0.125)
        self.assertEqual(u["time_unit_s"], 1 / 1024)
        self.assertEqual(u["energy_unit_j"], 1 / 16384)

    def test_firmware_pkg_power_limit(self):
        d = decode_pkg_limit(LIMIT, decode_units(UNITS))
        self.assertEqual(d["pl1_w"], 100.0)
        self.assertTrue(d["pl1_enable"])
        self.assertTrue(d["pl1_clamp"])
        self.assertAlmostEqual(d["pl1_window_s"], 28.0)
        self.assertEqual(d["pl2_w"], 125.0)
        self.assertTrue(d["pl2_enable"])
        self.assertFalse(d["pl2_clamp"])
        self.assertAlmostEqual(d["pl2_window_s"] * 1000, 2.44140625)
        self.assertFalse(d["lock"])

    def test_therm_status_bits(self):
        d = decode_therm_status((1 << 10) | (1 << 11) | 1)
        self.assertTrue(d["power_limited"] and d["power_limit_log"] and d["thermal_status"])
        self.assertFalse(d["prochot"])
        self.assertTrue(decode_therm_status(1 << 2)["prochot"])

    def test_pread_from_device_file_and_missing_device(self):
        fs = helpers.FakeSys(msr={0x606: UNITS, 0x610: LIMIT, 0x1B1: 1 << 10})
        try:
            m = Msr(fs.p("dev/cpu/0/msr"))
            self.assertEqual(m.pkg_limit()["pl2_w"], 125.0)
            self.assertTrue(m.therm_status()["power_limited"])
            fs.set_msr(0x610, (LIMIT & ~0x7FFF) | 280)       # PL1 35 W
            self.assertEqual(m.pkg_limit()["pl1_w"], 35.0)  # fresh pread, never a stale buffer
        finally:
            fs.cleanup()
        self.assertIsNone(read_msr("/nonexistent/dev/cpu/0/msr", 0x610))
        self.assertIsNone(Msr("/nonexistent/msr").pkg_limit())


if __name__ == "__main__":
    unittest.main()
