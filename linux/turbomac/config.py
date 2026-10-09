"""Config defaults, validation, dotted-key updates and atomic save (PLAN 4.2, 4.7)."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
from typing import Any

SCHEMA = 1
HARD_MAX_W = 45.0
EPP_CHOICES = ("default", "performance", "balance_performance", "balance_power", "power")
_EPS = 1e-9

DEFAULTS: dict[str, Any] = {
    "schema": SCHEMA,
    "governor": {
        "mode": "static",
        "static_cap_w": 35, "ceiling_w": 40, "floor_w": 10, "burst_w": 0,
        "shed_ratio": 0.88, "emergency_ratio": 0.94, "recover_ratio": 0.78,
        "cut_step_w": 5, "recover_step_w": 1, "recover_interval_s": 3,
        "stale_timeout_s": 2,
    },
    "gpu": {"mode": "auto", "cap_w": 15},
    "fan": {
        "mode": "floor",
        "power_floor_w": 25, "power_floor_rpm": 3500,
        "t1_c": 78, "t1_rpm": 3500, "t2_c": 84, "t2_rpm": 4500, "t3_c": 90, "t3_rpm": 5500,
        "hysteresis_c": 6, "step_down_interval_s": 20, "max_rpm": 5500,
    },
    "cpu": {"no_turbo": None, "epp": None, "max_perf_pct": None},
    "logging": {"retention_days": 14, "max_mib": 1024},
}

# key -> (kind, arg). kinds: choice, num, int, bool; "?" suffix allows None.
SPEC: dict[str, tuple[str, Any]] = {
    "governor.mode": ("choice", ("off", "static", "dynamic")),
    "governor.static_cap_w": ("num", (10, 45)),
    "governor.ceiling_w": ("num", (10, 45)),
    "governor.floor_w": ("num", (9, 20)),
    "governor.burst_w": ("num", (0, 5)),
    "governor.shed_ratio": ("num", (0.70, 0.95)),
    "governor.emergency_ratio": ("num", (0.73, 0.98)),
    "governor.recover_ratio": ("num", (0.50, 0.90)),
    "governor.cut_step_w": ("num", (1, 15)),
    "governor.recover_step_w": ("num", (1, 5)),
    "governor.recover_interval_s": ("num", (1, 30)),
    "governor.stale_timeout_s": ("num", (1, 10)),
    "gpu.mode": ("choice", ("auto", "low", "off")),
    "gpu.cap_w": ("num", (5, 35)),
    "fan.mode": ("choice", ("auto", "floor")),
    "fan.power_floor_w": ("num", (10, 45)),
    "fan.power_floor_rpm": ("int", (2000, 5900)),
    "fan.t1_c": ("num", (50, 99)),
    "fan.t1_rpm": ("int", (2000, 5900)),
    "fan.t2_c": ("num", (50, 99)),
    "fan.t2_rpm": ("int", (2000, 5900)),
    "fan.t3_c": ("num", (50, 99)),
    "fan.t3_rpm": ("int", (2000, 5900)),
    "fan.hysteresis_c": ("num", (2, 15)),
    "fan.step_down_interval_s": ("num", (5, 120)),
    "fan.max_rpm": ("int", (2500, 5900)),
    "cpu.no_turbo": ("bool?", None),
    "cpu.epp": ("choice?", EPP_CHOICES),
    "cpu.max_perf_pct": ("int?", (20, 100)),
    "logging.retention_days": ("int", (1, 365)),
    "logging.max_mib": ("int", (16, 8192)),
}

# (keys involved, predicate on merged flat config, reason)
CROSS_RULES = [
    (("governor.static_cap_w", "governor.ceiling_w"),
     lambda c: c["governor.static_cap_w"] <= c["governor.ceiling_w"] + _EPS,
     "static_cap_w must be <= ceiling_w"),
    (("governor.recover_ratio", "governor.shed_ratio"),
     lambda c: c["governor.recover_ratio"] <= c["governor.shed_ratio"] - 0.05 + _EPS,
     "recover_ratio must be <= shed_ratio - 0.05"),
    (("governor.emergency_ratio", "governor.shed_ratio"),
     lambda c: c["governor.emergency_ratio"] >= c["governor.shed_ratio"] + 0.03 - _EPS,
     "emergency_ratio must be >= shed_ratio + 0.03"),
    (("fan.t1_c", "fan.t2_c"), lambda c: c["fan.t1_c"] < c["fan.t2_c"] - _EPS, "t1_c must be < t2_c"),
    (("fan.t2_c", "fan.t3_c"), lambda c: c["fan.t2_c"] < c["fan.t3_c"] - _EPS, "t2_c must be < t3_c"),
    (("fan.t1_rpm", "fan.t2_rpm"), lambda c: c["fan.t1_rpm"] <= c["fan.t2_rpm"], "t1_rpm must be <= t2_rpm"),
    (("fan.t2_rpm", "fan.t3_rpm"), lambda c: c["fan.t2_rpm"] <= c["fan.t3_rpm"], "t2_rpm must be <= t3_rpm"),
]


def defaults() -> dict[str, Any]:
    """A fresh deep copy of the defaults."""
    return copy.deepcopy(DEFAULTS)


def flatten(cfg: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Nested dict -> {"section.key": value}."""
    out: dict[str, Any] = {}
    for k, v in cfg.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(flatten(v, key + "."))
        else:
            out[key] = v
    return out


