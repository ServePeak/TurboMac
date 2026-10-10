"""Shared widgets: status-driven text panels, sparkline rows, header bar and modals."""
from __future__ import annotations

from fractions import Fraction
from typing import Any, Sequence

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, Input, Label, Sparkline, Static

from turbomac.tui import fmt


class Panel(Widget):
    """A text panel rebuilt from the latest status at render time.

    show() only stores the status and repaints (no layout pass); render() asks
    build() for the text at the current width. A build error is recorded on
    the app (app.render_errors) and shown in the panel instead of crashing.
    """

    DEFAULT_CSS = "Panel { height: auto; }"

    def __init__(self, title: str = "", **kw: Any) -> None:
        super().__init__(**kw)
        self.status: dict[str, Any] = {}
        if title:
            self.border_title = title

    def show(self, status: dict[str, Any]) -> None:
        self.status = status
        self.refresh()

    def render(self) -> Text:
        try:
            return self.build(self.status or {}, max(1, self.content_size.width), max(1, self.content_size.height))
        except Exception as exc:  # noqa: BLE001 - a bad field must not kill the cockpit
            errs = getattr(self.app, "render_errors", None)
            if isinstance(errs, list):
                errs.append(f"{type(self).__name__}: {exc!r}")
            return Text(f"render error: {exc!r}", style=fmt.RED)

    def build(self, s: dict[str, Any], width: int, height: int) -> Text:
        raise NotImplementedError


def clean(values: Sequence[Any]) -> list[float]:
    return [float(v) for v in values if fmt.isnum(v)]


class ScaledSparkline(Sparkline):
    """Textual's Sparkline on a fixed physical scale (lo..max(hi, data)) instead of the data's own
    min..max, so 7.02 W vs 7.03 W is not drawn as a full swing. Fewer points than cells fill from the
    right (newest at the right edge) instead of being stretched."""

    BARS = "▁▂▃▄▅▆▇█"

    def __init__(self, lo: float = 0.0, hi: float | None = None, **kw: Any) -> None:
        super().__init__([], summary_function=max, **kw)
        self.lo, self.hi = lo, hi

    def render(self) -> Text:
        width = max(1, self.size.width)
        data = list(self.data or [])
        if not data:
            return fmt.text("▁" * width, fmt.EMPTY)
        top = max(self.hi if self.hi is not None else max(data), max(data))
        span = (top - self.lo) or 1.0
        if len(data) > width:
            step = Fraction(len(data), width)
            cells = [max(data[int(step * i):max(int(step * i) + 1, int(step * (i + 1)))]) for i in range(width)]
        else:
            cells = data
        lo_c = self.min_color or self.get_component_styles("sparkline--min-color").color
        hi_c = self.max_color or self.get_component_styles("sparkline--max-color").color
        t = fmt.text(" " * (width - len(cells)))
        for v in cells:
            f = min(1.0, max(0.0, (v - self.lo) / span))
            t.append(self.BARS[min(7, int(f * 8))], lo_c.blend(hi_c, f).hex)
        return t


class SparkRow(Horizontal):
    """label | 60 s sparkline | 10 min sparkline | current and 10 min range."""

    DEFAULT_CSS = """
    SparkRow { height: 1; }
    SparkRow > .spark-label { width: 9; }
    SparkRow > Sparkline { width: 1fr; margin-right: 1; }
    SparkRow > .spark-stats { width: 28; }
    """

    def __init__(self, label: str, unit: str, colors: tuple[str, str], long: bool = True,
                 scale: tuple[float, float | None] = (0.0, None), **kw: Any) -> None:
        super().__init__(**kw)
        self.spark_label, self.unit, self.spark_colors, self.has_long = label, unit, colors, long
        self.scale = scale

    def compose(self) -> ComposeResult:
        lo, hi = self.spark_colors
        yield Label(self.spark_label, classes="spark-label")
        lo_v, hi_v = self.scale
        yield ScaledSparkline(lo_v, hi_v, min_color=lo, max_color=hi, classes="spark-short")
        if self.has_long:
            yield ScaledSparkline(lo_v, hi_v, min_color=lo, max_color=hi, classes="spark-long wide-only")
        yield Static("", classes="spark-stats")

    def set(self, short: Sequence[Any], long: Sequence[Any] = (), current: Any = None, nd: int = 1) -> None:
        a = clean(short)
        b = clean(long) if self.has_long else a
        self.query_one(".spark-short", Sparkline).data = a
        if self.has_long:
            self.query_one(".spark-long", Sparkline).data = b
        rng = b or a
        t = fmt.text(f"{fmt.num(current, self.unit, nd):>8}", "bold")
        if rng:
            t.append(f"  {min(rng):.{nd}f}–{fmt.num(max(rng), self.unit, nd)}", fmt.DIM)
        self.query_one(".spark-stats", Static).update(t, layout=False)


