"""Profiles tool — Phase 0 identity setup (shipping / billing / payment / fingerprint)."""
from __future__ import annotations

from nicegui import ui

from raindance.core.registry import ToolPlugin, register_tool


@register_tool
class ProfilesTool(ToolPlugin):
    id = "profiles"
    name = "Profiles"
    icon = "badge"
    order = 5
    description = "Checkout identities: address, payment, fingerprint seed."

    def render(self, ctx) -> None:
        store = ctx.profile_store
        ui.label("Profiles").classes("text-2xl font-bold")
        ui.markdown(
            "**Phase 0** — each profile is a unique identity: browser fingerprint seed, "
            "shipping/billing, and payment. Tasks bind to a profile + proxy group. "
            "Data is stored under `data/profiles/` (gitignored)."
        ).classes("opacity-70")

        selected = {"id": None}
        form_box = ui.column().classes("w-full gap-2")
        list_box = ui.column().classes("w-full gap-1")

        def _reload_list():
            list_box.clear()
            items = store.list()
            with list_box:
                if not items:
                    ui.label("No profiles — create one.").classes("opacity-60")
                for p in items:
                    with ui.row().classes("items-center gap-2"):
                        ui.button(
                            p["name"],
                            on_click=lambda pid=p["id"]: _edit(pid),
                        ).props("flat no-caps dense")
                        ui.label(p.get("email") or p["id"]).classes("text-xs opacity-50")
                        ui.button(
                            icon="delete",
                            on_click=lambda pid=p["id"]: _delete(pid),
                        ).props("flat dense color=red")

        def _delete(pid: str):
            store.delete(pid)
            selected["id"] = None
            form_box.clear()
            _reload_list()
            ui.notify("Deleted", type="info")

        def _edit(pid: str | None):
            if pid:
                prof = store.get(pid) or store.create("New profile")
            else:
                prof = store.create("New profile")
            selected["id"] = prof["id"]
            form_box.clear()
            with form_box:
                ui.label(f"Editing: {prof['id']}").classes("text-sm opacity-60")
                name = ui.input("Name", value=prof.get("name") or "").classes("w-full")
                email = ui.input("Email", value=prof.get("email") or "").classes("w-full")
                phone = ui.input("Phone", value=prof.get("phone") or "").classes("w-full")
                seed = ui.number(
                    "Fingerprint seed",
                    value=int(prof.get("fingerprint_seed") or 0),
                ).classes("w-full")
                udir = ui.input(
                    "Browser profile dir (persistent cookies)",
                    value=prof.get("user_data_dir") or "",
                ).classes("w-full")

                ship = prof.get("shipping") or {}
                ui.label("Shipping").classes("font-semibold mt-2")
                s_first = ui.input("First name", value=ship.get("first_name") or "").classes("w-full")
                s_last = ui.input("Last name", value=ship.get("last_name") or "").classes("w-full")
                s_a1 = ui.input("Address 1", value=ship.get("address1") or "").classes("w-full")
                s_a2 = ui.input("Address 2", value=ship.get("address2") or "").classes("w-full")
                s_city = ui.input("City", value=ship.get("city") or "").classes("w-full")
                s_state = ui.input("State", value=ship.get("state") or "").classes("w-full")
                s_zip = ui.input("ZIP", value=ship.get("zip") or "").classes("w-full")

                pay = prof.get("payment") or {}
                ui.label("Payment").classes("font-semibold mt-2")
                ui.label("Stored locally only. Prefer dry-run until selectors are verified.").classes(
                    "text-xs opacity-50"
                )
                p_name = ui.input("Name on card", value=pay.get("card_name") or "").classes("w-full")
                p_num = ui.input("Card number", value=pay.get("card_number") or "").props(
                    "type=password"
                ).classes("w-full")
                p_exp = ui.input("Expiry MM/YY", value=pay.get("expiry") or "").classes("w-full")
                p_cvc = ui.input("CVC", value=pay.get("cvc") or "").props("type=password").classes(
                    "w-full"
                )

                def _save():
                    prof["name"] = name.value
                    prof["email"] = email.value
                    prof["phone"] = phone.value
                    prof["fingerprint_seed"] = int(seed.value or 0)
                    prof["user_data_dir"] = udir.value or f"data/browser_profiles/{prof['id']}"
                    prof["shipping"] = {
                        "first_name": s_first.value,
                        "last_name": s_last.value,
                        "address1": s_a1.value,
                        "address2": s_a2.value,
                        "city": s_city.value,
                        "state": s_state.value,
                        "zip": s_zip.value,
                        "country": "US",
                    }
                    prof["billing"] = {**prof["shipping"], "same_as_shipping": True}
                    prof["payment"] = {
                        "card_name": p_name.value,
                        "card_number": p_num.value,
                        "expiry": p_exp.value,
                        "cvc": p_cvc.value,
                    }
                    store.save(prof)
                    ui.notify("Profile saved", type="positive")
                    _reload_list()

                ui.button("Save profile", icon="save", color="primary", on_click=_save)

        with ui.row().classes("gap-2"):
            ui.button("New profile", icon="add", color="primary",
                      on_click=lambda: _edit(None))
            ui.button("Refresh", icon="refresh", on_click=_reload_list).props("outline")

        ui.label("Saved profiles").classes("text-sm opacity-60 mt-2")
        _reload_list()
        ui.separator()
        ui.label("Editor").classes("text-sm opacity-60")
        # show form area
