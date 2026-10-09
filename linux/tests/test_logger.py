import gzip
import json
import os
import tempfile
import threading
import time
import unittest

import helpers  # noqa: F401
from turbomac.daemon.logger import QUEUE_MAX, SampleLogger, compress_file


class LoggerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def names(self):
        return sorted(os.listdir(self.dir))

    def test_rotation_compresses_previous_file_and_keeps_current_plain(self):
        lg = SampleLogger(self.dir, background=False, boot_id="b1")
        for ts in (1000, 1001, 5000):                 # 5000 is the next UTC hour
            lg.write({"ts": ts, "input_w": 30.0})
        lg.close()
        self.assertEqual(self.names(), ["samples-19700101T001640Z.jsonl.gz", "samples-19700101T012320Z.jsonl"])
        with gzip.open(os.path.join(self.dir, self.names()[0]), "rt") as fh:
            rows = [json.loads(x) for x in fh]
        self.assertEqual([r.get("ts") for r in rows], [1000, 1000, 1001])  # header + 2 samples
        self.assertEqual(rows[0]["boot_id"], "b1")

    def test_startup_compresses_leftover_files_in_background(self):
        old = os.path.join(self.dir, "samples-20261001T000000Z.jsonl")
        helpers.write(old, '{"kind":"header"}\n{"ts":1}\n')
        os.utime(old, (1_700_000_000, 1_700_000_000))
        lg = SampleLogger(self.dir, retention_days=100000)
        lg.join(5)
        self.assertEqual(self.names(), ["samples-20261001T000000Z.jsonl.gz"])
        self.assertEqual(int(os.stat(old + ".gz").st_mtime), 1_700_000_000)   # age survives compression

    def test_compress_failure_keeps_original(self):
        self.assertIsNone(compress_file(os.path.join(self.dir, "missing.jsonl")))
        self.assertEqual(self.names(), [])

    def test_prune_counts_gz_for_age_and_size(self):
        now = time.time()
        for i, (age_days, size) in enumerate([(20, 10), (3, 3000), (2, 3000), (1, 3000)]):
            f = os.path.join(self.dir, f"samples-2026100{i}T000000Z.jsonl.gz")
            helpers.write(f, b"x" * size)
            os.utime(f, (now - age_days * 86400, now - age_days * 86400))
        lg = SampleLogger(self.dir, retention_days=14, max_mib=7000 / 2**20, background=False)
        self.assertEqual(self.names(), ["samples-20261002T000000Z.jsonl.gz", "samples-20261003T000000Z.jsonl.gz"])
        lg.write({"ts": now, "input_w": 1.0})         # the current plain file is never pruned
        lg.max_mib = 0
        lg.prune()
        self.assertEqual(self.names(), [os.path.basename(lg.path)])
        lg.close()


    def read_ts(self):
        rows = []
        for n in self.names():
            with open(os.path.join(self.dir, n)) as fh:
                rows += [json.loads(x) for x in fh]
        return [r["ts"] for r in rows if r.get("kind") != "header"]

    def test_writer_thread_keeps_order_and_close_drains(self):
        now = time.time()
        lg = SampleLogger(self.dir)
        for i in range(50):
            lg.write({"ts": now + i * 0.001, "i": i})
        lg.close()
        self.assertEqual(self.read_ts(), [now + i * 0.001 for i in range(50)])

    def test_full_queue_drops_oldest_and_reports_once(self):
        events, gate, now = [], threading.Event(), time.time()
        lg = SampleLogger(self.dir, on_event=lambda typ, **kw: events.append(typ))
        real = lg._write_now

        def slow_disk(rec, wall):
            gate.wait(5)
            real(rec, wall)
        lg._write_now = slow_disk
        lg.write({"ts": now})                                   # the writer takes this one and blocks
        time.sleep(0.05)
        for i in range(1, QUEUE_MAX + 6):
            lg.write({"ts": now + i})
        self.assertEqual(events, ["log_backlog"])
        gate.set()
        lg.close()
        self.assertEqual(self.read_ts(), [now] + [now + i for i in range(6, QUEUE_MAX + 6)])


if __name__ == "__main__":
    unittest.main()
