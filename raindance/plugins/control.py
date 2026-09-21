"""Control panel — Phase 1 Start / Stop for the multi-task orchestrator."""
from __future__ import annotations

from nicegui import ui

from raindance.core.registry import ToolPlugin, register_tool


@register_tool
class ControlTool(ToolPlugin):
    id = "control"
    name = "Control"
    icon = "play_circle"
    order = 1
    description = "Start / stop the bot. Phase 1 entry point."

    def render(self, ctx) -> None:
        orch = ctx.orchestrator
        d = ctx.settings.data

        ui.label("Control").classes("text-2xl font-bold")
        ui.markdown(
            "**Phase 1** — load managers and arm tasks. Monitoring (Phase 2) runs "
            "until stock, then parallel checkout sessions fire (Phases 3–8). "
            "Configure **Profiles**, **Tasks**, **Evasion/proxies**, and **CAPTCHA** first."
        ).classes("opacity-70")

        phase_lbl = ui.label("").classes("text-lg font-mono")
        stats = ui.row().classes("gap-3 text-sm flex-wrap")

        with ui.card().classes("w-full"):
            ui.label("Launch").classes("font-semibold")
            force = ui.switch(
                "Skip monitor — run all enabled tasks immediately (checkout-now)",
                value=False,
            )
            global_dry = ui.switch(
                "Global force dry-run (never place order)",
                value=bool((d.get("checkout") or {}).get("force_dry_run")),
            )

            def _save_dry():
                d.setdefault("checkout", {})["force_dry_run"] = bool(global_dry.value)
                ctx.settings.save()

            global_dry.on_value_change(lambda e: _save_dry())

            workers = ui.number(
                "Max parallel task workers",
                value=int((d.get("orchestrator") or {}).get("max_workers") or 8),
                min=1, max=64,
            ).classes("w-48")

            def _start():
                d.setdefault("orchestrator", {})["max_workers"] = int(workers.value or 8)
                d.setdefault("checkout", {})["force_dry_run"] = bool(global_dry.value)
                ctx.settings.save()
                orch.reload_managers()
                ok = orch.start(force_checkout=bool(force.value))
                ui.notify(
                    "Bot started" if ok else "Already running or failed — see log",
                    type="positive" if ok else "warning",
                )

            def _stop():
                orch.stop()
                ui.notify("Stop requested", type="info")

            def _reload():
                orch.reload_managers()
                ui.notify("Managers reloaded", type="positive")

            with ui.row().classes("gap-2 mt-2"):
                ui.button("Start Tasks", icon="play_arrow", color="primary",
                          on_click=_start).props("unelevated")
                ui.button("Stop", icon="stop", color="red", on_click=_stop).props("outline")
                ui.button("Reload managers", icon="refresh", on_click=_reload).props("flat")

        with ui.card().classes("w-full"):
            ui.label("Schedule (optional)").classes("font-semibold")
            start_at = ui.input(
                "Start at (HH:MM[:SS] or ISO)",
                value=(d.get("schedule") or {}).get("start_at") or "",
            ).classes("w-full")

            def _save_sched():
                d.setdefault("schedule", {})["start_at"] = start_at.value or ""
                ctx.settings.save()
                ui.notify("Schedule saved", type="positive")

            ui.button("Save schedule", icon="schedule", on_click=_save_sched).props("outline")

        with ui.card().classes("w-full"):
            ui.label("Live task board").classes("font-semibold")
            board = ui.column().classes("w-full gap-1")

        def _tick():
            snap = orch.snapshot()
            phase_lbl.set_text(
                f"{'● RUNNING' if snap['running'] else '○ idle'}  phase={snap.get('phase')}  "
                f"active={snap.get('active_tasks', 0)}  monitor="
                f"{'on' if snap.get('monitor_alive') else 'off'}"
            )
            stats.clear()
            with stats:
                ui.badge(f"tasks {snap.get('enabled_tasks')}/{snap.get('tasks')}", color="primary")
                ui.badge(f"profiles {snap.get('profiles')}", color="grey")
                total_px = sum(
                    (g or {}).get("count", 0)
                    for g in (snap.get("proxy_groups") or {}).values()
                )
                ui.badge(f"proxies {total_px}", color="grey")

            board.clear()
            with board:
                items = ctx.task_store.list() if ctx.task_store else []
                if not items:
                    ui.label("No tasks yet — add some on the Tasks page.").classes("opacity-60")
                for t in items:
                    st = t.get("status") or "idle"
                    color = {
                        "success": "green", "failed": "red", "monitoring": "blue",
                        "checkout": "orange", "atc": "orange", "captcha": "purple",
                        "launching": "orange", "triggered": "amber", "armed": "grey",
                    }.get(st, "grey")
                    with ui.row().classes("items-center gap-2 w-full no-wrap"):
                        ui.badge(st, color=color)
                        ui.label(t.get("name") or t.get("id")).classes("text-sm grow")
                        ui.label(t.get("message") or "").classes("text-xs opacity-50")

        ui.timer(0.6, _tick)
        _tick()
