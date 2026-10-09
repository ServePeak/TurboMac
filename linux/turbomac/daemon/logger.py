"""1 Hz JSONL sample log with hourly rotation and retention; journal events (PLAN 4.5)."""
from __future__ import annotations

import glob
import gzip
import json
import os
import queue
import shutil
import sys
import threading
import time
from collections import deque
from typing import Any, Callable, TextIO

from turbomac.daemon.sysfs import ensure_group_dir

EVENT_PREFIX = "TURBOMAC_EVENT "
QUEUE_MAX = 120   # records (seconds) buffered for the writer thread


class EventLog:
    """Recent events in memory, each also printed to stderr for the journal."""

    def __init__(self, maxlen: int = 1000, stream: TextIO | None = None) -> None:
        self.events: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self.stream = stream
        self.lock = threading.Lock()

    def emit(self, typ: str, **fields: Any) -> dict[str, Any]:
        rec = {"ts": round(time.time(), 3), "type": typ, **fields}
        with self.lock:
            self.events.append(rec)
        try:
            print(EVENT_PREFIX + json.dumps(rec, default=str), file=self.stream or sys.stderr, flush=True)
        except (OSError, ValueError):
            pass
        return rec

    def recent(self, n: int = 100) -> list[dict[str, Any]]:
        with self.lock:
            return list(self.events)[-max(0, n):] if n else []


