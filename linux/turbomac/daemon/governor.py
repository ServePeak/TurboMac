"""Pure governor (PLAN 4.2): step(state, sample, config, now) -> (new_state, actions).

No I/O here. Actions are dicts applied by the daemon:
  {"op": "set_limits", "pl1_w", "pl2_w", "reason"}   and   {"op": "event", "type", ...}
The daemon reports the readback of the last write in the next sample as
sample["readback"] = {"ok": bool, "reason": str}.
"""
from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass, field
from typing import Any

from turbomac.config import HARD_MAX_W

NORMAL, TRIM, EMERGENCY, FALLBACK, STATIC, OFF = "NORMAL", "TRIM", "EMERGENCY", "FALLBACK", "STATIC", "OFF"
REASSERT_S = 10.0
CUT_MIN_INTERVAL_S = 0.5
EMERGENCY_HOLD_S = 5.0
BACKOFF_MAX_S = 8.0
INPUT_MODEL = (8.0, 1.24)  # input W ~= a + b * package W (staircase slope; intercept refit with GPU off + fan floor)


@dataclass
class GovState:
    state: str = OFF
    since: float = 0.0
    started: float = 0.0
    pl1_w: float | None = None          # last commanded limits
    pl2_w: float | None = None
    last_write: float = -math.inf
    last_cut: float = -math.inf
    last_valid: float | None = None
    last_step: float | None = None
    below_shed_since: float | None = None
    below_recover_since: float | None = None
    fallback_reason: str | None = None
    retry_at: float | None = None
    backoff_s: float = 0.0
    fw_pl1_w: float | None = None
    fw_pl2_w: float | None = None
    last_action: str | None = None
    counters: dict[str, int] = field(default_factory=lambda: {
        "cuts": 0, "emergencies": 0, "fallbacks": 0, "recoveries": 0, "writes": 0, "readback_failures": 0})
    time_in_state: dict[str, float] = field(default_factory=dict)


def initial(now: float, firmware: dict[str, Any] | None = None) -> GovState:
    fw = firmware or {}
    return GovState(since=now, started=now, fw_pl1_w=fw.get("pl1_w"), fw_pl2_w=fw.get("pl2_w"))


def thresholds(acpw_w: float | None, g: dict[str, Any]) -> dict[str, float] | None:
    if not acpw_w:
        return None
    return {"shed_w": acpw_w * g["shed_ratio"], "emergency_w": acpw_w * g["emergency_ratio"],
            "recover_w": acpw_w * g["recover_ratio"]}


def predicted_input_w(package_w: float) -> float:
    return INPUT_MODEL[0] + INPUT_MODEL[1] * package_w


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


class _Step:
    """Mutable working copy for one step; the input state is never mutated."""

    def __init__(self, state: GovState, g: dict[str, Any], now: float) -> None:
        self.s = dataclasses.replace(state, counters=dict(state.counters), time_in_state=dict(state.time_in_state))
        self.g, self.now = g, now
        self.actions: list[dict[str, Any]] = []

    def event(self, typ: str, **kw: Any) -> None:
        self.actions.append({"op": "event", "type": typ, **kw})

    def enter(self, new: str, **kw: Any) -> None:
        s = self.s
        if new != s.state:
            self.event("state", **{"from": s.state, "to": new, **kw})
            s.state, s.since = new, self.now

    def cap1(self, w: float) -> float:
        return min(w, HARD_MAX_W, self.s.fw_pl1_w or HARD_MAX_W)

    def write(self, pl1: float, pl2: float, reason: str, force: bool = False) -> None:
        s = self.s
        pl1 = self.cap1(pl1)
        pl2 = min(pl2, HARD_MAX_W, s.fw_pl2_w or HARD_MAX_W)
        due = self.now - s.last_write >= REASSERT_S
        if not force and not due and (pl1, pl2) == (s.pl1_w, s.pl2_w):
            return
        if (pl1, pl2) == (s.pl1_w, s.pl2_w) and not force:
            reason = "reassert"
        self.actions.append({"op": "set_limits", "pl1_w": pl1, "pl2_w": pl2, "reason": reason})
        s.pl1_w, s.pl2_w, s.last_write = pl1, pl2, self.now
        s.counters["writes"] += 1
        s.last_action = f"{reason}: PL1 {pl1:g} W PL2 {pl2:g} W"

    def fallback_w(self, static: float) -> float:
        """The fallback limit: the static cap, but never above what is already commanded."""
        return min(static, self.s.pl1_w) if self.s.pl1_w is not None else static

    def fallback(self, reason: str, static: float) -> None:
        s = self.s
        if s.state != FALLBACK:
            s.counters["fallbacks"] += 1
            self.event("fallback", reason=reason)
        s.fallback_reason = reason
        self.enter(FALLBACK, reason=reason)
        s.below_shed_since = s.below_recover_since = None
        fb = self.fallback_w(static)
        self.write(fb, fb, "fallback", force=True)


