"""applesmc key reader through the global key_at_index selector.

The selector is process-global kernel state: the daemon is the only process
allowed to touch it, and every access goes through SMC.lock.
"""
from __future__ import annotations

import glob
import json
import os
import re
import struct
import threading
import time
from typing import Any, Callable

from turbomac.daemon.sysfs import read_int, read_text, warn_once, write_text

RESCAN_MIN_INTERVAL_S = 60.0   # a full scan takes seconds; a competing reader must not trigger one per read


def decode(typ: str, data: bytes) -> Any:
    """Decode raw SMC bytes by SMC type code."""
    t = typ.strip()
    if t == "flt" and len(data) >= 4:
        return struct.unpack("<f", data[:4])[0]
    if t in ("ui8", "ui16", "ui32", "ui64"):
        return int.from_bytes(data, "big", signed=False)
    if t in ("si8", "si16", "si32", "si64"):
        return int.from_bytes(data, "big", signed=True)
    if t == "sp78" and len(data) >= 2:
        return int.from_bytes(data[:2], "big", signed=True) / 256.0
    if t == "fpe2" and len(data) >= 2:
        return int.from_bytes(data[:2], "big", signed=False) / 4.0
    if len(t) == 4 and t[:2] in ("fp", "sp") and len(data) >= 2:
        try:
            frac = int(t[3], 16)
        except ValueError:
            return data.hex()
        return int.from_bytes(data[:2], "big", signed=t[:2] == "sp") / float(1 << frac)
    if t == "flag":
        return bool(data and data[0])
    return data.hex()


class SysfsSmcIO:
    """Raw access to the applesmc key_at_index files."""

    def __init__(self, base: str) -> None:
        self.base = base

    def _p(self, name: str) -> str:
        return os.path.join(self.base, name)

    def key_count(self) -> int:
        return read_int(self._p("key_count"), 0) or 0

    def get_selector(self) -> str | None:
        return read_text(self._p("key_at_index"))

    def select(self, index: int | str) -> None:
        err = write_text(self._p("key_at_index"), str(index))
        if err:
            raise OSError(err)

    def name(self) -> str:
        return read_text(self._p("key_at_index_name"), "") or ""

    def type(self) -> str:
        return read_text(self._p("key_at_index_type"), "") or ""

    def length(self) -> int:
        return read_int(self._p("key_at_index_data_length"), 0) or 0

    def data(self) -> bytes:
        with open(self._p("key_at_index_data"), "rb") as fh:
            return fh.read()


class SMC:
    """Name -> (index, type, length) table built once; reads verify the selector."""

    def __init__(self, io: Any, cache_path: str | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.io = io
        self.cache_path = cache_path
        self.clock = clock
        self.last_scan: float | None = None
        self.lock = threading.Lock()
        self.table: dict[str, tuple[int, str, int]] = {}
        self.original: str | None = None
        self.ok = False

    def open(self) -> bool:
        with self.lock:
            try:
                self.original = self.io.get_selector()
                count = self.io.key_count()
                if not self._load_cache(count):
                    self._scan(count)
                self.ok = bool(self.table)
            except OSError as exc:
                warn_once("smc-open", f"SMC unavailable: {exc}")
                self.ok = False
        return self.ok

    def _load_cache(self, count: int) -> bool:
        if not self.cache_path:
            return False
        try:
            with open(self.cache_path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if raw.get("key_count") != count or not raw.get("keys"):
                return False
            self.table = {k: (int(v[0]), str(v[1]), int(v[2])) for k, v in raw["keys"].items()}
            return True
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            return False

    def _scan(self, count: int) -> None:
        self.last_scan = self.clock()
        table: dict[str, tuple[int, str, int]] = {}
        for i in range(count):
            self.io.select(i)
            name = self.io.name()
            if name:
                table[name] = (i, self.io.type(), self.io.length())
        self.table = table
        if self.cache_path and table:
            try:
                os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
                tmp = self.cache_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump({"key_count": count, "keys": table}, fh)
                os.replace(tmp, self.cache_path)
            except OSError as exc:
                warn_once("smc-cache", f"cannot write SMC key cache: {exc}")

    def _raw_locked(self, key: str) -> tuple[str, bytes] | None:
        entry = self.table.get(key)
        if entry is None:
            return None
        index, typ, length = entry
        self.io.select(index)
        if self.io.name() != key:
            return None
        data = self.io.data()
        return typ, data[:length] if length else data

    def raw(self, key: str) -> tuple[str, bytes] | None:
        """Raw (type, bytes) for key, or None. A selector mismatch triggers a rescan,
        at most once per RESCAN_MIN_INTERVAL_S; in between it just returns None."""
        with self.lock:
            try:
                got = self._raw_locked(key)
                if got is None and key in self.table:
                    if self.last_scan is not None and self.clock() - self.last_scan < RESCAN_MIN_INTERVAL_S:
                        return None
                    warn_once(f"smc-rescan-{key}", f"SMC key {key} moved; rescanning key table")
                    self._scan(self.io.key_count())
                    got = self._raw_locked(key)
                return got
            except OSError as exc:
                warn_once(f"smc-read-{key}", f"SMC read {key} failed: {exc}")
                return None

    def read(self, key: str) -> Any:
        got = self.raw(key)
        return decode(*got) if got else None

    def read_many(self, keys: list[str]) -> dict[str, Any]:
        return {k: self.read(k) for k in keys}

    def has(self, key: str) -> bool:
        return key in self.table

    def keys_info(self) -> list[dict[str, Any]]:
        return [{"name": k, "index": v[0], "type": v[1], "len": v[2]} for k, v in sorted(self.table.items())]

    def restore(self) -> None:
        """Put the selector back to the value found at open()."""
        with self.lock:
            if self.original is not None:
                try:
                    self.io.select(self.original)
                except OSError:
                    pass


_NUM = re.compile(r"(\d+)")


def _idx(path: str) -> int:
    m = _NUM.search(os.path.basename(path))
    return int(m.group(1)) if m else 0


def read_temps(base: str | None) -> dict[str, float]:
    """applesmc temp*_input by label, invalid values (<= -100 or >= 150 C) dropped."""
    out: dict[str, float] = {}
    if not base:
        return out
    for p in sorted(glob.glob(os.path.join(base, "temp*_input")), key=_idx):
        v = read_int(p, quiet=True)
        if v is None or not (-100.0 < v / 1000.0 < 150.0):
            continue
        n = _idx(p)
        label = read_text(os.path.join(base, f"temp{n}_label"), f"temp{n}", quiet=True) or f"temp{n}"
        out[label] = round(v / 1000.0, 1)
    return out


def read_fans(base: str | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not base:
        return out
    for p in sorted(glob.glob(os.path.join(base, "fan*_input")), key=_idx):
        n = _idx(p)
        f = lambda s: os.path.join(base, f"fan{n}_{s}")  # noqa: E731
        out.append({"label": read_text(f("label"), f"fan{n}", quiet=True), "rpm": read_int(p, quiet=True),
                    "min": read_int(f("min"), quiet=True), "max": read_int(f("max"), quiet=True),
                    "manual": read_int(f("manual"), quiet=True)})
    return out