def compress_file(path: str) -> str | None:
    """gzip path to path.gz (tmp, fsync, rename), then delete the original. Keeps mtime."""
    gz, tmp = path + ".gz", path + ".gz.tmp"
    try:
        st = os.stat(path)
        with open(path, "rb") as src, open(tmp, "wb") as raw:
            with gzip.GzipFile(filename=os.path.basename(path), mode="wb", fileobj=raw, mtime=int(st.st_mtime)) as z:
                shutil.copyfileobj(src, z, 1 << 20)
            raw.flush()
            os.fsync(raw.fileno())
        os.utime(tmp, (st.st_atime, st.st_mtime))
        os.replace(tmp, gz)
        dfd = os.open(os.path.dirname(gz) or ".", os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
        os.unlink(path)
        return gz
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return None


class SampleLogger:
    """samples-YYYYMMDDTHHMMSSZ.jsonl, one fsynced line per second, new file each UTC hour.

    With background=True the control loop only enqueues: a writer thread does
    the write + fsync per line, and a housekeeping thread gzips finished files
    and prunes. background=False does everything inline (tests).
    """

    def __init__(self, directory: str, retention_days: float = 14, max_mib: float = 1024,
                 boot_id: str | None = None, background: bool = True,
                 on_event: Callable[..., Any] | None = None) -> None:
        self.dir = directory
        self.retention_days = retention_days
        self.max_mib = max_mib
        self.boot_id = boot_id
        self.background = background
        self.on_event = on_event or (lambda *a, **k: None)
        self.fh: TextIO | None = None
        self.path: str | None = None
        self.hour: str | None = None
        self._worker: threading.Thread | None = None
        self._again = False
        self.q: queue.Queue[tuple[dict[str, Any], float | None] | None] = queue.Queue(maxsize=QUEUE_MAX)
        self._backlog = False
        self._write_error = False
        ensure_group_dir(directory)
        self._writer: threading.Thread | None = None
        if background:
            self._writer = threading.Thread(target=self._writer_loop, name="log-writer", daemon=True)
            self._writer.start()
        self.housekeep()

    def _open(self, wall: float) -> None:
        if self.fh is not None:
            self.fh.close()
            self.fh = None
        name = time.strftime("samples-%Y%m%dT%H%M%SZ.jsonl", time.gmtime(wall))
        self.path = os.path.join(self.dir, name)
        self.fh = open(self.path, "a", encoding="utf-8")
        self.hour = time.strftime("%Y%m%d%H", time.gmtime(wall))
        self._line({"kind": "header", "boot_id": self.boot_id, "ts": round(wall, 3)})
        self.housekeep()

    def _line(self, rec: dict[str, Any]) -> None:
        assert self.fh is not None
        self.fh.write(json.dumps(rec, separators=(",", ":"), default=str) + "\n")
        self.fh.flush()
        os.fsync(self.fh.fileno())

    def _write_now(self, rec: dict[str, Any], wall: float | None) -> None:
        wall = rec.get("ts", time.time()) if wall is None else wall
        if self.fh is None or time.strftime("%Y%m%d%H", time.gmtime(wall)) != self.hour:
            self._open(wall)
        self._line(rec)

    def write(self, rec: dict[str, Any], wall: float | None = None) -> None:
        """Enqueue one record (never blocks). Full queue: drop the oldest, report once."""
        if not self.background:
            self._write_now(rec, wall)
            return
        try:
            self.q.put_nowait((rec, wall))
            return
        except queue.Full:
            pass
        try:
            self.q.get_nowait()
            self.q.task_done()
        except queue.Empty:
            pass
        if not self._backlog:
            self._backlog = True
            self.on_event("log_backlog", queued=QUEUE_MAX, message="sample writer is behind; dropping oldest")
        try:
            self.q.put_nowait((rec, wall))
        except queue.Full:
            pass

    def _writer_loop(self) -> None:
        while True:
            item = self.q.get()
            if item is None:
                self.q.task_done()
                return
            try:
                self._write_now(*item)
                self._write_error = False
            except (OSError, ValueError) as exc:
                if not self._write_error:
                    self.on_event("log_error", error=str(exc))
                self._write_error = True
            finally:
                self.q.task_done()
            if self._backlog and self.q.qsize() < QUEUE_MAX // 2:
                self._backlog = False

    def drain(self, timeout: float = 2.0) -> bool:
        """Wait until every queued record is written (or timeout). True if drained."""
        end = time.monotonic() + timeout
        while self.q.unfinished_tasks and time.monotonic() < end:
            time.sleep(0.005)
        return not self.q.unfinished_tasks

    def housekeep(self) -> None:
        """Compress every finished .jsonl, then prune. Runs on the worker thread."""
        if not self.background:
            self._housekeep_now()
            return
        if self._worker is not None and self._worker.is_alive():
            self._again = True
            return
        self._worker = threading.Thread(target=self._housekeep_loop, name="log-housekeep", daemon=True)
        self._worker.start()

    def _housekeep_loop(self) -> None:
        while True:
            self._again = False
            self._housekeep_now()
            if not self._again:
                return

    def _housekeep_now(self) -> None:
        for f in sorted(glob.glob(os.path.join(self.dir, "samples-*.jsonl"))):
            if f != self.path:
                compress_file(f)
        self.prune()

    def join(self, timeout: float | None = None) -> None:
        if self._worker is not None:
            self._worker.join(timeout)

    def prune(self, now: float | None = None) -> list[str]:
        """Drop .jsonl/.jsonl.gz older than retention_days, then oldest until under max_mib."""
        now = time.time() if now is None else now
        removed = []
        files = sorted(glob.glob(os.path.join(self.dir, "samples-*.jsonl"))
                       + glob.glob(os.path.join(self.dir, "samples-*.jsonl.gz")))
        keep = []
        for f in files:
            try:
                st = os.stat(f)
            except OSError:
                continue
            if f != self.path and now - st.st_mtime > self.retention_days * 86400:
                removed.append(f)
            else:
                keep.append((f, st.st_size))
        total = sum(sz for _, sz in keep)
        for f, sz in keep:
            if total <= self.max_mib * 1024 * 1024 or f == self.path:
                continue
            removed.append(f)
            total -= sz
        for f in removed:
            try:
                os.unlink(f)
            except OSError:
                pass
        return removed

    def close(self, timeout: float = 2.0) -> None:
        """Drain queued records for up to timeout seconds, then close the file."""
        if self._writer is not None and self._writer.is_alive():
            end = time.monotonic() + timeout
            try:
                self.q.put(None, timeout=timeout)       # wait for room rather than drop data
            except queue.Full:
                try:                                    # writer stuck: give up the oldest record
                    self.q.get_nowait()
                    self.q.task_done()
                    self.q.put_nowait(None)
                except (queue.Empty, queue.Full):
                    pass
            self._writer.join(max(0.0, end - time.monotonic()))
        if self.fh is not None:
            try:
                self.fh.close()
            except (OSError, ValueError):
                pass
            finally:
                self.fh = None
