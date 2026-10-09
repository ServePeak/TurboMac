"""Screen 3: dGPU power mode auto / low / off (PLAN 4.4); off is gated by typing OFF."""
from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import RadioButton, RadioSet, Static

from turbomac.tui import fmt
from turbomac.tui.fmt import DIM
from turbomac.tui.screens.dashboard import gpu_text
from turbomac.tui.widgets import GpuOffScreen

MODES = ("auto", "low", "off")
LABELS = {"auto": "auto · cap at gpu.cap_w, DPM auto",
          "low": "low  · cap 10 W, DPM low",
          "off": "off  · switcheroo power-off (gated)"}


class GpuView(Vertical):
    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.pending: str | None = None     # a change is running
        self.expect: str | None = None      # the mode our last change reached, until a status confirms it
        self.local: dict[str, Any] | None = None
        self.status: dict[str, Any] = {}
        self.steps_key: Any = None

    def compose(self) -> ComposeResult:
        yield Static(Text("Radeon Pro 560 power mode. Selecting a mode applies it through turbomacd and saves "
                          "the mode it actually reached.", DIM), id="gpu-intro")
        with Horizontal(id="gpu-top"):
            with RadioSet(id="gpu-modes"):
                for m in MODES:
                    yield RadioButton(LABELS[m], id=f"gm-{m}")
            yield Static("", id="gpu-state")
        yield Static(Text("Click a mode or use ↑/↓ and Enter. off asks you to type OFF.", DIM), id="gpu-progress")
        yield Static("", id="gpu-steps")

    def current(self) -> str | None:
        return (self.status.get("gpu") or {}).get("config_mode")

    def effective(self) -> str | None:
        return self.expect or self.current()

    def press(self, mode: str | None) -> None:
        if mode in MODES:
            btn = self.query_one(f"#gm-{mode}", RadioButton)
            if not btn.value:
                btn.value = True

    def activate(self) -> None:
        pass

    def show(self, status: dict[str, Any]) -> None:
        self.status = status
        gpu = status.get("gpu") or {}
        st = self.query_one("#gpu-state", Static)
        st.border_title = "NOW"
        st.update(gpu_text(gpu, max(20, st.content_size.width or 40)), layout=False)
        if self.expect is not None and self.current() == self.expect:
            self.expect = None                # statuses in flight during the change are stale until then
        if self.pending is None and self.expect is None:
            self.press(self.current())
        self.render_steps(gpu.get("last_result") or self.local)

    def render_steps(self, res: dict[str, Any] | None) -> None:
        if res == self.steps_key and self.steps_key is not None:
            return
        self.steps_key = res
        t = fmt.text("LAST MODE CHANGE\n", "bold")
        if not res:
            t.append("none since the daemon started", DIM)
        else:
            ok = res.get("ok")
            t.append("OK" if ok else "FAILED", "bold " + (fmt.GREEN if ok else fmt.RED))
            t.append(f" → mode {res.get('mode')}: {res.get('reason')}")
            if "persisted" in res:
                t.append("  saved" if res.get("persisted") else f"  NOT saved {res.get('persist_error') or ''}",
                         DIM if res.get("persisted") else fmt.AMBER)
            for step in res.get("steps") or []:
                t.append(f"\n  {step}", fmt.RED if "FAILED" in step else "")
        self.query_one("#gpu-steps", Static).update(t)

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        event.stop()
        mode = MODES[event.index] if 0 <= event.index < len(MODES) else None
        if mode is None or mode == self.effective() or self.pending is not None:
            return
        if mode == "off":
            self.app.push_screen(GpuOffScreen(), self.off_answer)
        else:
            self.apply(mode)

    def off_answer(self, ok: bool | None) -> None:
        if ok:
            self.apply("off")
            return
        self.query_one("#gpu-progress", Static).update(Text("off cancelled: nothing changed", DIM))
        self.press(self.effective())

    def apply(self, mode: str) -> None:
        self.pending = mode
        self.query_one("#gpu-modes", RadioSet).disabled = True
        note = " (switcheroo off, verify, wait 10 s, verify)" if mode == "off" else ""
        self.query_one("#gpu-progress", Static).update(Text(f"applying {mode}{note}…", "bold " + fmt.AMBER))
        self.app.call_backend(lambda: self.app.backend.gpu_set(mode), self.done)

    def done(self, res: Any, err: str | None) -> None:
        mode, self.pending = self.pending, None
        self.query_one("#gpu-modes", RadioSet).disabled = False
        prog = self.query_one("#gpu-progress", Static)
        if err:
            prog.update(Text(f"{mode} REJECTED: {err}", "bold " + fmt.RED))
        else:
            self.local = res
            ok = res.get("ok")
            prog.update(Text(f"{mode}: {'OK' if ok else 'FAILED'} → mode {res.get('mode')} ({res.get('reason')})",
                             "bold " + (fmt.GREEN if ok else fmt.RED)))
            self.render_steps(res)
        self.expect = (res or {}).get("mode") if not err else None
        self.press(self.effective())