def get(cfg: dict[str, Any], dotted: str) -> Any:
    node: Any = cfg
    for part in dotted.split("."):
        node = node[part]
    return node


def _check_one(key: str, value: Any) -> tuple[Any, str | None]:
    """Normalize one value; return (value, error_or_None)."""
    if key == "schema":
        return value, None if value == SCHEMA else f"schema must be {SCHEMA}"
    if key == "governor.hard_max_w":
        return value, f"hard_max_w is fixed at {HARD_MAX_W:g} W"
    if key not in SPEC:
        return value, "unknown key"
    kind, arg = SPEC[key]
    if kind.endswith("?"):
        if value is None:
            return None, None
        kind = kind[:-1]
    if kind == "choice":
        return (value, None) if value in arg else (value, f"must be one of {', '.join(arg)}")
    if kind == "bool":
        return (value, None) if isinstance(value, bool) else (value, "must be true or false")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return value, "must be a number"
    lo, hi = arg
    if kind == "int":
        if float(value) != int(value):
            return value, "must be an integer"
        value = int(value)
    if not (lo - _EPS <= value <= hi + _EPS):
        return value, f"out of range {lo:g}..{hi:g}"
    return value, None


def validate(updates: dict[str, Any], base: dict[str, Any] | None = None
             ) -> tuple[dict[str, Any], dict[str, str]]:
    """Validate dotted-key updates against base (defaults if None).

    Returns (accepted, rejected_with_reasons). Cross-field rules are checked on
    the merged result; a failing rule rejects the updated keys it involves.
    """
    flat_base = flatten(base if base is not None else DEFAULTS)
    accepted: dict[str, Any] = {}
    rejected: dict[str, str] = {}
    for key, value in updates.items():
        norm, err = _check_one(key, value)
        if err:
            rejected[key] = err
        elif key != "schema":
            accepted[key] = norm
    for _ in range(len(CROSS_RULES) + 1):
        merged = {**flat_base, **accepted}
        broken = False
        for keys, ok, reason in CROSS_RULES:
            if ok(merged):
                continue
            culprits = [k for k in keys if k in accepted] or list(keys)
            for k in culprits:
                accepted.pop(k, None)
                rejected.setdefault(k, reason)
            broken = True
        if not broken:
            break
    return accepted, rejected


def apply_updates(cfg: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of cfg with already-validated dotted-key updates applied."""
    out = copy.deepcopy(cfg)
    for dotted, value in updates.items():
        node = out
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return out


def from_dict(raw: Any) -> tuple[dict[str, Any], dict[str, str]]:
    """Build a full config from a (possibly partial or bad) nested dict."""
    if not isinstance(raw, dict):
        return defaults(), {"<file>": "top level must be an object"}
    accepted, rejected = validate(flatten(raw))
    return apply_updates(defaults(), accepted), rejected


def load(path: str) -> tuple[dict[str, Any], dict[str, str]]:
    """Load config; missing file gives defaults. Returns (config, problems)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return defaults(), {}
    except (OSError, ValueError) as exc:
        return defaults(), {"<file>": f"unreadable, using defaults: {exc}"}
    return from_dict(raw)


def save(path: str, cfg: dict[str, Any]) -> None:
    """Atomic write: temp file in the same dir, fsync, rename, mode 0644."""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".config-", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2, sort_keys=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        dfd = os.open(d, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def digest(cfg: dict[str, Any]) -> str:
    """Short stable hash of the config, for status replies."""
    return hashlib.sha1(json.dumps(cfg, sort_keys=True).encode()).hexdigest()[:10]
