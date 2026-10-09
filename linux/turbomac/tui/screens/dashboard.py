"""Screen 1: the dashboard (PLAN 5 sketch)."""
from __future__ import annotations

import statistics
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Label

from turbomac.daemon.governor import predicted_input_w
from turbomac.daemon.sampler import EXTRA_SMC_KEYS
from turbomac.tui import fmt
from turbomac.tui.fmt import DIM, DASH, num, w
from turbomac.tui.widgets import Panel, SparkRow


def gpu_owner(gpu: dict[str, Any]) -> str:
    sw = gpu.get("switcheroo") or {}
    for key, name in (("DIS", "dGPU (Radeon)"), ("IGD", "iGPU (Intel)")):
        if str(sw.get(key, "")).startswith("+"):
            return name
    return DASH


def gpu_text(gpu: dict[str, Any], width: int, cap_bar: bool = True) -> Text:
    """Shared by the dashboard GPU panel and the GPU screen."""
    t = Text()
    live, cfg = gpu.get("mode"), gpu.get("config_mode")
    off = live == "off"
    t.append(w(gpu.get("power_w")) if not off else "OFF · not polled", "bold")
    t.append(f"  cap {fmt.g(gpu.get('cap_w'))}", DIM)
    if cap_bar and fmt.isnum(gpu.get("power_w")) and fmt.isnum(gpu.get("cap_w")) and gpu["cap_w"] > 0:
        t.append(" ")
        t.append_text(fmt.bar(gpu["power_w"] / gpu["cap_w"], max(3, width - len(t.plain) - 1), fmt.CYAN))
    t.append("\nmode ", DIM)
    t.append(str(live or DASH).upper(), "bold " + (fmt.GREY if off else fmt.CYAN))
    t.append(f"  config {cfg or DASH}", DIM if cfg == live else fmt.AMBER)
    if gpu.get("busy"):
        t.append("  CHANGING", fmt.AMBER)
    t.append("\n")
    fmt.kv(t, "", num(gpu.get("temp_c"), "°C", 0), fmt.temp_style(gpu.get("temp_c")))
    fmt.kv(t, "busy", num(gpu.get("busy_pct"), "%", 0))
    t.append(num(gpu.get("sclk_mhz"), "MHz", 0), "bold")
    t.append("\n")
    fmt.kv(t, "dpm", str(gpu.get("dpm_level") or DASH))
    fmt.kv(t, "runtime", str(gpu.get("runtime_status") or DASH), end="")
    t.append("\n")
    fmt.kv(t, "PCI", str(gpu.get("power_state") or DASH))
    fmt.kv(t, "present", {True: "yes", False: "no"}.get(gpu.get("present"), DASH), end="")
    sw = gpu.get("switcheroo") or {}
    t.append("\nsw ", DIM)
    t.append("  ".join(f"{k} {v}" for k, v in sw.items() if not k.endswith("Audio")) or DASH)
    t.append("\npanel owner ", DIM)
    t.append(gpu_owner(gpu), "bold")
    return t