def step(state: GovState, sample: dict[str, Any], config: dict[str, Any], now: float
         ) -> tuple[GovState, list[dict[str, Any]]]:
    g = config["governor"]
    w = _Step(state, g, now)
    s = w.s
    if s.last_step is not None and now > s.last_step:
        s.time_in_state[s.state] = s.time_in_state.get(s.state, 0.0) + (now - s.last_step)
    s.last_step = now

    pdtr, mean, acpw = sample.get("pdtr_w"), sample.get("input_mean_1s"), sample.get("acpw_w")
    valid = _num(pdtr) and _num(acpw) and acpw > 0
    if valid:
        s.last_valid = now
    mode = g["mode"]
    static = float(g["static_cap_w"])
    if mode == "off":
        s.retry_at, s.fallback_reason = None, None
        if s.state != OFF:  # leaving governed control: put the static cap back once
            w.write(static, static, "off", force=True)
        w.enter(OFF)
        return s, w.actions
    force = state.state in (OFF, FALLBACK)

    # Readback verification of the previous write (rule 6).
    rb = sample.get("readback")
    if rb is not None:
        if rb.get("ok"):
            if s.state != FALLBACK:
                s.backoff_s = 0.0
        else:
            s.counters["readback_failures"] += 1
            s.backoff_s = 1.0 if s.backoff_s <= 0 else min(BACKOFF_MAX_S, s.backoff_s * 2)
            s.retry_at = now + s.backoff_s
            reason = f"readback mismatch: {rb.get('reason', '?')}"
            if s.state != FALLBACK:
                w.fallback(reason, static)
            else:
                s.fallback_reason = reason
            w.event("retry_scheduled", backoff_s=s.backoff_s, reason=reason)
            return s, w.actions
    retried = False
    if s.state == FALLBACK and s.retry_at is not None:
        if now < s.retry_at:
            floor = min(float(g["floor_w"]), static)
            th = thresholds(acpw, g) if valid else None
            if mode == "dynamic" and th and pdtr >= th["emergency_w"] and (s.pl1_w is None or s.pl1_w > floor):
                s.counters["emergencies"] += 1   # rule 2 still applies while waiting to retry
                w.event("emergency", input_w=round(pdtr, 2), emergency_w=round(th["emergency_w"], 2),
                        floor_w=floor, during="fallback")
                w.write(floor, floor, "emergency", force=True)
            return s, w.actions
        s.retry_at, retried = None, True
        w.event("retry", reason=s.fallback_reason)

    if mode == "static":
        s.fallback_reason = None
        force = force or s.state != STATIC
        w.enter(STATIC)
        w.write(static, static, "static", force=force)
        return s, w.actions

    # Dynamic mode. Stale telemetry => fallback to the static cap (rule 7).
    ref = s.last_valid if s.last_valid is not None else s.started
    if now - ref >= g["stale_timeout_s"]:
        reason = f"stale SMC telemetry (no valid sample for >= {g['stale_timeout_s']:g} s)"
        if s.state != FALLBACK:
            s.retry_at = None
            w.fallback(reason, static)
        else:
            s.fallback_reason = reason
            fb = w.fallback_w(static)
            w.write(fb, fb, "fallback", force=retried)
        return s, w.actions

    floor = min(float(g["floor_w"]), static)
    ceiling = w.cap1(max(float(g["ceiling_w"]), static))
    resume = s.state not in (NORMAL, TRIM, EMERGENCY) or s.pl1_w is None
    if not resume:
        pl1 = s.pl1_w
    elif s.state == FALLBACK:
        pl1 = w.fallback_w(static)   # leaving fallback never raises the limit; recover slowly from here
    else:
        pl1 = static
    pl1 = min(max(pl1, floor), ceiling)
    new = s.state if not resume else (NORMAL if pl1 >= ceiling else TRIM)
    if valid:
        th = thresholds(acpw, g)
        m = mean if _num(mean) else pdtr
        if pdtr >= th["emergency_w"]:                                   # rule 2
            if s.state != EMERGENCY:
                s.counters["emergencies"] += 1
                w.event("emergency", input_w=round(pdtr, 2), emergency_w=round(th["emergency_w"], 2), floor_w=floor)
            new, pl1 = EMERGENCY, floor
            s.below_shed_since = s.below_recover_since = None
        elif m >= th["shed_w"]:                                         # rule 3
            s.below_shed_since = s.below_recover_since = None
            if s.state != EMERGENCY:
                new = TRIM
                if now - s.last_cut >= CUT_MIN_INTERVAL_S and pl1 > floor:
                    old, pl1 = pl1, max(floor, pl1 - g["cut_step_w"])
                    s.last_cut = now
                    s.counters["cuts"] += 1
                    w.event("cut", from_w=old, to_w=pl1, input_mean_w=round(m, 2), shed_w=round(th["shed_w"], 2))
        else:
            if s.below_shed_since is None:
                s.below_shed_since = now
            if s.state == EMERGENCY:                                    # rule 5
                if now - s.below_shed_since >= EMERGENCY_HOLD_S:
                    new = TRIM
                    s.below_recover_since = None
            else:                                                       # rule 4
                if m < th["recover_w"]:
                    if s.below_recover_since is None:
                        s.below_recover_since = now
                    elif now - s.below_recover_since >= g["recover_interval_s"] and pl1 < ceiling:
                        old, pl1 = pl1, min(ceiling, pl1 + g["recover_step_w"])
                        s.below_recover_since = now
                        s.counters["recoveries"] += 1
                        w.event("recover", from_w=old, to_w=pl1, input_mean_w=round(m, 2))
                else:
                    s.below_recover_since = None
                new = NORMAL if pl1 >= ceiling else TRIM
    s.fallback_reason = None
    w.enter(new)
    pl2 = pl1 + g["burst_w"] if new in (NORMAL, TRIM) else pl1
    w.write(pl1, pl2, new.lower(), force=force or resume)
    return s, w.actions


def summary(s: GovState) -> dict[str, Any]:
    return {"state": s.state, "since": s.since, "pl1_w": s.pl1_w, "pl2_w": s.pl2_w,
            "fallback_reason": s.fallback_reason, "retry_at": s.retry_at, "backoff_s": s.backoff_s,
            "last_action": s.last_action, "counters": dict(s.counters),
            "time_in_state": {k: round(v, 1) for k, v in s.time_in_state.items()}}
