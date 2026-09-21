"""Watch — the wait. A countdown, the board, and the log.

Statuses here are the ones the scanner actually returns. A bot-protected
retailer reports `error`, not a comforting "out of stock", and a price the
parser could not read shows as an em-dash. The UI does not improve on the truth.
"""
from __future__ import annotations

from datetime import datetime

from nicegui import ui

from auto_checkout import parse_start_at
from raindance.core.registry import ToolPlugin, register_tool
from raindance.ui import theme

_NOTE = (
    "<b>These states are honest.</b> JS-heavy or bot-protected retailers block a plain "
    "HTTP fetch, so they report <code>Error</code> rather than a fake “out of stock”. "
    "A price reads <code>—</code> when the parser finds no dollar amount with cents. "
    "Both are real limits of the check, not display bugs."
)


def _countdown(start_at: str) -> str:
    """Seconds remaining as HH:MM:SS, or a word when there is nothing to count."""
    if not start_at:
        return ""
    try:
        target = parse_start_at(start_at)
    except ValueError:
        return ""
    if not target:
        return ""
    left = int((target - datetime.now()).total_seconds())
    if left <= 0:
        return ""
    h, rem = divmod(left, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


@register_tool
class WatchTool(ToolPlugin):
    id = "watch"
    name = "Watch"
    icon = "visibility"
    order = 3
    stage = True
    description = "Countdown to the drop, and the live state of every armed task."

    def render(self, ctx) -> None:
        theme.inject()
        theme.title("Watch")

        start_at = (ctx.settings.data.get("schedule") or {}).get("start_at") or ""

        with ui.row().classes("items-baseline gap-4 flex-wrap"):
            clock = ui.label("").classes("rd-clock rd-num")
            with ui.column().classes("gap-0"):
                when = ui.label("").classes("rd-lab")
                armed = ui.label("").style("color:var(--rd-ink-dim);font-size:13px")

        @ui.refreshable
        def board() -> None:
            tasks = ctx.task_store.list() if ctx.task_store else []
            if not tasks:
                theme.note("Nothing armed yet. Build a plan on <b>Arm</b>.")
                return
            by_url = {p["url"]: p for p in ctx.store.items}
            with theme.card():
                with ui.element("div").classes("w-full").style("overflow-x:auto"):
                    with ui.element("table").classes("w-full").style("border-collapse:collapse"):
                        with ui.element("thead"):
                            with ui.element("tr"):
                                for h in ("Item", "Retailer", "Status", "Price", "Checked"):
                                    cell = ui.element("td").classes("rd-lab").style(
                                        "padding:10px 14px;text-align:left;"
                                        "border-bottom:1px solid var(--rd-line)")
                                    with cell:
                                        ui.label(h)
                        with ui.element("tbody"):
                            for t in tasks:
                                p = by_url.get(t.get("url"), {})
                                with ui.element("tr"):
                                    for value in (t.get("name") or t.get("url"),
                                                  p.get("site") or "—"):
                                        with ui.element("td").style(
                                                "padding:12px 14px;font-size:13.5px;"
                                                "border-bottom:1px solid var(--rd-line-soft)"):
                                            ui.label(str(value))
                                    with ui.element("td").style(
                                            "padding:12px 14px;"
                                            "border-bottom:1px solid var(--rd-line-soft)"):
                                        theme.chip(t.get("status") or "idle")
                                    price = p.get("price")
                                    for value in (f"${price:.2f}" if price else "—",
                                                  p.get("last_checked") or "—"):
                                        with ui.element("td").classes("rd-mono rd-num").style(
                                                "padding:12px 14px;font-size:12.5px;"
                                                "color:var(--rd-ink-dim);"
                                                "border-bottom:1px solid var(--rd-line-soft)"):
                                            ui.label(str(value))

        board()

        theme.lab("Live log")
        logbox = ui.log(max_lines=600).classes("w-full").style(
            "height:180px;background:var(--rd-term-bg);color:var(--rd-term-ink);"
            "font-family:var(--rd-mono);font-size:11.5px;border:1px solid var(--rd-line);"
            "border-radius:5px")

        # Drain the backlog *before* replaying history, or every queued line that is
        # already in history gets printed twice.
        ctx.bus.drain()
        for line in ctx.bus.history[-200:]:
            logbox.push(f"[{line.ts}] {line.text}")

        theme.note(_NOTE)

        def _tick() -> None:
            for line in ctx.bus.drain():
                logbox.push(f"[{line.ts}] {line.text}")

            left = _countdown(start_at)
            snap = ctx.orchestrator.snapshot()
            if left:
                clock.set_text(left)
                when.set_text(f"until {start_at}")
            else:
                clock.set_text("—:—:—" if not snap.get("running") else "watching")
                when.set_text("watching now" if snap.get("running") else "not armed")
            armed.set_text(f"{snap.get('enabled_tasks', 0)} armed · "
                           f"{(ctx.settings.data.get('orchestrator') or {}).get('max_workers', 8)} workers")
            board.refresh()

        _tick()
        ui.timer(1.0, _tick)
