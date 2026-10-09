"""Clean-shutdown marker and abrupt-boot incident capture (PLAN 4.5)."""
from __future__ import annotations

import glob
import gzip
import json
import os
import re
import subprocess
import time
from collections import deque
from typing import Any, Callable

from turbomac.daemon.paths import Paths
from turbomac.daemon.sysfs import ensure_group_dir, read_text

JournalRunner = Callable[[list[str]], "str | None"]
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def run_journal(args: list[str]) -> str | None:
    """journalctl output, or None if it fails or is missing."""
    try:
        r = subprocess.run(["journalctl", *args], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def previous_boot_id(runner: JournalRunner) -> str | None:
    out = runner(["--list-boots", "--no-pager", "-o", "json"])
    if not out:
        return None
    try:
        boots = json.loads(out)
    except ValueError:
        return None
    for b in boots if isinstance(boots, list) else []:
        if b.get("index", b.get("offset")) == -1:
            return b.get("boot_id")
    return None


def journal_clean(tail: str) -> bool:
    """A clean shutdown ends with journald's 'Journal stopped'."""
    return "Journal stopped" in "\n".join(tail.strip().splitlines()[-3:])


def mark_clean(paths: Paths) -> None:
    os.makedirs(paths.state, exist_ok=True)
    with open(paths.clean_marker, "w", encoding="utf-8") as fh:
        json.dump({"ts": round(time.time(), 3), "boot_id": read_text(paths.boot_id, quiet=True)}, fh)
        fh.flush()
        os.fsync(fh.fileno())


def _read_marker(path: str) -> dict[str, Any] | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            txt = fh.read()
    except OSError:
        return None
    try:
        data = json.loads(txt)
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def last_samples(samples_dir: str, n: int, exclude_boot: str | None) -> tuple[list[dict[str, Any]], str | None]:
    """Newest n sample records not from exclude_boot, plus the boot id they came from."""
    rows: list[dict[str, Any]] = []
    boot = None
    files: dict[str, str] = {}
    for f in glob.glob(os.path.join(samples_dir, "samples-*.jsonl.gz")):
        files[f[:-3]] = f
    for f in glob.glob(os.path.join(samples_dir, "samples-*.jsonl")):
        files[f] = f   # uncompressed wins if both exist (crash between rename and unlink)
    for base in sorted(files, reverse=True):
        f = files[base]
        try:
            opener = gzip.open if f.endswith(".gz") else open
            with opener(f, "rt", encoding="utf-8") as fh:
                head = json.loads(fh.readline() or "{}")
                tail = deque(fh, maxlen=n)
        except (OSError, ValueError, EOFError):
            continue
        fboot = head.get("boot_id") if head.get("kind") == "header" else None
        if exclude_boot and fboot == exclude_boot:
            continue
        if boot is not None and fboot != boot:
            break
        boot = fboot
        recs = []
        for line in tail:
            try:
                recs.append(json.loads(line))
            except ValueError:
                pass  # torn last line from a power loss
        rows = recs + rows
        if len(rows) >= n:
            break
    return rows[-n:], boot


def check_previous_boot(paths: Paths, boot_id: str | None, runner: JournalRunner = run_journal,
                        now: float | None = None) -> dict[str, Any]:
    """Run once per boot. Returns {"verdict": clean|abrupt|unknown|skipped, ...}."""
    now = time.time() if now is None else now
    marker = _read_marker(paths.clean_marker)
    if marker is not None:
        try:  # the marker only ever vouches for the boot that wrote it last
            os.unlink(paths.clean_marker)
        except OSError:
            pass
    if boot_id and read_text(paths.last_boot_checked, quiet=True) == boot_id:
        return {"verdict": "skipped", "reason": "previous boot already checked"}
    tail = runner(["-b", "-1", "-n", "20", "--no-pager", "-o", "cat"])
    tail = tail if tail and tail.strip() else None
    # The journal decides when it is available; the marker only fills in when it is not,
    # and only if it was written during the previous boot.
    if tail is None:
        prev_boot = previous_boot_id(runner)
        if marker is not None and prev_boot and marker.get("boot_id") == prev_boot:
            res: dict[str, Any] = {"verdict": "clean",
                                   "reason": "journal unavailable; clean-shutdown marker from the previous boot"}
        else:
            res = {"verdict": "unknown", "reason": "previous boot journal unavailable"}
    elif journal_clean(tail):
        res = {"verdict": "clean", "reason": "previous boot journal ends with 'Journal stopped'"}
    else:
        rows, sample_boot = last_samples(paths.samples_dir, 60, boot_id)
        prev = previous_boot_id(runner) or sample_boot
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
        inc = {"id": f"{stamp}-{(prev or 'unknown')[:8]}", "detected_at": stamp, "boot_id": prev,
               "current_boot_id": boot_id, "reason": "previous boot journal does not end in 'Journal stopped'",
               "marker_present": marker is not None,
               "last_sample_time": rows[-1].get("time") if rows else None,
               "samples": rows, "journal_tail": tail.strip().splitlines()[-20:]}
        ensure_group_dir(paths.incidents_dir)
        with open(os.path.join(paths.incidents_dir, inc["id"] + ".json"), "w", encoding="utf-8") as fh:
            json.dump(inc, fh, indent=1)
        res = {"verdict": "abrupt", "reason": inc["reason"], "incident_id": inc["id"]}
    try:
        os.makedirs(paths.state, exist_ok=True)
        if boot_id:
            with open(paths.last_boot_checked, "w", encoding="utf-8") as fh:
                fh.write(boot_id + "\n")
    except OSError:
        pass
    return res


def list_incidents(directory: str) -> list[dict[str, Any]]:
    out = []
    for f in sorted(glob.glob(os.path.join(directory, "*.json")), reverse=True):
        try:
            with open(f, "r", encoding="utf-8") as fh:
                inc = json.load(fh)
        except (OSError, ValueError):
            continue
        out.append({k: inc.get(k) for k in ("id", "detected_at", "boot_id", "last_sample_time", "reason")}
                   | {"n_samples": len(inc.get("samples") or [])})
    return out


def load_incident(directory: str, incident_id: str) -> dict[str, Any] | None:
    if not _ID_RE.match(incident_id or ""):
        return None
    try:
        with open(os.path.join(directory, incident_id + ".json"), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None
