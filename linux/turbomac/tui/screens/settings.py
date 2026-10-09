"""Screen 2: every config key, editable in place; turbomacd validates (PLAN 4.2, 4.7)."""
from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widget import Widget
from textual.widgets import Input, Label, Select, Static

from turbomac import config
from turbomac.daemon.governor import predicted_input_w
from turbomac.tui import fmt
from turbomac.tui.fmt import DASH, DIM
from turbomac.tui.widgets import ConfirmScreen, GpuOffScreen

NULL = "__null__"
GROUPS = ("governor", "gpu", "cpu", "logging")
MEANING = {
    "governor.mode": "off = telemetry only · static = PL1 = PL2 = static cap · dynamic = trim on measured input",
    "governor.static_cap_w": "boot and fallback cap; Intel's cTDP-down level for this part",
    "governor.ceiling_w": "highest PL1 the dynamic loop may recover to",
    "governor.floor_w": "emergency target; below ~9 W the package does not respond",
    "governor.burst_w": "PL2 = PL1 + burst; 0 means no burst, which is the point",
    "governor.shed_ratio": "of live ACPW: cut PL1 when the 1 s mean input reaches it",
    "governor.emergency_ratio": "of live ACPW: drop to the floor when one input sample reaches it",
    "governor.recover_ratio": "of live ACPW: recover only while the 1 s mean input stays below it",
    "governor.cut_step_w": "PL1 cut per shed event, at most one per 0.5 s",
    "governor.recover_step_w": "PL1 raise per recovery interval",
    "governor.recover_interval_s": "time below the recover band per recovery step",
    "governor.stale_timeout_s": "no valid SMC sample for this long ⇒ FALLBACK to the static cap",
    "gpu.mode": "auto = power1_cap at gpu.cap_w · low = 10 W, DPM low · off = switcheroo power-off (gated)",
    "gpu.cap_w": "dGPU power1_cap in auto mode",
    "cpu.no_turbo": "intel_pstate no_turbo; – leaves the kernel value alone",
    "cpu.epp": "energy-performance preference for every CPU; – leaves it alone",
    "cpu.max_perf_pct": "intel_pstate max_perf_pct; empty leaves it alone",
    "logging.retention_days": "sample logs older than this are deleted",
    "logging.max_mib": "size budget for all sample logs; oldest go first",
}


def unit_for(key: str) -> str:
    for suffix, unit in (("_w", "W"), ("_s", "s"), ("_days", "d"), ("_mib", "MiB"), ("_pct", "%"),
                         ("_ratio", "× ACPW")):
        if key.endswith(suffix):
            return unit
    return ""


def value_text(key: str, v: Any, acpw: Any = None) -> str:
    if v is None:
        return f"{DASH} (leave)"
    if isinstance(v, bool):
        return "true" if v else "false"
    if fmt.isnum(v):
        if key.endswith("_ratio"):
            return f"{v:g} ({acpw * v:.1f} W)" if fmt.isnum(acpw) else f"{v:g}"
        return f"{v:g} {unit_for(key)}".strip()
    return str(v)


def bounds_text(key: str) -> str:
    kind, arg = config.SPEC[key]
    null = kind.endswith("?")
    if kind.startswith("choice"):
        return " / ".join(arg) + (" / –" if null else "")
    if kind.startswith("bool"):
        return "true / false / –"
    lo, hi = arg
    return f"{lo:g}–{hi:g} {unit_for(key)}".strip() + (", empty = –" if null else "")


def editor_value(key: str, v: Any) -> Any:
    """Config value -> what the editor widget holds."""
    kind = config.SPEC[key][0]
    if kind.startswith(("choice", "bool")):
        return NULL if v is None else (("true" if v else "false") if isinstance(v, bool) else v)
    return "" if v is None else (f"{v:g}" if fmt.isnum(v) else str(v))