class InputPanel(Panel):
    """INPUT gauge against ACPW with band markers and the PL-predicted input."""

    def build(self, s: dict[str, Any], width: int, height: int) -> Text:
        lat, der, th, gov = (s.get(k) or {} for k in ("latest", "derived", "thresholds", "governor"))
        inp = der.get("input_mean_1s")
        acpw = der.get("acpw_median_10s") or lat.get("acpw_w")
        scale = max(x for x in (acpw, inp, 80.0) if fmt.isnum(x)) if fmt.isnum(acpw) else max(80.0, inp or 0)
        style = fmt.band_style(inp, th or None)
        wide = width >= 100
        head = fmt.text("INPUT ", "bold")
        head.append(f"{w(inp):>7}", "bold " + style)
        head.append(" ▕")
        tail = Text("▏ ")
        tail.append(w(acpw), "bold")
        tail.append(" ACPW", DIM)
        if wide:
            tail.append(f"   {num(lat.get('v'), 'V', 2)} × {num(lat.get('a'), 'A', 2)}")
        tail.append("   headroom " if wide else " · hr ", DIM)
        tail.append(w(der.get("headroom_w")), "bold " + style)
        bw = max(10, width - len(head.plain) - len(tail.plain))
        gauge = Text()
        filled = (inp / scale * bw) if fmt.isnum(inp) else 0.0
        for i in range(bw):
            watts = (i + 0.5) / bw * scale
            zone = fmt.band_style(watts, th or None)
            if i < int(filled):
                gauge.append("█", zone)
            elif i == int(filled) and filled - int(filled) > 0.05:
                gauge.append(fmt.EIGHTHS[max(1, int((filled - int(filled)) * 8))], zone)
            else:
                gauge.append("░", fmt.EMPTY if zone == fmt.GREY else f"{zone} dim")
        line1 = head + gauge + tail
        pl1 = gov.get("pl1_w")
        pred = predicted_input_w(pl1) if fmt.isnum(pl1) else None
        marks = [" "] * bw
        styles = [""] * bw
        for value, ch, st in ((th.get("recover_w"), "▲", fmt.GREEN), (th.get("shed_w"), "▲", fmt.AMBER),
                              (th.get("emergency_w"), "▲", fmt.RED), (pred, "◆", fmt.CYAN)):
            if fmt.isnum(value):
                p = min(bw - 1, max(0, int(value / scale * bw)))
                marks[p], styles[p] = ch, st
        line2 = Text(" " * len(head.plain))
        for ch, st in zip(marks, styles):
            line2.append(ch, st)
        if wide:
            line2.append(f"  {fmt.pd_contract(acpw, lat.get('v'))} · 1 s max {w(der.get('input_max_1s'))}", DIM)
        line3 = Text(" " * (len(head.plain) - 2) if wide else "")
        for label, key, st in (("recover", "recover_w", fmt.GREEN), ("shed", "shed_w", fmt.AMBER),
                               ("emergency", "emergency_w", fmt.RED)):
            line3.append("▲ ", st)
            line3.append(f"{label} {w(th.get(key))}  " if wide else f"{w(th.get(key))}  ", DIM)
        line3.append("◆ ", fmt.CYAN)
        line3.append(f"PL1 {fmt.g(pl1)} ≈ {w(pred)} predicted input" if wide else f"{w(pred)}", DIM)
        return Text("\n").join([line1, line2, line3])


class CpuPanel(Panel):
    def build(self, s: dict[str, Any], width: int, height: int) -> Text:
        lat, cpu, msr, gov = (s.get(k) or {} for k in ("latest", "cpu", "msr", "governor"))
        lim = s.get("limits") or {}
        sysfs, ml = lim.get("sysfs") or {}, lim.get("msr") or {}
        temps = cpu.get("temps") or {}
        t = Text()
        fmt.kv(t, "package", w(lat.get("package_w")), "bold " + fmt.BLUE)
        t.append(f"PL1 {fmt.g(gov.get('pl1_w'))} · PL2 {fmt.g(gov.get('pl2_w'))}\n", "bold")
        for label, key in (("core", "core_w"), ("uncore", "uncore_w"), ("dram", "dram_w")):
            fmt.kv(t, label, w(lat.get(key)), "")
        mhz = [m for m in cpu.get("mhz") or [] if fmt.isnum(m)]
        line = Text()
        fmt.kv(line, "", num(statistics.fmean(mhz) / 1000, "GHz", 2) if mhz else DASH)
        fmt.kv(line, "busy", num(cpu.get("busy_pct"), "%", 0))
        fmt.kv(line, "pkg", num(temps.get("pkg_c"), "°C", 0), fmt.temp_style(temps.get("pkg_c")), end="")
        tot, av = cpu.get("mem_total_mib"), cpu.get("mem_avail_mib")
        mem = f"{(tot - av) / 1024:.1f}/{num(tot / 1024, 'GiB')}" if fmt.isnum(tot) and fmt.isnum(av) else ""
        if mem and len(line.plain) + len(mem) + 6 <= width:
            fmt.kv(line, "  mem", mem, "", end="")
        t.append("\n")
        t.append_text(line)
        t.append("\n")
        t.append_text(fmt.yes(msr.get("power_limited"), "power-limited", fmt.AMBER))
        t.append("  ")
        t.append_text(fmt.yes(msr.get("thermal"), "thermal", fmt.RED))
        t.append("  ")
        t.append_text(fmt.yes(msr.get("prochot"), "PROCHOT", fmt.RED))
        t.append("\nsysfs ", DIM)
        t.append(f"{fmt.g(sysfs.get('pl1_w'))}/{fmt.g(sysfs.get('pl2_w'))}")
        t.append(" · MSR ", DIM)
        t.append(f"{fmt.g(ml.get('pl1_w'))}/{fmt.g(ml.get('pl2_w'))}")
        if ml.get("pl1_clamp"):
            t.append(" clamp", DIM)
        if ml.get("lock"):
            t.append(" LOCKED", fmt.RED)
        freqs, busy = cpu.get("mhz") or [], cpu.get("busy_per_cpu") or []
        cols = 2 if width >= 28 else 1
        cell = (width - 2 * (cols - 1)) // cols
        pct = cell >= 20
        bw = max(2, cell - (17 if pct else 11))
        for i, f in enumerate(freqs):
            t.append("\n" if i % cols == 0 else "  ")
            b = busy[i] if i < len(busy) else None
            t.append(f"c{i:<2}", DIM)
            t.append_text(fmt.bar((b or 0) / 100, bw, fmt.busy_style(b)))
            t.append(f"{num(f / 1000 if fmt.isnum(f) else None, 'GHz', 1):>8}")
            if pct:
                t.append(f"{num(b, '%', 0):>6}", DIM)
        return t


