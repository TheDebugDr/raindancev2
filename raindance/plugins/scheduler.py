"""Drop Timer tool — arm the monitor to start at a known drop time, with a live
countdown. It just sets schedule.start_at, which the backend already honours."""
from __future__ import annotations

from datetime import datetime

from nicegui import ui

import auto_checkout as backend
from raindance.core.registry import ToolPlugin, register_tool


@register_tool
class SchedulerTool(ToolPlugin):
    id = "scheduler"
    name = "Drop Timer"
    icon = "timer"
    order = 20
    description = "Countdown to an announced drop; arms the monitor to start then."

    def render(self, ctx) -> None:
        d = ctx.settings.data

        ui.label("Drop Timer").classes("text-2xl font-bold")
        ui.label("Arm the monitor to begin at a known drop time — like setting an "
                 "alarm.").classes("opacity-70")

        with ui.card().classes("w-full"):
            inp = ui.input("Start at (HH:MM[:SS] or full ISO)",
                           value=d["schedule"].get("start_at", "")).classes("w-full")
            countdown = ui.label("—").classes("text-lg font-mono mt-1")

            def tick():
                try:
                    target = backend.parse_start_at(inp.value)
                except Exception:
                    countdown.set_text("⚠ invalid time format")
                    return
                if not target:
                    countdown.set_text("no schedule set — Start begins immediately")
                    return
                secs = (target - datetime.now()).total_seconds()
                if secs <= 0:
                    countdown.set_text("armed time reached ✓")
                else:
                    h, rem = divmod(int(secs), 3600)
                    m, sec = divmod(rem, 60)
                    countdown.set_text(f"starts in {h:02d}:{m:02d}:{sec:02d}")

            ui.timer(1.0, tick)

            def save():
                d["schedule"]["start_at"] = inp.value
                ctx.settings.save()
                ui.notify("Schedule saved", type="positive")

            ui.button("Save", icon="save", color="primary", on_click=save)

        ui.markdown("The scheduled time applies to the **Orders** tool when you "
                    "press **Start** (browser watch / checkout).").classes("opacity-70 text-sm")
