import json
import os
import stat
import tempfile
import unittest

import helpers  # noqa: F401
from turbomac import config

PLAN_DEFAULTS = {
    "schema": 1,
    "governor": {"mode": "static", "static_cap_w": 35, "ceiling_w": 40, "floor_w": 10, "burst_w": 0,
                 "shed_ratio": 0.88, "emergency_ratio": 0.94, "recover_ratio": 0.78,
                 "cut_step_w": 5, "recover_step_w": 1, "recover_interval_s": 3, "stale_timeout_s": 2},
    "gpu": {"mode": "auto", "cap_w": 15},
    "cpu": {"no_turbo": None, "epp": None, "max_perf_pct": None},  # PLAN: false; null = untouched
    "logging": {"retention_days": 14, "max_mib": 1024},  # PLAN says 256; raised with gzip (audit batch 1)
}


class ConfigTest(unittest.TestCase):
    def test_defaults_match_plan(self):
        self.assertEqual(config.defaults(), PLAN_DEFAULTS)

    def test_bounds(self):
        ok = {"governor.static_cap_w": 10, "governor.floor_w": 20, "governor.burst_w": 5,
              "governor.cut_step_w": 15, "governor.recover_step_w": 1, "governor.recover_interval_s": 30,
              "governor.stale_timeout_s": 1, "gpu.cap_w": 5, "gpu.mode": "off", "cpu.epp": "power",
              "cpu.max_perf_pct": 20, "cpu.no_turbo": True, "governor.mode": "dynamic"}
        acc, rej = config.validate(ok)
        self.assertEqual((acc, rej), (ok, {}))
        self.assertEqual(config.validate({"logging.max_mib": 8192})[1], {})
        self.assertEqual(config.validate({"cpu.no_turbo": None}), ({"cpu.no_turbo": None}, {}))
        self.assertIn("16..8192", config.validate({"logging.max_mib": 8193})[1]["logging.max_mib"])
        bad = {"governor.static_cap_w": 9.9, "governor.floor_w": 8, "governor.burst_w": 6,
               "governor.cut_step_w": 0, "governor.recover_interval_s": 31, "governor.stale_timeout_s": 11,
               "gpu.cap_w": 36, "gpu.mode": "turbo", "cpu.epp": "max", "cpu.max_perf_pct": 19,
               "cpu.no_turbo": 1, "governor.ceiling_w": True, "logging.max_mib": 2.5,
               "governor.mode": "aggressive", "governor.shed_ratio": 0.96}
        acc, rej = config.validate(bad)
        self.assertEqual(acc, {})
        self.assertEqual(set(rej), set(bad))
        self.assertIn("10..45", rej["governor.static_cap_w"])

    def test_unknown_and_fixed_keys(self):
        acc, rej = config.validate({"governor.turbo": 1, "foo": 2, "governor.hard_max_w": 50})
        self.assertEqual(acc, {})
        self.assertEqual(rej["foo"], "unknown key")
        self.assertIn("fixed", rej["governor.hard_max_w"])

    def test_cross_field_rules(self):
        _, rej = config.validate({"governor.static_cap_w": 42})          # ceiling 40
        self.assertIn("ceiling", rej["governor.static_cap_w"])
        acc, rej = config.validate({"governor.static_cap_w": 42, "governor.ceiling_w": 44})
        self.assertEqual((len(acc), rej), (2, {}))
        _, rej = config.validate({"governor.ceiling_w": 30})              # below static 35
        self.assertIn("governor.ceiling_w", rej)
        _, rej = config.validate({"governor.recover_ratio": 0.84})        # shed 0.88 - 0.05
        self.assertIn("recover_ratio", rej["governor.recover_ratio"])
        acc, _ = config.validate({"governor.recover_ratio": 0.83})
        self.assertIn("governor.recover_ratio", acc)
        _, rej = config.validate({"governor.emergency_ratio": 0.90})
        self.assertIn("emergency_ratio", rej["governor.emergency_ratio"])
        acc, rej = config.validate({"governor.emergency_ratio": 0.91})    # exactly shed + 0.03
        self.assertEqual(rej, {})
        _, rej = config.validate({"governor.shed_ratio": 0.93})           # breaks emergency 0.94
        self.assertIn("governor.shed_ratio", rej)
        base = config.apply_updates(config.defaults(), {"governor.ceiling_w": 45})
        acc, rej = config.validate({"governor.static_cap_w": 45}, base)
        self.assertEqual(rej, {})

    def test_apply_updates_does_not_mutate(self):
        c = config.defaults()
        new = config.apply_updates(c, {"governor.ceiling_w": 38, "cpu.epp": "balance_power"})
        self.assertEqual((c["governor"]["ceiling_w"], new["governor"]["ceiling_w"]), (40, 38))
        self.assertEqual(config.get(new, "cpu.epp"), "balance_power")
        self.assertNotEqual(config.digest(c), config.digest(new))

    def test_load_and_atomic_save(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "etc", "config.json")
            self.assertEqual(config.load(p), (PLAN_DEFAULTS, {}))
            c = config.apply_updates(config.defaults(), {"governor.mode": "dynamic"})
            config.save(p, c)
            config.save(p, c)
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o644)
            self.assertEqual(os.listdir(os.path.dirname(p)), ["config.json"])
            self.assertEqual(config.load(p), (c, {}))
            with open(p, "w") as fh:
                json.dump({"schema": 1, "governor": {"ceiling_w": 99, "floor_w": 12}, "junk": 1}, fh)
            loaded, problems = config.load(p)
            self.assertEqual(loaded["governor"]["ceiling_w"], 40)
            self.assertEqual(loaded["governor"]["floor_w"], 12)
            self.assertEqual(set(problems), {"governor.ceiling_w", "junk"})
            with open(p, "w") as fh:
                fh.write("{broken")
            loaded, problems = config.load(p)
            self.assertEqual((loaded, list(problems)), (PLAN_DEFAULTS, ["<file>"]))


if __name__ == "__main__":
    unittest.main()
