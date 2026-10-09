"""Screen 4: abrupt boots recorded by turbomacd (PLAN 4.5)."""
from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import DataTable, Static

from turbomac.tui import fmt
from turbomac.tui.fmt import DASH, DIM
from turbomac.tui.widgets import SparkRow


class IncidentsView(Vertical):
    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.loaded_id: str | None = None

    def compose(self) -> ComposeResult:
        yield Static("", id="inc-head")
        yield DataTable(id="inc-table", cursor_type="row", zebra_stripes=True)
        with VerticalScroll(id="inc-detail"):
            yield Static("", id="inc-title")
            yield SparkRow("input", "W", (fmt.GREEN, fmt.RED), long=False, scale=(0.0, 80.0), id="inc-sp-input")
            yield SparkRow("package", "W", (fmt.BLUE, fmt.MAGENTA), long=False, scale=(0.0, 45.0), id="inc-sp-pkg")
            yield SparkRow("GPU", "W", (fmt.TEAL, fmt.CYAN), long=False, scale=(0.0, 15.0), id="inc-sp-gpu")
            yield Static("", id="inc-last")
            yield Static("", id="inc-top")
            yield Static("", id="inc-journal")

    def on_mount(self) -> None:
        self.query_one(DataTable).add_columns("incident", "boot", "last sample", "samples", "reason")

    def show(self, status: dict[str, Any]) -> None:
        pass

    def activate(self) -> None:
        self.app.call_backend(self.app.backend.incidents, self.listed)

    def listed(self, rows: Any, err: str | None) -> None:
        head = self.query_one("#inc-head", Static)
        table = self.query_one(DataTable)
        if err:
            head.update(Text(f"cannot list incidents: {err}", fmt.RED))
            return
        ic = (getattr(self.app, "status", {}) or {}).get("incident_check") or {}
        t = fmt.text(f"{len(rows)} incident(s) recorded", "bold")
        t.append(f"   last boot check: {ic.get('verdict', DASH)} ({ic.get('reason', DASH)})", DIM)
        head.update(t)
        keep = self.loaded_id
        table.clear()
        for r in rows:
            table.add_row(r.get("id"), str(r.get("boot_id") or DASH)[:8], r.get("last_sample_time") or DASH,
                          f"{r.get('n_samples', 0)}", str(r.get("reason") or DASH)[:44], key=r.get("id"))
        if not rows:
            self.query_one("#inc-title", Static).update(Text("no abrupt boots recorded", DIM))
            return
        ids = [r.get("id") for r in rows]
        table.move_cursor(row=ids.index(keep) if keep in ids else 0)
        self.load(keep if keep in ids else ids[0])

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        iid = event.row_key.value if event.row_key is not None else None
        if iid and iid != self.loaded_id:
            self.load(iid)

    def load(self, iid: str) -> None:
        self.loaded_id = iid
        self.app.call_backend(lambda: self.app.backend.incident(iid), self.render_incident)

    def render_incident(self, inc: Any, err: str | None) -> None:
        title = self.query_one("#inc-title", Static)
        if err or not inc:
            title.update(Text(f"cannot load incident: {err}", fmt.RED))
            return
        samples = inc.get("samples") or []
        t = fmt.text(f"{inc.get('id')}", "bold")
        t.append(f"   boot {inc.get('boot_id') or DASH}   detected {inc.get('detected_at') or DASH}   "
                 f"{len(samples)} s of samples\n", DIM)
        t.append(str(inc.get("reason") or ""), fmt.AMBER)
        title.update(t)
        last = samples[-1] if samples else {}
        for key, wid in (("input_w", "#inc-sp-input"), ("pkg_w", "#inc-sp-pkg"), ("gpu_w", "#inc-sp-gpu")):
            self.query_one(wid, SparkRow).set([r.get(key) for r in samples], (), last.get(key))
        lt = fmt.text("last sample ", "bold")
        lt.append(f"{last.get('time') or DASH}  ", DIM)
        for label, val in (("input", fmt.w(last.get("input_w"))), ("package", fmt.w(last.get("pkg_w"))),
                           ("GPU", fmt.w(last.get("gpu_w"))), ("PL1", fmt.g(last.get("pl1_w"))),
                           ("pkg", fmt.num(last.get("pkg_c"), "°C", 0)), ("state", str(last.get("state") or DASH))):
            fmt.kv(lt, label, val)
        self.query_one("#inc-last", Static).update(lt)
        top = (last.get("cpu") or {}).get("top") or []
        tt = fmt.text("top processes  ", "bold")
        tt.append("   ".join(f"{p.get('name')} {fmt.num(p.get('cpu_pct'), '%', 0)}" for p in top[:8]) or "none recorded",
                  "" if top else DIM)
        self.query_one("#inc-top", Static).update(tt)
        jt = fmt.text("journal tail (previous boot)\n", "bold")
        jt.append("\n".join(inc.get("journal_tail") or ["(empty)"]), DIM)
        self.query_one("#inc-journal", Static).update(jt)