class GpuPanel(Panel):
    def build(self, s: dict[str, Any], width: int, height: int) -> Text:
        gpu = s.get("gpu") or {}
        t = gpu_text(gpu, width)
        res = gpu.get("last_result")
        if res:
            t.append("\nlast ", DIM)
            t.append(f"{res.get('mode')} {'ok' if res.get('ok') else 'FAILED'}", fmt.GREEN if res.get("ok") else fmt.RED)
        return t


class GovPanel(Panel):
    def build(self, s: dict[str, Any], width: int, height: int) -> Text:
        gov, der = s.get("governor") or {}, s.get("derived") or {}
        c = gov.get("counters") or {}
        mode = str(gov.get("mode") or DASH)
        t = fmt.badge(gov.get("state"))
        t.append(f" for {fmt.dur(gov.get('in_state_s'))}")
        t.append("  mode ", DIM)
        t.append(mode.upper(), "bold " + (fmt.AMBER if mode == "off" else ""))
        t.append("\n")
        fmt.kv(t, "PL1", fmt.g(gov.get("pl1_w")), "bold " + fmt.BLUE)
        t.append("→ ceiling ", DIM)
        t.append(fmt.g(gov.get("ceiling_w")), "bold")
        t.append("\n")
        fmt.kv(t, "PL2", fmt.g(gov.get("pl2_w")), "")
        fmt.kv(t, "static", fmt.g(gov.get("static_cap_w")), "")
        fmt.kv(t, "floor", fmt.g(gov.get("floor_w")), "", end="")
        t.append(f"\n{c.get('cuts', 0)} cuts · {c.get('emergencies', 0)} emerg · {c.get('fallbacks', 0)} fallbacks")
        t.append(f"\n{c.get('recoveries', 0)} recov · {c.get('writes', 0)} writes · "
                 f"{c.get('readback_failures', 0)} rb fail")
        t.append("\n")
        fmt.kv(t, "non-CPU est", w(der.get("noncpu_est_w")), "", end="")
        t.append("\n")
        fmt.kv(t, "ceiling ≈", f"{w(der.get('pred_input_at_ceiling_w'))} input", "", end="")
        t.append("\nlast ", DIM)
        t.append(str(gov.get("last_action") or DASH))
        if gov.get("fallback_reason"):
            t.append("\nFALLBACK ", "bold " + fmt.MAGENTA)
            t.append(str(gov["fallback_reason"]), fmt.MAGENTA)
        elif mode == "off":
            t.append("\nOFF: telemetry only, no limits enforced", fmt.AMBER)
        return t


