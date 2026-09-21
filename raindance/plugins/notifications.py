"""Notifications tool — a settings panel generated from whatever providers are
registered. It reads each provider's declared `config_fields`, so a brand-new
provider needs zero UI code: drop the file, its fields appear here."""
from __future__ import annotations

from nicegui import ui

from raindance.core.notifications import NotificationEvent
from raindance.core.registry import ToolPlugin, register_tool


@register_tool
class NotificationsTool(ToolPlugin):
    id = "notifications"
    name = "Notifications"
    icon = "notifications"
    order = 40
    description = "Configure alert channels. Discord is one provider; add more freely."

    def render(self, ctx) -> None:
        ui.label("Notifications").classes("text-2xl font-bold")
        ui.markdown("Discord is just one provider. Add Slack / Telegram / email by "
                    "dropping a file in `raindance/providers/`.").classes("opacity-70")

        # Read-only view of persisted config for the widgets' initial values.
        notif = ctx.settings.data.get("notifications", {})
        fields: list = []  # (provider_id, key, widget)

        for prov in ctx.hub.providers():
            store = notif.get(prov.id, {})
            with ui.card().classes("w-full"):
                ui.label(prov.name).classes("font-semibold")
                ui.label(prov.description).classes("text-xs opacity-60 mb-1")
                for f in prov.config_fields():
                    key, typ = f["key"], f.get("type", "text")
                    if typ == "bool":
                        w = ui.switch(f["label"], value=bool(store.get(key, False)))
                    elif typ == "secret":
                        w = ui.input(f["label"], value=store.get(key, ""),
                                     placeholder=f.get("placeholder", "")).props("type=password").classes("w-full")
                    else:
                        w = ui.input(f["label"], value=store.get(key, ""),
                                     placeholder=f.get("placeholder", "")).classes("w-full")
                    fields.append((prov.id, key, w))
                ui.button("Send test", icon="send",
                          on_click=lambda p=prov: _test(p)).props("flat")

        def _collect() -> dict:
            """Gather current widget values WITHOUT touching persisted settings,
            so a 'Send test' or unsaved edit never leaks to config.json."""
            cfg: dict = {}
            for pid, key, w in fields:
                cfg.setdefault(pid, {})[key] = w.value
            return cfg

        def _save():
            cfg = _collect()
            ctx.settings.data["notifications"] = cfg   # commit only on explicit Save
            ctx.settings.save()
            ctx.hub.configure(cfg)
            ui.notify("Notifications saved", type="positive")

        def _test(prov):
            ctx.hub.configure(_collect())              # transient; not persisted
            if not prov.is_ready():
                ui.notify(f"{prov.name} is off or not fully configured", type="warning")
                return
            try:
                prov.send(NotificationEvent("info", "RainDance test", "Alerts are working ✓"))
                ui.notify(f"Sent a test via {prov.name}", type="positive")
            except Exception as e:  # a failing channel must not crash the handler
                ui.notify(f"{prov.name} test failed: {e}", type="negative")
                if ctx.bus:
                    ctx.bus.log(f"{prov.name} test failed: {e}", "warn")

        ui.button("Save notifications", icon="save", color="primary", on_click=_save)
