"""Tiny sysfs helpers: never raise on missing files, warn once per path."""
from __future__ import annotations

import grp
import os
import sys
import threading

_warned: set[str] = set()
_lock = threading.Lock()


def warn_once(key: str, msg: str) -> None:
    with _lock:
        if key in _warned:
            return
        _warned.add(key)
    print(f"turbomacd: {msg}", file=sys.stderr, flush=True)


def read_text(path: str, default: str | None = None, quiet: bool = False) -> str | None:
    try:
        with open(path, "r", encoding="ascii", errors="replace") as fh:
            return fh.read().strip()
    except OSError as exc:
        if not quiet:
            warn_once(path, f"cannot read {path}: {exc.strerror or exc}")
        return default


def read_int(path: str, default: int | None = None, quiet: bool = False) -> int | None:
    txt = read_text(path, None, quiet)
    try:
        return int(txt) if txt is not None else default
    except ValueError:
        return default


def write_text(path: str, value: str) -> str | None:
    """Write a value; return None on success, else the error text."""
    try:
        with open(path, "w", encoding="ascii") as fh:
            fh.write(value)
        return None
    except OSError as exc:
        return f"write {path}: {exc.strerror or exc}"


def ensure_group_dir(path: str, mode: int = 0o750, group: str = "turbomac") -> None:
    """mkdir -p, chmod mode, chgrp group when it exists (failures ignored)."""
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, mode)
    except OSError:
        pass
    try:
        os.chown(path, -1, grp.getgrnam(group).gr_gid)
    except (KeyError, OSError):
        pass
