import json
import os
import struct
import tempfile
import unittest

import helpers
from turbomac.daemon.smc import SMC, decode, read_fans, read_temps


class DecodeTest(unittest.TestCase):
    def test_flt_is_little_endian_float32(self):
        raw = struct.pack("<f", 62.4375)
        self.assertEqual(raw, b"\x00\xc0\x79\x42")
        self.assertEqual(decode("flt ", raw), 62.4375)
        self.assertAlmostEqual(decode("flt ", b"\x9a\x99\xa1\x41"), 20.2, places=5)

    def test_ui32_acpw_big_endian_milliwatts(self):
        self.assertEqual(decode("ui32", b"\x00\x01\x36\x88"), 79496)
        self.assertEqual(decode("ui8 ", b"\x07"), 7)
        self.assertEqual(decode("ui16", b"\x12\x34"), 0x1234)

    def test_signed(self):
        self.assertEqual(decode("si8 ", b"\xff"), -1)
        self.assertEqual(decode("si16", b"\xff\x38"), -200)

    def test_sp78(self):
        self.assertEqual(decode("sp78", b"\x3d\x80"), 61.5)
        self.assertEqual(decode("sp78", b"\xff\x00"), -1.0)

    def test_fpe2(self):
        self.assertEqual(decode("fpe2", b"\x21\xd4"), 2165.0)  # fan rpm * 4

    def test_generic_fixed_point(self):
        self.assertEqual(decode("fp88", b"\x01\x80"), 1.5)
        self.assertEqual(decode("sp4b", b"\xf8\x00"), -1.0)   # int16 -2048 / 2**11

    def test_flag_and_unknown(self):
        self.assertIs(decode("flag", b"\x01"), True)
        self.assertIs(decode("flag", b"\x00"), False)
        self.assertEqual(decode("ch8*", b"AB"), "4142")


class FakeIO:
    """Emulates applesmc's global key_at_index selector."""

    def __init__(self, keys):
        self.keys = keys            # list of (name, type, bytes)
        self.sel = 5
        self.selects = 0

    def key_count(self):
        return len(self.keys)

    def get_selector(self):
        return str(self.sel)

    def select(self, i):
        self.sel = int(i)
        self.selects += 1

    def name(self):
        return self.keys[self.sel][0]

    def type(self):
        return self.keys[self.sel][1]

    def length(self):
        return len(self.keys[self.sel][2])

    def data(self):
        return self.keys[self.sel][2] + b"\x00\x00"   # driver may return padding


KEYS = [("ACPW", "ui32", b"\x00\x01\x36\x88"), ("ID0R", "flt ", struct.pack("<f", 3.0)),
        ("PDTR", "flt ", struct.pack("<f", 62.5)), ("PSTR", "flt ", struct.pack("<f", 12.0)),
        ("TC0P", "sp78", b"\x3d\x80"), ("VD0R", "flt ", struct.pack("<f", 20.25))]


class SmcTableTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = os.path.join(self.tmp.name, "smc-keys.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_scan_read_verify_and_restore(self):
        io = FakeIO(list(KEYS))
        smc = SMC(io, self.cache)
        self.assertTrue(smc.open())
        self.assertEqual(smc.read("PDTR"), 62.5)
        self.assertEqual(smc.read("ACPW"), 79496)
        self.assertIsNone(smc.read("NOPE"))
        self.assertEqual(smc.keys_info()[0], {"name": "ACPW", "index": 0, "type": "ui32", "len": 4})
        smc.restore()
        self.assertEqual(io.sel, 5)
        with open(self.cache) as fh:
            self.assertEqual(json.load(fh)["key_count"], len(KEYS))

    def test_cache_is_used_and_a_moved_key_triggers_rescan(self):
        SMC(FakeIO(list(KEYS)), self.cache).open()
        io = FakeIO(list(KEYS))
        smc = SMC(io, self.cache)
        smc.open()
        self.assertEqual(io.selects, 0)            # no linear scan: table came from cache
        io.keys[2], io.keys[3] = io.keys[3], io.keys[2]   # PDTR moved: name check catches it
        self.assertEqual(smc.read("PDTR"), 62.5)
        self.assertEqual(smc.table["PDTR"][0], 3)

    def test_rescans_are_throttled_to_one_per_minute(self):
        now = [1000.0]
        io = FakeIO(list(KEYS))
        smc = SMC(io, None, clock=lambda: now[0])
        smc.open()                                        # fresh scan counts as the last rescan
        io.keys[2], io.keys[3] = io.keys[3], io.keys[2]
        before = io.selects
        self.assertIsNone(smc.read("PDTR"))               # mismatch within 60 s: no rescan
        self.assertEqual(io.selects - before, 1)
        now[0] += 60.0
        self.assertEqual(smc.read("PDTR"), 62.5)          # allowed again
        io.keys[2], io.keys[3] = io.keys[3], io.keys[2]
        now[0] += 30.0
        self.assertIsNone(smc.read("PDTR"))

    def test_hwmon_temps_and_fans_drop_invalid(self):
        d = self.tmp.name
        for n, (label, val) in enumerate([("TC0P", 61500), ("TA0P", -127000), ("TB0T", 151000), ("TG0D", 57000)], 1):
            helpers.write(f"{d}/temp{n}_input", f"{val}\n")
            helpers.write(f"{d}/temp{n}_label", f"{label}\n")
        helpers.write(f"{d}/fan1_input", "2167\n")
        helpers.write(f"{d}/fan1_label", "Left side\n")
        helpers.write(f"{d}/fan1_min", "2160\n")
        self.assertEqual(read_temps(d), {"TC0P": 61.5, "TG0D": 57.0})
        f = read_fans(d)[0]
        self.assertEqual((f["label"], f["rpm"], f["min"], f["max"]), ("Left side", 2167, 2160, None))


if __name__ == "__main__":
    unittest.main()
