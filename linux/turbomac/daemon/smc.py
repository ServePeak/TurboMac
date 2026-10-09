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
# Every applesmc read busy-waits in the kernel (~0.23 ms CPU per SMC transaction): slow-changing SMC
# values (temperatures, the extra rails, fan min/max/manual) are refreshed only this often.
SMC_SLOW_EVERY_S = 3.0
DUE_SLACK_S = 0.1   # loop jitter: a 1 Hz reader is due on every 3rd slow tick, not sometimes the 4th


def due(last: float | None, now: float, every_s: float) -> bool:
    """True when a value read at `last` should be read again at `now` (always when every_s <= 0)."""
    return last is None or not (0 <= now - last < every_s - DUE_SLACK_S)


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


# The applesmc sensor files do not change while the daemon runs: each base directory is globbed
# once (an empty result is retried) and its label text cached; the values are read every call.
_TEMP_SENSORS: dict[str, list[tuple[str, str]]] = {}                    # base -> [(temp*_input, label)]
_FAN_SENSORS: dict[str, list[tuple[str, str | None, str, str, str]]] = {}  # base -> [(input, label, min, max, manual)]
_FAN_LIMITS: dict[str, tuple[float, list[tuple[int | None, ...]]]] = {}   # base -> (read at, [(min, max, manual)])


def clear_caches() -> None:
    """Forget the discovered sensor files and the cached fan min/max/manual (tests)."""
    _TEMP_SENSORS.clear()
    _FAN_SENSORS.clear()
    _FAN_LIMITS.clear()


def _temp_sensors(base: str) -> list[tuple[str, str]]:
    found = _TEMP_SENSORS.get(base)
    if found is None:
        found = []
        for p in sorted(glob.glob(os.path.join(base, "temp*_input")), key=_idx):
            n = _idx(p)
            found.append((p, read_text(os.path.join(base, f"temp{n}_label"), f"temp{n}", quiet=True) or f"temp{n}"))
        if found:
            _TEMP_SENSORS[base] = found
    return found


def _fan_sensors(base: str) -> list[tuple[str, str | None, str, str, str]]:
    found = _FAN_SENSORS.get(base)
    if found is None:
        found = []
        for p in sorted(glob.glob(os.path.join(base, "fan*_input")), key=_idx):
            n = _idx(p)
            f = lambda s: os.path.join(base, f"fan{n}_{s}")  # noqa: E731
            found.append((p, read_text(f("label"), f"fan{n}", quiet=True), f("min"), f("max"), f("manual")))
        if found:
            _FAN_SENSORS[base] = found
    return found


def read_temps(base: str | None) -> dict[str, float]:
    """applesmc temp*_input by label, invalid values (<= -100 or >= 150 C) dropped."""
    out: dict[str, float] = {}
    if not base:
        return out
    for p, label in _temp_sensors(base):
        v = read_int(p, quiet=True)
        if v is None or not (-100.0 < v / 1000.0 < 150.0):
            continue
        out[label] = round(v / 1000.0, 1)
    return out


def read_fans(base: str | None, now: float | None = None, every_s: float = SMC_SLOW_EVERY_S) -> list[dict[str, Any]]:
    """fan*_input every call; the label (cached once) and min/max/manual, re-read at most every every_s
    of `now` (default: the monotonic clock). The fan floor's own read-back of fan*_min is in fans.py."""
    out: list[dict[str, Any]] = []
    if not base:
        return out
    sensors = _fan_sensors(base)
    now = time.monotonic() if now is None else now
    cached = _FAN_LIMITS.get(base)
    if cached is None or len(cached[1]) != len(sensors) or due(cached[0], now, every_s):
        limits = [(read_int(mn, quiet=True), read_int(mx, quiet=True), read_int(manual, quiet=True))
                  for _, _, mn, mx, manual in sensors]
        _FAN_LIMITS[base] = (now, limits)
    else:
        limits = cached[1]
    for (p, label, *_), (mn, mx, manual) in zip(sensors, limits):
        out.append({"label": label, "rpm": read_int(p, quiet=True), "min": mn, "max": mx, "manual": manual})
    return out
