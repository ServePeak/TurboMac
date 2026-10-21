import json
import os
import unittest

import helpers
from turbomac.daemon import incidents
from turbomac.daemon.logger import SampleLogger
from turbomac.daemon.paths import Paths

ABRUPT_TAIL = "\n".join(f"Oct 05 03:1{i % 10} jia-mac-pro kernel: line {i}" for i in range(25))
CLEAN_TAIL = "systemd[1]: Reached target poweroff.target.\nsystemd-shutdown[1]: Syncing filesystems\nsystemd-journald[412]: Journal stopped\n"
BOOTS = json.dumps([{"index": -1, "boot_id": "prevboot0123456789", "first_entry": 1, "last_entry": 2},
                    {"index": 0, "boot_id": "cur-boot-0000", "first_entry": 3, "last_entry": 4}])


def runner(tail, boots=BOOTS):
    calls = []

    def run(args):
        calls.append(args)
        return boots if "--list-boots" in args else tail
    run.calls = calls
    return run


class IncidentTest(unittest.TestCase):
    def setUp(self):
        self.fs = helpers.FakeSys()
        self.paths = Paths(sysroot=self.fs.root)
        self.boot = "cur-boot-0000"

    def tearDown(self):
        self.fs.cleanup()

    def write_samples(self, boot, n, start, torn=False):
        lg = SampleLogger(self.paths.samples_dir, boot_id=boot, background=False)
        for i in range(n):
            lg.write({"ts": start + i, "time": f"t{start + i}", "input_w": 30.0 + i % 5})
        lg.close()
        if torn:
            with open(lg.path, "a") as fh:
                fh.write('{"ts": 99, "inp')

    def put_marker(self, boot):
        os.makedirs(self.paths.state, exist_ok=True)
        with open(self.paths.clean_marker, "w") as fh:
            json.dump({"ts": 1, "boot_id": boot}, fh)

    def test_journal_wins_over_marker(self):
        self.put_marker("prevboot0123456789")
        res = incidents.check_previous_boot(self.paths, self.boot, runner(ABRUPT_TAIL))
        self.assertEqual(res["verdict"], "abrupt")
        self.assertTrue(incidents.load_incident(self.paths.incidents_dir, res["incident_id"])["marker_present"])
        self.assertFalse(os.path.exists(self.paths.clean_marker))
        self.assertEqual(helpers.read(self.paths.last_boot_checked), self.boot)

    def test_marker_counts_only_without_journal_and_only_for_the_previous_boot(self):
        self.put_marker("prevboot0123456789")
        res = incidents.check_previous_boot(self.paths, self.boot, runner(None))
        self.assertEqual(res["verdict"], "clean")
        os.unlink(self.paths.last_boot_checked)
        self.put_marker("someolderboot")
        res = incidents.check_previous_boot(self.paths, self.boot, runner(None))
        self.assertEqual(res["verdict"], "unknown")

    def test_clean_by_journal(self):
        res = incidents.check_previous_boot(self.paths, self.boot, runner(CLEAN_TAIL))
        self.assertEqual(res["verdict"], "clean")
        self.assertEqual(incidents.list_incidents(self.paths.incidents_dir), [])

    def test_abrupt_records_incident_with_last_60_samples_of_previous_boot(self):
        self.write_samples("prevboot0123456789", 50, 1000)
        self.write_samples("prevboot0123456789", 30, 5000, torn=True)   # hourly rotation, power cut mid-line
        names = sorted(os.listdir(self.paths.samples_dir))
        self.assertTrue(names[0].endswith(".jsonl.gz") and names[1].endswith(".jsonl"), names)
        run = runner(ABRUPT_TAIL)
        res = incidents.check_previous_boot(self.paths, self.boot, run, now=1_760_000_000)
        self.assertEqual(res["verdict"], "abrupt")
        inc = incidents.load_incident(self.paths.incidents_dir, res["incident_id"])
        self.assertEqual(inc["boot_id"], "prevboot0123456789")
        self.assertEqual(len(inc["samples"]), 60)
        self.assertEqual(inc["samples"][-1]["ts"], 5029)
        self.assertEqual(inc["samples"][0]["ts"], 1020)   # 30 rows came from the .gz file
        self.assertEqual(inc["last_sample_time"], "t5029")
        self.assertEqual(len(inc["journal_tail"]), 20)
        self.assertIn(["-b", "-1", "-n", "20", "--no-pager", "-o", "cat"], run.calls)
        listing = incidents.list_incidents(self.paths.incidents_dir)
        self.assertEqual((listing[0]["id"], listing[0]["n_samples"]), (res["incident_id"], 60))
        # Same boot again (daemon restart): no second incident.
        res2 = incidents.check_previous_boot(self.paths, self.boot, runner(ABRUPT_TAIL))
        self.assertEqual(res2["verdict"], "skipped")
        self.assertEqual(len(incidents.list_incidents(self.paths.incidents_dir)), 1)

    def test_samples_from_current_boot_are_ignored(self):
        self.write_samples(self.boot, 10, 9000)
        res = incidents.check_previous_boot(self.paths, self.boot, runner(ABRUPT_TAIL, boots=None))
        inc = incidents.load_incident(self.paths.incidents_dir, res["incident_id"])
        self.assertEqual((inc["samples"], inc["boot_id"]), ([], None))

    def test_journal_unavailable_is_unknown(self):
        res = incidents.check_previous_boot(self.paths, self.boot, lambda args: None)
        self.assertEqual(res["verdict"], "unknown")

    def test_marker_from_a_stop_earlier_this_boot_is_consumed(self):
        incidents.check_previous_boot(self.paths, self.boot, runner(CLEAN_TAIL))
        incidents.mark_clean(self.paths)                      # e.g. systemctl restart
        res = incidents.check_previous_boot(self.paths, self.boot, runner(CLEAN_TAIL))
        self.assertEqual(res["verdict"], "skipped")
        self.assertFalse(os.path.exists(self.paths.clean_marker))

    def test_incident_id_validation(self):
        self.assertIsNone(incidents.load_incident(self.paths.incidents_dir, "../../etc/passwd"))
        self.assertTrue(incidents.journal_clean(CLEAN_TAIL))
        self.assertFalse(incidents.journal_clean(ABRUPT_TAIL))


if __name__ == "__main__":
    unittest.main()
