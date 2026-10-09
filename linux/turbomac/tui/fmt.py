"""Formatting for turbomactop: every number gets a unit, None renders as an en dash."""
from __future__ import annotations

import math
import time
from typing import Any

from rich.text import Text

DASH = "–"
GREEN, AMBER, RED, BLUE, MAGENTA, GREY, CYAN = ("#3fb950", "#f0a020", "#f85149", "#58a6ff", "#d070f0",
                                                 "#8b949e", "#39c5cf")
DIM, EMPTY, TEAL, YELLOW = "#6e7681", "#262c36", "#1b7c83", "#d4b000"
STATE_STYLE = {"NORMAL": "bold #0b0e14 on #3fb950", "TRIM": "bold #0b0e14 on #f0a020",
               "EMERGENCY": "bold #ffffff on #d73a49", "FALLBACK": "bold #ffffff on #a040c0",
               "STATIC": "bold #ffffff on #1f6feb", "OFF": "bold #0b0e14 on #8b949e"}
EIGHTHS = " ▏▎▍▌▋▊▉█"


def isnum(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def num(v: Any, unit: str = "", nd: int = 1, sep: str = " ") -> str:
    """'56.1 W'; '–' for None/NaN/non-numbers."""
    if not isnum(v):
        return DASH
    if nd >= 0:
        v = round(v, nd) + 0.0   # no "-0"
    s = f"{v:.{nd}f}" if nd >= 0 else f"{v:g}"
    return f"{s}{sep}{unit}" if unit else s


def w(v: Any, nd: int = 1) -> str:
    return num(v, "W", nd)


def g(v: Any, unit: str = "W") -> str:
    """Compact: 35 W, 37.5 W."""
    return num(v, unit, -1)


def hms(ts: Any) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts)) if isnum(ts) else DASH


def dur(sec: Any) -> str:
    """12 s, 4m05s, 1h02m, 4d 01:27."""
    if not isnum(sec):
        return DASH
    sec = int(max(0, sec))
    if sec < 60:
        return f"{sec} s"
    if sec < 3600:
        return f"{sec // 60}m{sec % 60:02d}s"
    if sec < 86400:
        return f"{sec // 3600}h{sec % 3600 // 60:02d}m"
    return f"{sec // 86400}d {sec % 86400 // 3600:02d}:{sec % 3600 // 60:02d}"


def text(s: str, style: str = "") -> Text:
    """A Text whose first span is styled (Text(s, style) would style everything appended later)."""
    t = Text()
    t.append(s, style)
    return t


def badge(state: Any) -> Text:
    st = str(state) if state else DASH
    return text(f" {st} ", STATE_STYLE.get(st, "bold reverse"))


def band_style(input_w: Any, th: dict[str, Any] | None) -> str:
    """green below recover, amber up to shed, red at or above shed."""
    if not isnum(input_w) or not th:
        return GREY
    if input_w >= th["shed_w"]:
        return RED
    return AMBER if input_w >= th["recover_w"] else GREEN


def bar(frac: Any, width: int, style: str = GREEN, empty: str = EMPTY) -> Text:
    """Horizontal bar with 1/8-cell resolution."""
    t = Text()
    if width <= 0:
        return t
    f = min(1.0, max(0.0, frac)) if isnum(frac) else 0.0
    cells = f * width
    full = int(cells)
    t.append("█" * full, style)
    if full < width:
        part = int((cells - full) * 8)
        t.append(EIGHTHS[part] if part else "░", style if part else empty)
        t.append("░" * (width - full - 1), empty)
    return t


def pd_contract(acpw_w: Any, v: Any) -> str:
    """Inferred USB-PD contract, e.g. '20 V / 4 A class (80 W)'."""
    if not isnum(acpw_w) or acpw_w <= 0:
        return DASH
    nominal = min((5, 9, 12, 15, 20), key=lambda n: abs(n - (v if isnum(v) else 20.0)))
    amps = round(acpw_w / nominal * 4) / 4
    return f"{nominal} V / {amps:g} A class ({nominal * amps:g} W)"


def kv(t: Text, label: str, value: str, style: str = "bold", end: str = "  ") -> Text:
    """Append 'label value' + end, the value highlighted."""
    if label:
        t.append(label + " ", DIM)
    t.append(value, style)
    t.append(end)
    return t


def yes(v: Any, on: str, style: str = RED) -> Text:
    if v is None:
        return Text(f"{on} {DASH}", DIM)
    return Text(f"{on} ●", style) if v else Text(f"{on} ○", DIM)


def temp_style(v: Any) -> str:
    if not isnum(v):
        return DIM
    return RED if v >= 90 else AMBER if v >= 75 else ("" if v > 0 else DIM)


def busy_style(pct: Any) -> str:
    if not isnum(pct):
        return DIM
    return RED if pct >= 90 else AMBER if pct >= 50 else GREEN