def parse(key: str, raw: Any) -> Any:
    """Editor content -> config value. Unparsable text is sent as-is so the daemon names the problem."""
    kind = config.SPEC[key][0]
    if raw == NULL:
        return None
    if kind.startswith("bool"):
        return {"true": True, "false": False}.get(raw, raw)
    if kind.startswith("choice"):
        return raw
    text = str(raw).strip()
    if text == "" and kind.endswith("?"):
        return None
    try:
        v = float(text)
    except ValueError:
        return text
    return int(v) if v.is_integer() else v


def make_editor(key: str) -> Widget:
    kind, arg = config.SPEC[key]
    wid = "ed-" + key.replace(".", "--")
    if kind.startswith("choice"):
        opts = [(c, c) for c in arg] + ([("– leave alone", NULL)] if kind.endswith("?") else [])
        return Select(opts, allow_blank=False, compact=True, id=wid, classes="set-editor")
    if kind.startswith("bool"):
        opts = [("true", "true"), ("false", "false"), ("– leave alone", NULL)]
        return Select(opts, allow_blank=False, compact=True, id=wid, classes="set-editor")
    typ = "integer" if kind == "int" else ("number" if kind == "num" else "text")
    return Input(compact=True, type=typ, id=wid, classes="set-editor",
                 placeholder="empty = leave" if kind.endswith("?") else "")


class SettingRow(Vertical):
    def __init__(self, key: str) -> None:
        super().__init__(id="row-" + key.replace(".", "--"), classes="set-row")
        self.key = key

    def compose(self) -> ComposeResult:
        with Horizontal(classes="set-line"):
            yield Label(self.key, classes="set-key")
            yield make_editor(self.key)
            yield Static("", classes="set-info")
            yield Static("", classes="set-result")
        rules = [r for keys, _, r in config.CROSS_RULES if self.key in keys]
        yield Static(Text(MEANING.get(self.key, "") + ("  [" + "; ".join(rules) + "]" if rules else ""), DIM),
                     classes="set-meaning")

    def result(self, text: Text) -> None:
        self.query_one(".set-result", Static).update(text)


