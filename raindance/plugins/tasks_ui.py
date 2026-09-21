"""Tasks tool — Phase 0 task configuration + run-now controls."""
from __future__ import annotations

from nicegui import ui

from raindance.core.registry import ToolPlugin, register_tool
from raindance.sites import list_sites


@register_tool
class TasksTool(ToolPlugin):
    id = "tasks"
    name = "Tasks"
    icon = "playlist_add_check"
    order = 6
    description = "Configure product tasks: URL, profile, proxy group, mode."

    def render(self, ctx) -> None:
        ts = ctx.task_store
        ps = ctx.profile_store
        orch = ctx.orchestrator

        ui.label("Tasks").classes("text-2xl font-bold")
        ui.markdown(
            "**Phase 0** — each task is one purchase attempt: product URL/PID, "
            "quantity, bound **profile**, **proxy group**, site handler, and mode "
            "(guest vs account). Enable tasks, then hit **Start Tasks** on Control."
        ).classes("opacity-70")

        table_box = ui.column().classes("w-full gap-2")

        with ui.card().classes("w-full"):
            ui.label("Add task").classes("font-semibold")
            name = ui.input("Name", placeholder="PC Ascended Heroes ETB").classes("w-full")
            url = ui.input("Product URL", placeholder="https://www.pokemoncenter.com/...").classes("w-full")
            pid = ui.input("Product ID (optional)", placeholder="PID").classes("w-full")
            qty = ui.number("Quantity", value=1, min=1, max=10).classes("w-32")
            profiles = {p["id"]: p["name"] for p in ps.list()} or {"": "(create a profile)"}
            prof = ui.select(profiles, label="Profile",
                             value=next(iter(profiles))).classes("w-full")
            groups = (ctx.orchestrator.proxy_groups.group_names()
                      if orch else ["default"])
            pgroup = ui.select(
                {g: g for g in groups} or {"default": "default"},
                value="default",
                label="Proxy group",
            ).classes("w-full")
            site = ui.select(
                {s: s for s in list_sites()},
                value="pokemon_center",
                label="Site handler",
            ).classes("w-full")
            mode = ui.select(
                {"guest": "Guest checkout", "account": "Account"},
                value="guest",
                label="Mode",
            ).classes("w-full")
            monitor = ui.switch("Monitor for stock first (Phase 2)", value=True)
            dry = ui.switch("Dry-run (stop before place order)", value=True)
            retries = ui.number("Max retries on failure", value=2, min=0, max=10).classes("w-40")

            def _add():
                if not (url.value or "").strip() and not (pid.value or "").strip():
                    ui.notify("URL or product ID required", type="warning")
                    return
                t = ts.add(
                    name=name.value or url.value or pid.value,
                    url=(url.value or "").strip(),
                    product_id=(pid.value or "").strip(),
                    quantity=int(qty.value or 1),
                    profile_id=prof.value or "",
                    proxy_group=pgroup.value or "default",
                    site=site.value or "generic",
                    mode=mode.value or "guest",
                    monitor=bool(monitor.value),
                    dry_run=bool(dry.value),
                    max_retries=int(retries.value or 0),
                    enabled=True,
                )
                ui.notify(f"Added {t['id']}", type="positive")
                name.value = ""
                url.value = ""
                _rebuild()

            ui.button("Add task", icon="add", color="primary", on_click=_add)

        def _rebuild():
            table_box.clear()
            with table_box:
                items = ts.list()
                if not items:
                    ui.label("No tasks configured.").classes("opacity-60")
                    return
                for t in items:
                    with ui.card().classes("w-full").props("flat bordered"):
                        with ui.row().classes("items-start justify-between w-full no-wrap"):
                            with ui.column().classes("gap-1 min-w-0 grow"):
                                with ui.row().classes("items-center gap-2"):
                                    ui.label(t.get("name") or t["id"]).classes("font-semibold")
                                    ui.badge(t.get("status") or "idle")
                                    if t.get("enabled", True):
                                        ui.badge("on", color="green")
                                    else:
                                        ui.badge("off", color="grey")
                                ui.label(t.get("url") or t.get("product_id") or "").classes(
                                    "text-xs opacity-60 break-all"
                                )
                                ui.label(
                                    f"site={t.get('site')} · mode={t.get('mode')} · "
                                    f"profile={t.get('profile_id')} · proxy={t.get('proxy_group')} · "
                                    f"qty={t.get('quantity')} · "
                                    f"{'monitor' if t.get('monitor') else 'instant'} · "
                                    f"{'dry-run' if t.get('dry_run') else 'LIVE'}"
                                ).classes("text-xs opacity-70")
                                if t.get("last_error"):
                                    ui.label(t["last_error"]).classes("text-xs text-red-400")
                                if t.get("order_id"):
                                    ui.label(f"order: {t['order_id']}").classes(
                                        "text-xs text-green-400"
                                    )
                            with ui.column().classes("gap-1"):
                                def _toggle(tid=t["id"], cur=t.get("enabled", True)):
                                    ts.update(tid, enabled=not cur)
                                    _rebuild()

                                def _run(tid=t["id"]):
                                    orch.run_task_now(tid)
                                    ui.notify("Task submitted", type="info")

                                def _del(tid=t["id"]):
                                    ts.remove(tid)
                                    _rebuild()

                                ui.button(
                                    "Disable" if t.get("enabled", True) else "Enable",
                                    on_click=_toggle,
                                ).props("flat dense")
                                ui.button("Run now", icon="bolt", on_click=_run).props(
                                    "flat dense"
                                )
                                ui.button(icon="delete", on_click=_del).props(
                                    "flat dense color=red"
                                )

        ui.label("Configured tasks").classes("text-sm opacity-60 mt-2")
        _rebuild()
        ui.timer(1.0, _rebuild)
