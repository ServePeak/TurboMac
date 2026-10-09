"""Screen 5: the daemon's event stream (band changes, cuts, writes, fallbacks, config changes)."""
from __future__ import annotations

import json
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Input, RichLog, Select, Static

from turbomac.tui import fmt

ALL = "*"
TYPE_STYLE = {"cut": fmt.AMBER, "emergency": "bold " + fmt.RED, "fallback": "bold " + fmt.MAGENTA,
              "state": fmt.BLUE, "recover": fmt.GREEN, "write": fmt.DIM, "config_change": fmt.CYAN,
              "gpu_mode": fmt.CYAN, "gpu_mode_request": fmt.CYAN, "retry_scheduled": fmt.MAGENTA}


def event_line(ev: dict[str, Any]) -> Text:
    typ = str(ev.get("type"))
    style = TYPE_STYLE.get(typ, fmt.RED if "error" in typ or "fail" in typ else "")
    t = fmt.text(f"{fmt.hms(ev.get('ts'))}  ", fmt.DIM)
    t.append(f"{typ:<18} ", style or "bold")
    t.append(" ".join(f"{k}={short(v)}" for k, v in ev.items() if k not in ("ts", "type")))
    return t


def short(v: Any, limit: int = 60) -> str:
    s = json.dumps(v, default=str)
    return s if len(s) <= limit else s[: limit - 1] + "…"


def ev_key(ev: dict[str, Any]) -> str:
    return json.dumps(ev, sort_keys=True, default=str)


class LogView(Vertical):
    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.events: list[dict[str, Any]] = []
        self.types: list[str] = []
        self.busy = False

    def compose(self) -> ComposeResult:
        with Horizontal(id="log-bar"):
            yield Input(placeholder="filter: text in the event (Enter or typing)", id="log-filter", compact=True)
            yield Select([("all types", ALL)], value=ALL, allow_blank=False, compact=True, id="log-type")
            yield Static("", id="log-count")
        yield RichLog(id="log-view", max_lines=5000, wrap=False, min_width=40, markup=False, highlight=False, auto_scroll=True)

    def show(self, status: dict[str, Any]) -> None:
        pass

    def activate(self) -> None:
        self.poll()

    def poll(self) -> None:
        if not self.busy:
            self.busy = True
            self.app.call_backend(lambda: self.app.backend.events(500), self.got)

    def matches(self, ev: dict[str, Any]) -> bool:
        typ = self.query_one("#log-type", Select).value
        if typ not in (ALL, Select.BLANK) and ev.get("type") != typ:
            return False
        needle = self.query_one("#log-filter", Input).value.strip().lower()
        return not needle or needle in ev_key(ev).lower()

    def got(self, evs: Any, err: str | None) -> None:
        self.busy = False
        log = self.query_one(RichLog)
        if err:
            self.query_one("#log-count", Static).update(Text(f"events unavailable: {err}", fmt.RED))
            return
        old = self.events
        last = ev_key(old[-1]) if old else None
        keys = [ev_key(e) for e in evs]
        self.events = list(evs)
        types = sorted({str(e.get("type")) for e in evs} | set(self.types))
        if types != self.types:
            self.types = types
            sel = self.query_one("#log-type", Select)
            cur = sel.value
            sel.set_options([("all types", ALL)] + [(t, t) for t in types])
            sel.value = cur if cur in types else ALL
        if last is not None and last in keys:
            for ev in self.events[keys.index(last) + 1:]:
                if self.matches(ev):
                    log.write(event_line(ev))
            self.count()
        else:
            self.rerender()

    def rerender(self) -> None:
        log = self.query_one(RichLog)
        log.clear()
        for ev in self.events:
            if self.matches(ev):
                log.write(event_line(ev))
        self.count()

    def count(self) -> None:
        n = sum(1 for e in self.events if self.matches(e))
        self.query_one("#log-count", Static).update(Text(f" {n} of {len(self.events)} events, newest last", fmt.DIM))

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "log-filter":
            self.rerender()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "log-type":
            self.rerender()
