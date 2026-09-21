"""Monitors page — the core dashboard.

Products are grouped into a tab per retailer (tabs are derived from the data, so
they're fully dynamic). Includes a multi-step add-product flow and a slide-over
discovery panel that searches the internet and routes each result to the right
tab automatically.
"""
from __future__ import annotations

from nicegui import ui

from raindance.core.registry import ToolPlugin, register_tool
from raindance.core.scanner import STATUS_META
from raindance.core.search import placeholder_image
from raindance.core.sites import detect_site

_FREQS = ["5s", "10s", "15s", "30s"]


@register_tool
class MonitorsTool(ToolPlugin):
    id = "monitors"
    name = "Monitors"
    icon = "inventory_2"
    order = 10
    description = "Products you're monitoring, grouped into a tab per retailer."

    def render(self, ctx) -> None:
        store = ctx.store
        live_refs: list[dict] = []          # per-card label refs, refreshed by a timer
        search_state = {"loaded": False}

        # -- header ---------------------------------------------------------- #
        ui.label("Monitors").classes("text-2xl font-bold")
        with ui.row().classes("items-center justify-between w-full"):
            ui.label("Products grouped by retailer — add manually or discover via search."
                     ).classes("opacity-70")
            with ui.row().classes("items-center gap-2"):
                ui.button("Add product", icon="add", color="primary",
                          on_click=lambda: _open_add())
                ui.button("Search products", icon="travel_explore",
                          on_click=lambda: _open_search()).props("outline")

        tabs_container = ui.column().classes("w-full gap-2")

        # -- add-product dialog (multi-step) -------------------------------- #
        with ui.dialog() as add_dialog, ui.card().classes("w-[28rem] max-w-full"):
            ui.label("Add a product to monitor").classes("text-lg font-bold")
            with ui.stepper().props("vertical").classes("w-full") as stepper:
                with ui.step("Product"):
                    name_i = ui.input("Product name").classes("w-full")
                    url_i = ui.input("Product URL", placeholder="https://retailer.com/...").classes("w-full")
                    with ui.stepper_navigation():
                        ui.button("Next", on_click=lambda: _step1_next())
                with ui.step("Monitoring"):
                    thr_i = ui.number("Price threshold ($) — alert at or below").classes("w-full")
                    freq_i = ui.select(_FREQS, value="30s", label="Check frequency").classes("w-full")
                    with ui.stepper_navigation():
                        ui.button("Back", on_click=stepper.previous).props("flat")
                        ui.button("Next", on_click=lambda: (_update_review(), stepper.next()))
                with ui.step("Review"):
                    review = ui.label("").classes("text-sm whitespace-pre-line")
                    with ui.stepper_navigation():
                        ui.button("Back", on_click=stepper.previous).props("flat")
                        ui.button("Add product", color="primary", on_click=lambda: _submit_add())

        # -- discovery slide-over ------------------------------------------- #
        backdrop = (ui.element("div")
                    .style("position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:40;display:none")
                    .on("click", lambda: _close_search()))
        panel = ui.column().classes("gap-3").style(
            "position:fixed;top:0;right:0;height:100vh;width:26rem;max-width:100vw;z-index:50;"
            "background:#0f172a;color:#e5e7eb;padding:16px;box-shadow:-8px 0 24px rgba(0,0,0,.4);"
            "transform:translateX(100%);transition:transform .25s ease;overflow:auto")
        with panel:
            with ui.row().classes("items-center justify-between w-full"):
                ui.label("🔎 Discover products").classes("text-lg font-bold")
                ui.button(icon="close", on_click=lambda: _close_search()).props("flat dense round")
            source_lbl = ui.label(f"source: {ctx.search.provider_name}").classes("text-xs opacity-60")
            with ui.expansion("Search source / SerpApi key").classes("w-full").props("dense"):
                key_i = (ui.input("SerpApi API key",
                                  value=ctx.settings.data.get("search", {}).get("serpapi_key", ""),
                                  password=True, password_toggle_button=True)
                         .props("dense outlined dark").classes("w-full"))
                ui.label("Set a key for live Google Shopping results; empty = offline mock. "
                         "Get one at serpapi.com.").classes("text-xs opacity-50")

                def _save_key():
                    ctx.settings.data.setdefault("search", {})["serpapi_key"] = (key_i.value or "").strip()
                    ctx.settings.save()
                    source_lbl.set_text(f"source: {ctx.search.provider_name}")
                    ui.notify(f"Saved — searches use {ctx.search.provider_name}", type="positive")

                ui.button("Save key", icon="save", on_click=_save_key).props("dense")
            with ui.row().classes("w-full no-wrap gap-2 items-center"):
                query_i = (ui.input(placeholder="Search Pokémon products…")
                           .props("dense outlined dark").classes("grow")
                           .on("keydown.enter", lambda: _do_search()))
                ui.button(icon="search", on_click=lambda: _do_search()).props("dense")
            results_box = ui.column().classes("w-full gap-2")

        # -- handlers -------------------------------------------------------- #
        def _open_add():
            stepper.value = "Product"
            add_dialog.open()

        def _step1_next():
            if not (url_i.value or "").strip():
                ui.notify("Enter a product URL", type="warning")
                return
            stepper.next()

        def _update_review():
            site = detect_site(url_i.value) if url_i.value else "—"
            thr = thr_i.value
            review.set_text(
                f"Name:      {name_i.value or url_i.value or '—'}\n"
                f"Retailer:  {site}   ← its tab\n"
                f"URL:       {url_i.value or '—'}\n"
                f"Threshold: {('$' + str(thr)) if thr not in (None, '') else '—'}\n"
                f"Frequency: every {freq_i.value}")

        def _submit_add():
            if not (url_i.value or "").strip():
                ui.notify("Enter a product URL", type="warning")
                return
            store.add(name=name_i.value, url=url_i.value,
                      price_threshold=thr_i.value, frequency=freq_i.value)
            ui.notify(f"Added → {detect_site(url_i.value)}", type="positive")
            name_i.value = ""
            url_i.value = ""
            thr_i.value = None
            freq_i.value = "30m"
            stepper.value = "Product"
            add_dialog.close()
            _rebuild()

        def _open_search():
            panel.style("transform:translateX(0)")
            backdrop.style("display:block")
            if not search_state["loaded"]:
                search_state["loaded"] = True
                _do_search()

        def _close_search():
            panel.style("transform:translateX(100%)")
            backdrop.style("display:none")

        def _do_search():
            # Synchronous: the mock is in-memory and a live SerpApi call is a quick
            # request, so this avoids the async/coroutine handling pitfalls.
            results = ctx.search.search(query_i.value or "")
            results_box.clear()
            with results_box:
                if not results:
                    ui.label("No results.").classes("opacity-60")
                for r in results:
                    _result_card(r)

        def _result_card(r):
            holder: dict = {}

            def _add():
                store.add(name=r.name, url=r.url, price=r.price, site=r.site, image=r.image)
                btn = holder.get("btn")
                if btn:
                    btn.set_text("Added")
                    btn.props("disable")
                ui.notify(f"Added {r.name} → {r.site}", type="positive")
                _rebuild()

            ph = placeholder_image(r.name)
            src = r.image or ph
            # Raw <img> with an onerror fallback: a broken/blocked thumbnail
            # degrades to the placeholder instead of a broken-image icon.
            img_html = (
                f'<img src="{src}" referrerpolicy="no-referrer" loading="lazy" '
                f'onerror="this.onerror=null;this.src=\'{ph}\'" '
                'style="width:56px;height:56px;min-width:56px;object-fit:cover;'
                'border-radius:8px;background:#0f172a">')
            with ui.card().classes("w-full").style("background:#1e293b"):
                with ui.row().classes("items-center gap-3 no-wrap w-full"):
                    ui.html(img_html, sanitize=False)  # trusted markup; keep onerror fallback
                    with ui.column().classes("gap-1 min-w-0 grow"):
                        ui.label(r.name).classes("text-sm font-medium").style("white-space:normal")
                        with ui.row().classes("items-center gap-2"):
                            ui.badge(r.site, color="primary")
                            ui.label(f"${r.price:.2f}" if r.price is not None else "—").classes("text-sm opacity-80")
                    holder["btn"] = ui.button("Add", icon="add", on_click=_add).props("dense unelevated")

        def _run_check(p):
            ctx.scanner.scan([p])
            ui.notify(f"Checking {p['name']}…")

        def _delete(p):
            store.remove(p["id"])
            ui.notify("Removed", type="info")
            _rebuild()

        # -- tab / card rendering ------------------------------------------- #
        def _product_card(p):
            ph = placeholder_image(p["name"])
            src = p.get("image") or ph
            thumb = (f'<img src="{src}" referrerpolicy="no-referrer" loading="lazy" '
                     f'onerror="this.onerror=null;this.src=\'{ph}\'" '
                     'style="width:48px;height:48px;min-width:48px;object-fit:cover;'
                     'border-radius:6px;background:#0f172a">')
            with ui.card().classes("w-full").props("flat bordered"):
                with ui.row().classes("items-start justify-between w-full no-wrap"):
                    with ui.row().classes("items-start gap-3 min-w-0 no-wrap"):
                        ui.html(thumb, sanitize=False)
                        with ui.column().classes("gap-1 min-w-0"):
                            with ui.row().classes("items-center gap-2"):
                                ui.label(p["name"]).classes("font-semibold")
                                lbl, color = STATUS_META.get(p["status"], (p["status"], "grey"))
                                badge = ui.badge(lbl, color=color)
                            ui.link(p["url"], p["url"]).classes("text-xs opacity-60 break-all").props("target=_blank")
                            with ui.row().classes("items-center gap-3 text-xs opacity-70"):
                                price_lbl = ui.label("—")
                                thr = p.get("price_threshold")
                                ui.label(f"threshold ${thr}" if thr not in (None, "") else "no threshold")
                                ui.label(f"every {p.get('frequency', '—')}")
                                checked_lbl = ui.label("never checked")
                    with ui.column().classes("items-end gap-1"):
                        ui.button(icon="refresh", on_click=lambda p=p: _run_check(p)).props("flat dense").tooltip("Run check now")
                        ui.button(icon="delete", on_click=lambda p=p: _delete(p)).props("flat dense color=red").tooltip("Delete")
            live_refs.append({"p": p, "badge": badge, "price": price_lbl, "checked": checked_lbl})

        def _rebuild():
            live_refs.clear()
            tabs_container.clear()
            with tabs_container:
                # stats
                with ui.row().classes("items-center gap-4 text-sm opacity-70"):
                    ui.label(f"{len(store.items)} product(s)")
                    ui.label(f"{len(store.sites())} retailer(s)")
                if not store.items:
                    with ui.card().classes("w-full items-center").style("padding:2rem"):
                        ui.icon("inventory_2", size="42px").classes("opacity-40")
                        ui.label("No products yet").classes("text-lg")
                        ui.label("Add one, or open Search to discover popular Pokémon products."
                                 ).classes("opacity-60")
                    return
                sites = store.sites()
                grouped = store.by_site()
                with ui.tabs().props("align=left active-color=primary").classes("w-full") as tabbar:
                    tab_objs = {s: ui.tab(s, label=f"{s}  ({len(grouped[s])})") for s in sites}
                with ui.tab_panels(tabbar, value=tab_objs[sites[0]]).classes("w-full"):
                    for s in sites:
                        with ui.tab_panel(tab_objs[s]).classes("gap-2"):
                            for p in grouped[s]:
                                _product_card(p)

        def _refresh_live():
            for ref in live_refs:
                p = ref["p"]
                lbl, color = STATUS_META.get(p["status"], (p["status"], "grey"))
                ref["badge"].set_text(lbl)
                ref["badge"].props(f"color={color}")
                price = p.get("price")
                ref["price"].set_text(f"${price:.2f}" if isinstance(price, (int, float)) else "—")
                ref["checked"].set_text(f"checked {p['last_checked']}" if p.get("last_checked") else "never checked")

        ui.timer(0.8, _refresh_live)
        _rebuild()