class ConfirmScreen(ModalScreen[bool]):
    """y / n confirmation."""

    BINDINGS = [Binding("y", "answer(True)", "Yes"), Binding("n,escape", "answer(False)", "No")]

    def __init__(self, question: str) -> None:
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(self.question, classes="dialog-text")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Yes (y)", id="yes", variant="error")
                yield Button("No (n)", id="no")

    def action_answer(self, value: bool) -> None:
        self.dismiss(value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


GPU_OFF_TEXT = (
    "Power the Radeon Pro 560 OFF?\n\n"
    "turbomacd unbinds fbcon, switches the panel to the Intel iGPU and powers the dGPU off through "
    "vga_switcheroo. It verifies, waits 10 s, verifies again, and only then saves gpu.mode = off; on any "
    "failure it reverts to low and says why.\n\n"
    "The internal panel may stay BLACK until reboot. Do this over SSH, supervised. A reboot always "
    "restores the firmware state.\n\nType OFF and press Enter to confirm, Esc to cancel.")


class GpuOffScreen(ModalScreen[bool]):
    """Gate for gpu.mode = off: dismisses True only when the user typed OFF exactly."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog", id="gpu-off-dialog"):
            yield Static(GPU_OFF_TEXT, classes="dialog-text")
            yield Input(placeholder="type OFF", id="gpu-off-input")
            yield Static("", id="gpu-off-error")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Power off", id="confirm", variant="error")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#gpu-off-input", Input).focus()

    def _try(self) -> None:
        if self.query_one("#gpu-off-input", Input).value.strip() == "OFF":
            self.dismiss(True)
        else:
            self.query_one("#gpu-off-error", Static).update(Text("Not confirmed: type OFF exactly.", fmt.RED))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._try()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm":
            self._try()
        else:
            self.dismiss(False)

    def action_cancel(self) -> None:
        self.dismiss(False)


HELP_TEXT = """[b]turbomactop[/b] keys

  [b]1[/b] Dashboard   [b]2[/b] Settings   [b]3[/b] GPU   [b]4[/b] Incidents   [b]5[/b] Log
  [b]r[/b] reset session peaks          [b]p[/b] pause / resume updates
  [b]?[/b] this help                    [b]q[/b] quit
  [b]Esc[/b] leave a text field (keys go to the field while it has focus)

Settings: edit a value and press [b]Enter[/b] to apply (turbomacd validates; rejected
values show the reason). Selects apply when chosen. [b]d[/b] resets to defaults.
GPU: auto / low / off; off asks you to type OFF.
Mouse: tabs, fields, radio buttons and table rows are clickable.

Gauge: [#3fb950]green[/] below recover, [#f0a020]amber[/] up to shed, [#f85149]red[/] at shed and above.
▲ recover / shed / emergency bands (ratios of the live ACPW), ◆ input predicted
for the current PL1 (8.0 W + 1.24 × PL1).

Press any key to close."""


class HelpScreen(ModalScreen[None]):
    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog", id="help-dialog"):
            yield Static(HELP_TEXT, classes="dialog-text")

    def on_key(self, event: Any) -> None:
        event.stop()
        self.dismiss(None)

    def on_click(self) -> None:
        self.dismiss(None)