class TempsPanel(Panel):
    def build(self, s: dict[str, Any], width: int, height: int) -> Text:
        temps = (s.get("cpu") or {}).get("temps") or {}
        cells = [("pkg", temps.get("pkg_c")), ("x86", temps.get("x86_pkg_c"))]
        cells += list((temps.get("cores") or {}).items()) + [("nvme", temps.get("nvme_c"))]
        cells += list((s.get("smc_temps") or {}).items())
        cells = [c for c in cells if c[1] is not None]
        cw = 11
        cols = max(1, (width + 1) // cw)
        cap = cols * height
        more = len(cells) - cap
        if more > 0:
            cells = cells[: cap - 1]
        t = Text()
        for i, (label, v) in enumerate(cells):
            if i and i % cols == 0:
                t.append("\n")
            t.append(f"{label:<5.5}", DIM)
            t.append(f"{num(v, '°C', 0, ''):>5} ", fmt.temp_style(v))
        if more > 0:
            t.append(f"+{more + 1} more", DIM)
        return t or Text(DASH)


class FansPanel(Panel):
    def build(self, s: dict[str, Any], width: int, height: int) -> Text:
        t = Text()
        for i, f in enumerate(s.get("fans") or []):
            rpm, lo, hi = f.get("rpm"), f.get("min"), f.get("max")
            label = str(f.get("label") or f"fan{i + 1}").replace(" side", "")
            if i:
                t.append("\n")
            t.append(f"{label:<6.6}", DIM)
            t.append(f"{num(rpm, 'rpm', 0):>9} ", "bold")
            frac = (rpm - lo) / (hi - lo) if all(fmt.isnum(x) for x in (rpm, lo, hi)) and hi > lo else None
            t.append_text(fmt.bar(frac, max(3, width - 16), fmt.CYAN))
            t.append(f"\n      {num(lo, '', 0)}–{num(hi, 'rpm', 0)}" + (" manual" if f.get("manual") else ""), DIM)
        return t or Text(DASH)


class RailsPanel(Panel):
    def build(self, s: dict[str, Any], width: int, height: int) -> Text:
        smc = s.get("smc") or {}
        keys = [k for k in EXTRA_SMC_KEYS if k in smc] or list(smc)
        t = Text()
        for i, k in enumerate(keys):
            t.append("\n" if i and i % 2 == 0 else ("  " if i else ""))
            t.append(f"{k} ", DIM)
            t.append(f"{w(smc.get(k)):>7}", "bold")
        return t or Text(DASH)


class TopPanel(Panel):
    def build(self, s: dict[str, Any], width: int, height: int) -> Text:
        t = Text()
        for i, p in enumerate(((s.get("cpu") or {}).get("top") or [])[:8]):
            if i:
                t.append("\n")
            t.append(f"{str(p.get('name'))[: max(4, width - 8)]:<{max(4, width - 8)}}")
            t.append(f"{num(p.get('cpu_pct'), '%', 0):>8}", fmt.busy_style((p.get("cpu_pct") or 0) / 8))
        return t or Text("no samples yet", DIM)


class PeaksPanel(Panel):
    """Session peaks, kept by the app (r resets)."""

    def build(self, s: dict[str, Any], width: int, height: int) -> Text:
        peaks, since = getattr(self.app, "peaks", {}), getattr(self.app, "peaks_since", None)
        t = fmt.text("PEAKS ", "bold")
        t.append(f"since {fmt.hms(since)}  ", DIM)
        items = (("input", "input", "W", 1), ("pkg", "package", "W", 1), ("gpu", "GPU", "W", 1),
                 ("temp", "pkg", "°C", 0), ("amps", "current", "A", 2))
        for i, (key, label, unit, nd) in enumerate(items):
            p = peaks.get(key)
            part = fmt.text(f"{label} ", DIM)
            part.append(num(p["value"], unit, nd) if p else DASH, "bold")
            part.append(f" @{fmt.hms(p['ts'])}" if p else "", DIM)
            if len(t.plain) + len(part.plain) + 3 > width:
                break
            t.append(" · " if i else "", DIM)
            t.append_text(part)
        return t


class Banner(Panel):
    """Incident / governor-off / connection warnings; hidden when there is nothing to say."""

    def build(self, s: dict[str, Any], width: int, height: int) -> Text:
        return Text("  ·  ").join(self.messages(s))

    def messages(self, s: dict[str, Any]) -> list[Text]:
        out = []
        be = getattr(self.app, "backend", None)
        if be is not None and not be.connected:
            out.append(Text(f"daemon unreachable: {be.last_error} (retrying)" if be.last_error
                            else "connecting to turbomacd…", "bold " + fmt.RED))
        ic = s.get("incident_check") or {}
        if ic.get("verdict") == "abrupt":
            out.append(Text(f"previous boot ended abruptly: incident {ic.get('incident_id') or DASH} (press 4)",
                            "bold " + fmt.RED))
        if (s.get("governor") or {}).get("mode") == "off":
            out.append(Text("governor OFF: telemetry only, no limits enforced", "bold " + fmt.AMBER))
        if s and not s.get("smc_ok", True):
            out.append(Text("SMC unavailable: no input telemetry", "bold " + fmt.RED))
        return out

    def show(self, status: dict[str, Any]) -> None:
        on = bool(self.messages(status))
        if self.display != on:
            self.display = on
        super().show(status)


class Dashboard(VerticalScroll):
    """Panels are duplicated where the narrow layout moves them; CSS shows one copy."""

    def compose(self) -> ComposeResult:
        yield Banner(id="banner")
        yield InputPanel(id="input-panel")
        with Horizontal(id="spark-head"):
            yield Label("HISTORY", classes="spark-label")
            yield Label("60 s", classes="spark-caption")
            yield Label("10 min", classes="spark-caption wide-only")
            yield Label("now    10 min range", classes="spark-stats")
        yield SparkRow("input", "W", (fmt.GREEN, fmt.RED), scale=(0.0, 80.0), id="sp-input")
        yield SparkRow("package", "W", (fmt.BLUE, fmt.MAGENTA), scale=(0.0, 45.0), id="sp-pkg")
        yield SparkRow("GPU", "W", (fmt.TEAL, fmt.CYAN), scale=(0.0, 15.0), id="sp-gpu")
        yield SparkRow("pkg temp", "°C", (fmt.YELLOW, fmt.RED), scale=(30.0, 100.0), id="sp-temp")
        with Horizontal(id="row-a", classes="dash-row"):
            yield CpuPanel("CPU", id="cpu-panel")
            yield GpuPanel("GPU · Radeon Pro 560", classes="wide-only", id="gpu-panel")
            yield GovPanel("GOVERNOR", id="gov-panel")
        with Horizontal(id="row-b", classes="dash-row"):
            yield GpuPanel("GPU · Radeon Pro 560", classes="narrow-only", id="gpu-panel-narrow")
            yield TempsPanel("TEMPS", classes="wide-only", id="temps-panel")
            with Vertical(id="fans-col"):
                yield FansPanel("FANS", id="fans-panel")
                yield RailsPanel("RAILS (SMC)", classes="wide-only", id="rails-panel")
            yield TopPanel("TOP CPU", classes="wide-only", id="top-panel")
        yield TempsPanel("TEMPS", classes="narrow-only", id="temps-panel-narrow")
        yield PeaksPanel(id="peaks-panel")

    def show(self, s: dict[str, Any], fast: dict[str, list[Any]], slow: dict[str, list[Any]]) -> None:
        for p in self.query(Panel):
            p.show(s)
        lat, der = s.get("latest") or {}, s.get("derived") or {}
        cur = {"input": der.get("input_mean_1s"), "pkg": lat.get("package_w"),
               "gpu": (s.get("gpu") or {}).get("power_w"), "temp": ((s.get("cpu") or {}).get("temps") or {}).get("pkg_c")}
        for key, wid, nd in (("input", "#sp-input", 1), ("pkg", "#sp-pkg", 1), ("gpu", "#sp-gpu", 1),
                             ("temp", "#sp-temp", 0)):
            self.query_one(wid, SparkRow).set(fast.get(key, []), slow.get(key, []), cur[key], nd)
