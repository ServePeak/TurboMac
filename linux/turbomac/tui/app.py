"""turbomactop: the Textual app. Data comes only from a Backend (socket, mock or replay)."""
from __future__ import annotations

import time
from collections import deque
from typing import Any, Callable

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widget import Widget
from textual.widgets import Footer, TabbedContent, TabPane

from turbomac.tui import fmt
from turbomac.tui.backend import Backend
from turbomac.tui.fmt import DASH, DIM
from turbomac.tui.screens.dashboard import Dashboard
from turbomac.tui.screens.gpu import GpuView
from turbomac.tui.screens.incidents import IncidentsView
from turbomac.tui.screens.log import LogView
from turbomac.tui.screens.settings import SettingsView
from turbomac.tui.widgets import HelpScreen

NARROW_W = 110
FAST_SPAN_S = 60.0
SERIES = ("input", "pkg", "gpu", "temp")
VIEWS = {"dash": "#dashboard", "settings": "#settings-view", "gpu": "#gpu-view", "incidents": "#incidents-view",
         "log": "#log-view-pane"}


def series(s: dict[str, Any]) -> dict[str, Any]:
    """The four sparkline series from one status (4 Hz values where the daemon has them)."""
    lat, gpu, cpu = s.get("latest") or {}, s.get("gpu") or {}, s.get("cpu") or {}
    return {"input": lat.get("pdtr_w"), "pkg": lat.get("package_w"), "gpu": gpu.get("power_w"),
            "temp": (cpu.get("temps") or {}).get("pkg_c")}


class HeaderBar(Widget):
    """host · OS · uptime · governor mode/state/time in state ...... daemon CPU · SMC · connection · clock."""

    DEFAULT_CSS = "HeaderBar { height: 1; }"

    def render(self) -> Text:
        app: TurboMacTop = self.app  # type: ignore[assignment]
        s, be = app.status or {}, app.backend
        gov, cpu = s.get("governor") or {}, s.get("cpu") or {}
        smc_ok = s.get("smc_ok")
        state = fmt.text(" · governor ", DIM)
        state.append(str(gov.get("mode") or DASH).upper(), "bold")
        state.append(" ")
        state.append_text(fmt.badge(gov.get("state")))
        state.append(f" {fmt.dur(gov.get('in_state_s'))}", DIM)
        smc = fmt.text("SMC ", DIM)
        smc.append("ok" if smc_ok else (DASH if smc_ok is None else "DOWN"),
                   fmt.GREEN if smc_ok else (DIM if smc_ok is None else "bold " + fmt.RED))
        conn = fmt.text("● ", fmt.GREEN) if be.connected else fmt.text("○ disconnected", "bold " + fmt.RED)
        me = fmt.text("daemon ", DIM)
        me.append(fmt.num((s.get("self") or {}).get("cpu_pct"), "%"))
        me.append(" · ", DIM)
        clock = s.get("ts") if be.kind == "replay" else time.time()
        # (priority, side, text): the lowest priorities are dropped first when the line is too narrow
        segs = [(9, "L", fmt.text(" TurboMac ", "bold #ffffff on #1f6feb")),
                (9, "L", fmt.text(f" {s.get('host') or DASH}", "bold")),
                (2, "L", fmt.text(f" · {app.os_name}") if app.os_name else Text()),
                (3, "L", fmt.text(f" · up {fmt.dur(cpu.get('uptime_s'))}", DIM)),
                (8, "L", state),
                (9, "R", fmt.text(" PAUSED ", "bold #0b0e14 on #f0a020") + Text(" ") if app.paused else Text()),
                (0, "R", me),   # dropped first: the header is unchanged when the line is full
                (5, "R", smc),
                (1, "R", fmt.text(f" {app.smc_keys} keys", DIM) if app.smc_keys is not None else Text()),
                (9, "R", fmt.text(" · ") + conn),
                (4, "R", fmt.text(be.label) if be.connected else Text()),   # the only priority-4 segment
                (1, "R", fmt.text(" (log time)", DIM) if be.kind == "replay" else Text()),
                (9, "R", fmt.text(f" · {fmt.hms(clock)} ", "bold"))]
        short = fmt.text(be.kind if be.connected else "")
        for prio in range(10):
            if sum(len(t.plain) for _, _, t in segs) < self.size.width:
                break
            if prio == 4:   # the full backend label becomes its kind (live / mock / replay)
                segs = [(6, side, short) if p == 4 else (p, side, t) for p, side, t in segs]
            segs = [x for x in segs if x[0] > prio]
        left, right = Text(), Text()
        for _, side, t in segs:
            (left if side == "L" else right).append_text(t)
        pad = max(1, self.size.width - len(left.plain) - len(right.plain))
        return left + Text(" " * pad) + right