class SettingsView(Vertical):
    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.cfg: dict[str, Any] = {}
        self.defaults: dict[str, Any] = config.flatten(config.DEFAULTS)
        self.digest: str | None = None
        self.path = ""
        self.persist_note: Text | None = None
        self.last_change: float | None = None
        self.loading = False
        self.keep: set[str] = set()          # rejected keys keep the user's text
        self.status: dict[str, Any] = {}

    def compose(self) -> ComposeResult:
        yield Static("", id="cfg-badge")
        yield Static("", id="cfg-preview")
        with VerticalScroll(id="cfg-rows"):
            for group in GROUPS:
                yield Label(group.upper(), classes="set-group")
                for key in config.SPEC:
                    if key.startswith(group + "."):
                        yield SettingRow(key)

    # data ------------------------------------------------------------------------
    def activate(self) -> None:
        self.reload()

    def reload(self) -> None:
        if self.loading:
            return
        self.loading = True
        be = self.app.backend

        def fetch() -> tuple[dict[str, Any], list[dict[str, Any]]]:
            return be.config_get(), be.events(300)
        self.app.call_backend(fetch, self.loaded)

    def loaded(self, res: Any, err: str | None) -> None:
        self.loading = False
        if err:
            self.persist_note = Text(f"cannot read config: {err}", fmt.RED)
            self.render_badge()
            return
        cfg, evs = res
        self.cfg, self.digest, self.path = config.flatten(cfg["config"]), cfg.get("digest"), cfg.get("path", "")
        self.defaults = config.flatten(cfg.get("defaults") or config.DEFAULTS)
        changes = [e["ts"] for e in evs if e.get("type") in ("config_change", "gpu_mode", "config_reload")]
        self.last_change = changes[-1] if changes else self.last_change
        acpw = self.acpw()
        for key in config.SPEC:
            row = self.query_one("#row-" + key.replace(".", "--"), SettingRow)
            cur = self.cfg.get(key)
            info = fmt.text(f"now {value_text(key, cur, acpw):<14.14}", "bold")
            info.append(f" def {value_text(key, self.defaults.get(key)):<10.10}", DIM)
            info.append(f" {bounds_text(key)}", DIM)
            row.query_one(".set-info", Static).update(info)
            if key in self.keep:
                continue
            ed = row.query_one(".set-editor")
            val = editor_value(key, cur)
            if isinstance(ed, Select) and ed.value != val:
                ed.value = val
            elif isinstance(ed, Input) and ed.value != val and not ed.has_focus:
                ed.value = val
        self.render_badge()
        self.render_preview()

    def acpw(self) -> Any:
        return (self.status.get("derived") or {}).get("acpw_median_10s")

    def show(self, status: dict[str, Any]) -> None:
        self.status = status
        if self.digest and status.get("config_digest") not in (None, self.digest) and not self.loading:
            self.reload()   # changed elsewhere (turbomacctl, SIGHUP)
        self.render_preview()

    def render_badge(self) -> None:
        t = fmt.text("config ", DIM)
        t.append(self.digest or DASH, "bold")
        t.append("  ")
        be = self.app.backend
        note = self.persist_note or (Text("not persisted (replay)", fmt.AMBER) if be.kind == "replay"
                                     else Text("persisted", fmt.GREEN))
        t.append_text(note)
        t.append(f"  last change {fmt.hms(self.last_change)}  ", DIM)
        t.append("Enter applies · d defaults · Esc leaves a field", DIM)
        t.append(f"  {self.path}", DIM)
        self.query_one("#cfg-badge", Static).update(t, layout=False)

    def pending(self, key: str) -> Any:
        """The editor's value if it parses to a number, else the config value."""
        try:
            ed = self.query_one("#ed-" + key.replace(".", "--"), Input)
            v = parse(key, ed.value)
            return v if fmt.isnum(v) else self.cfg.get(key)
        except Exception:  # noqa: BLE001 - not mounted yet
            return self.cfg.get(key)

    def render_preview(self) -> None:
        acpw = self.acpw()
        ceil, static = self.pending("governor.ceiling_w"), self.pending("governor.static_cap_w")
        ratios = {n: self.pending(f"governor.{n}_ratio") for n in ("recover", "shed", "emergency")}
        th = ({f"{n}_w": acpw * r for n, r in ratios.items()}
              if fmt.isnum(acpw) and all(fmt.isnum(r) for r in ratios.values()) else None)
        t = fmt.text("preview  ", "bold")
        if fmt.isnum(ceil):
            p = predicted_input_w(ceil)
            t.append(f"ceiling {fmt.g(ceil)} ≈ {fmt.w(p)} input")
            t.append(f" ({p / acpw * 100:.0f} % of ACPW {fmt.w(acpw)})" if fmt.isnum(acpw) and acpw else " (ACPW –)",
                     fmt.band_style(p, th))
        if fmt.isnum(static):
            t.append(f"   static {fmt.g(static)} ≈ {fmt.w(predicted_input_w(static))}", DIM)
        t.append("\nbands    ", "bold")
        for name, r in ratios.items():
            t.append(f"{name} {r:g} → {fmt.w(acpw * r)}   " if th else f"{name} {DASH}   ", DIM)
        t.append(f"of ACPW {fmt.w(acpw)}   model: input ≈ 20.9 W + 1.24 × PL1", DIM)
        self.query_one("#cfg-preview", Static).update(t, layout=False)

    # editing ------------------------------------------------------------------------
    def key_of(self, widget: Widget) -> str:
        return (widget.id or "")[3:].replace("--", ".")

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id and event.input.id.startswith("ed-"):
            self.render_preview()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if not (event.input.id or "").startswith("ed-"):
            return
        event.stop()
        key = self.key_of(event.input)
        self.apply({key: parse(key, event.value)})

    def on_select_changed(self, event: Select.Changed) -> None:
        # Only a user's choice applies: programmatic syncs and the mount-time default arrive without focus.
        if not (event.select.id or "").startswith("ed-") or not self.cfg or not event.select.has_focus_within:
            return
        key = self.key_of(event.select)
        value = parse(key, event.value)
        if value == self.cfg.get(key) and type(value) is type(self.cfg.get(key)):
            return
        if key == "gpu.mode":
            self.gpu_mode(str(value))
        else:
            self.apply({key: value})

    def row(self, key: str) -> SettingRow:
        return self.query_one("#row-" + key.replace(".", "--"), SettingRow)

    def apply(self, updates: dict[str, Any]) -> None:
        for k in updates:
            if k in config.SPEC:
                self.row(k).result(Text("applying…", fmt.AMBER))
        self.app.call_backend(lambda: self.app.backend.config_set(updates), lambda r, e: self.applied(updates, r, e))

    def applied(self, updates: dict[str, Any], res: Any, err: str | None) -> None:
        if err:
            res = {"accepted": {}, "rejected": {k: f"daemon: {err}" for k in updates}, "persisted": True}
        persisted = res.get("persisted", True)
        for key in config.SPEC:
            if key in res.get("accepted", {}):
                self.keep.discard(key)
                msg = "✓ applied · saved" if persisted else f"✓ applied live · NOT saved: {res.get('error')}"
                self.row(key).result(Text(msg, fmt.GREEN if persisted else fmt.AMBER))
            elif key in res.get("rejected", {}):
                self.keep.add(key)
                self.row(key).result(Text(f"REJECTED: {res['rejected'][key]}", "bold " + fmt.RED))
        gpu = res.get("gpu")
        if gpu:
            self.row("gpu.mode").result(Text(f"{'✓' if gpu.get('ok') else 'FAILED'} {gpu.get('mode')}: "
                                             f"{gpu.get('reason')}", fmt.GREEN if gpu.get("ok") else fmt.RED))
        if res.get("accepted"):
            self.persist_note = (Text("persisted", fmt.GREEN) if persisted
                                 else Text(f"NOT persisted: {res.get('error')}", fmt.AMBER))
        self.reload()

    def gpu_mode(self, mode: str) -> None:
        if mode != "off":
            self.gpu_apply(mode)
            return

        def answer(ok: bool | None) -> None:
            if ok:
                self.gpu_apply("off")
            else:
                self.row("gpu.mode").result(Text("off cancelled", DIM))
                self.query_one("#ed-gpu--mode", Select).value = self.cfg.get("gpu.mode") or "auto"
        self.app.push_screen(GpuOffScreen(), answer)

    def gpu_apply(self, mode: str) -> None:
        self.row("gpu.mode").result(Text(f"applying {mode}…", fmt.AMBER))

        def done(res: Any, err: str | None) -> None:
            if err:
                self.row("gpu.mode").result(Text(f"REJECTED: {err}", "bold " + fmt.RED))
            else:
                ok = res.get("ok")
                self.row("gpu.mode").result(Text(f"{'✓' if ok else 'FAILED'} {res.get('mode')}: {res.get('reason')}",
                                                 fmt.GREEN if ok else fmt.RED))
            self.reload()
        self.app.call_backend(lambda: self.app.backend.gpu_set(mode), done)

    def action_reset_defaults(self) -> None:
        def answer(ok: bool | None) -> None:
            if ok:
                self.keep.clear()
                self.app.call_backend(self.app.backend.config_reset, lambda r, e: self.applied({}, r, e))
        self.app.push_screen(ConfirmScreen("Reset every setting to the defaults (PLAN 4.7)? "
                                           "gpu.mode returns to auto."), answer)