class TurboMacTop(App[None]):
    CSS_PATH = "turbomactop.tcss"
    TITLE = "turbomactop"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("1", "tab('dash')", "Dashboard"), Binding("2", "tab('settings')", "Settings"),
        Binding("3", "tab('gpu')", "GPU"), Binding("4", "tab('incidents')", "Incidents"),
        Binding("5", "tab('log')", "Log"), Binding("r", "reset_peaks", "Reset peaks"),
        Binding("p", "pause", "Pause"), Binding("question_mark", "help", "Help", key_display="?"),
        Binding("q", "quit", "Quit"), Binding("escape", "blur", show=False),
        Binding("d", "reset_defaults", "Defaults", show=False),
    ]

    def __init__(self, backend: Backend, **kw: Any) -> None:
        super().__init__(**kw)
        self.backend = backend
        self.status: dict[str, Any] = {}
        self.paused = False
        self.peaks: dict[str, dict[str, float]] = {}
        self.peaks_since: float | None = None
        self.fast: dict[str, deque[tuple[float, Any]]] = {k: deque() for k in SERIES}
        self.slow: dict[str, list[Any]] = {k: [] for k in SERIES}
        self.render_errors: list[str] = []
        self.smc_keys: int | None = None
        self.os_name: str | None = None
        self.sub: Any = None
        self.closing = False

    def compose(self) -> ComposeResult:
        yield HeaderBar(id="header")
        with TabbedContent(initial="dash", id="tabs"):
            with TabPane("1 Dashboard", id="dash"):
                yield Dashboard(id="dashboard")
            with TabPane("2 Settings", id="settings"):
                yield SettingsView(id="settings-view")
            with TabPane("3 GPU", id="gpu"):
                yield GpuView(id="gpu-view")
            with TabPane("4 Incidents", id="incidents"):
                yield IncidentsView(id="incidents-view")
            with TabPane("5 Log", id="log"):
                yield LogView(id="log-view-pane")
        yield Footer()

    # lifecycle ------------------------------------------------------------------
    def on_mount(self) -> None:
        self.theme = "textual-dark"
        self.apply_layout(self.size.width)
        self.load_initial()
        self.query_one("#banner").show({})  # type: ignore[attr-defined]
        self.sub = self.backend.subscribe(self.from_thread, self.conn_from_thread)
        self.set_interval(5.0, self.refresh_history)
        self.set_interval(1.0, self.tick)
        self.set_interval(2.0, self.poll_log)

    def on_unmount(self) -> None:
        self.closing = True
        if self.sub is not None:
            self.sub.stop()
        self.backend.close()

    def call_backend(self, fn: Callable[[], Any], done: Callable[[Any, str | None], None]) -> None:
        """Run a (blocking) backend call on a worker thread; done(result, error) runs on the UI thread."""
        def run() -> None:
            try:
                res, err = fn(), None
            except Exception as exc:  # noqa: BLE001 - every failure is shown to the user
                res, err = None, str(exc) or type(exc).__name__
            if not self.closing:
                try:
                    self.call_from_thread(done, res, err)
                except RuntimeError:
                    pass
        self.run_worker(run, thread=True, exit_on_error=False, group="backend")

    def from_thread(self, status: dict[str, Any]) -> None:
        if self.closing:
            raise RuntimeError("closing")
        self.call_from_thread(self.on_status, status)

    def conn_from_thread(self, ok: bool, err: str | None) -> None:
        if not self.closing:
            self.call_from_thread(self.conn_changed, ok, err)

    # data -----------------------------------------------------------------------
    def load_initial(self) -> None:
        be = self.backend

        def initial() -> tuple[Any, Any, Any, Any]:
            return be.status(), be.history(600), len(be.smc_keys()), be.os_name()
        self.call_backend(initial, self.got_initial)

    def got_initial(self, res: Any, err: str | None) -> None:
        if err:
            return    # the subscription reports the connection state; we retry when it connects
        st, hist, nkeys, osn = res
        self.smc_keys, self.os_name = nkeys, osn
        self.set_history(hist)
        if st and not self.status:
            self.on_status(st)

    def refresh_history(self) -> None:
        self.call_backend(lambda: self.backend.history(600), lambda h, e: None if e else self.set_history(h))

    def set_history(self, rows: list[dict[str, Any]]) -> None:
        rows = rows or []
        for key, field in zip(SERIES, ("input_w", "pkg_w", "gpu_w", "pkg_c")):
            self.slow[key] = [r.get(field) for r in rows]
            buf = self.fast[key]
            if len(buf) < 8 and rows and fmt.isnum(rows[-1].get("ts")):   # seed the 60 s view from 1 Hz history
                end = rows[-1]["ts"]
                old = [(r["ts"], r.get(field)) for r in rows if fmt.isnum(r.get("ts")) and r["ts"] > end - FAST_SPAN_S]
                newer = [x for x in buf if x[0] > end]
                buf.clear()
                buf.extend(old + newer)

    def on_status(self, s: dict[str, Any]) -> None:
        if not isinstance(s, dict) or not s:
            return
        self.status = s
        ts = s.get("ts")
        if fmt.isnum(ts):
            vals = series(s)
            amps = (s.get("latest") or {}).get("a")
            for key in SERIES:
                buf = self.fast[key]
                if buf and ts < buf[-1][0] - 1:
                    buf.clear()       # clock went backwards (daemon restart or replay reset)
                buf.append((ts, vals[key]))
                while buf and buf[0][0] < ts - FAST_SPAN_S:
                    buf.popleft()
            if self.peaks_since is None:
                self.peaks_since = ts
            for key, v in (*vals.items(), ("amps", amps)):
                if fmt.isnum(v) and (key not in self.peaks or v > self.peaks[key]["value"]):
                    self.peaks[key] = {"value": float(v), "ts": ts}
        self.query_one(HeaderBar).refresh()
        if not self.paused:
            self.update_active()

    def update_active(self) -> None:
        if not self.status:
            return
        tabs = self.query_one(TabbedContent)
        view = self.query_one(VIEWS[tabs.active])
        if isinstance(view, Dashboard):
            view.show(self.status, {k: [v for _, v in self.fast[k]] for k in SERIES}, self.slow)
        else:
            view.show(self.status)  # type: ignore[attr-defined]

    def conn_changed(self, ok: bool, err: str | None) -> None:
        self.query_one(HeaderBar).refresh()
        if ok:
            self.load_initial() if self.smc_keys is None else self.refresh_history()
        self.query_one("#banner").show(self.status)  # type: ignore[attr-defined]

    # Interval timers can still fire while the app tears its widgets down: query(), not query_one().
    def tick(self) -> None:
        for bar in self.query(HeaderBar):
            bar.refresh()

    def poll_log(self) -> None:
        tabs = self.query(TabbedContent)
        if tabs and tabs.first().active == "log":
            for view in self.query(LogView):
                view.poll()

    # layout and actions -----------------------------------------------------------
    def apply_layout(self, width: int) -> None:
        self.query_one("#tabs").set_class(width < NARROW_W, "-narrow")

    def on_resize(self, event: Any) -> None:
        self.apply_layout(event.size.width)

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        view = self.query_one(VIEWS[self.query_one(TabbedContent).active])
        activate = getattr(view, "activate", None)
        if activate is not None:
            activate()
        self.update_active()

    def action_tab(self, tab: str) -> None:
        self.query_one(TabbedContent).active = tab

    def action_reset_peaks(self) -> None:
        self.peaks, self.peaks_since = {}, None
        self.notify("session peaks reset")
        self.update_active()

    def action_pause(self) -> None:
        self.paused = not self.paused
        self.query_one(HeaderBar).refresh()
        if not self.paused:
            self.update_active()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_blur(self) -> None:
        self.set_focus(None)

    def action_reset_defaults(self) -> None:
        if self.query_one(TabbedContent).active == "settings":
            self.query_one(SettingsView).action_reset_defaults()

