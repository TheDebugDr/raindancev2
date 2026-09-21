"""Orders tool — run browser monitor / checkout against a product.

This is the path that actually opens Playwright. When **Evasion** is enabled,
CheckoutEngine launches the browser through BrowserFactory (stealth + proxies).
HTTP-only stock scans on the Execute page are separate and do not use a browser.
"""
from __future__ import annotations

import json

from nicegui import ui

from raindance.core.registry import ToolPlugin, register_tool

_DEFAULT_IN_STOCK = {
    "text_absent": "Sold Out",
    "text_present": "Add to Cart",
    "timeout_ms": 8000,
}


@register_tool
class OrdersTool(ToolPlugin):
    id = "orders"
    name = "Orders"
    icon = "shopping_cart"
    order = 12
    description = "Browser watch + checkout for a product (uses evasion when enabled)."

    def render(self, ctx) -> None:
        d = ctx.settings.data
        store = ctx.store
        engine = ctx.engine
        evasion = d.get("evasion") or {}

        ui.label("Orders").classes("text-2xl font-bold")
        ui.markdown(
            "Process a restock with a **real browser**. Toggle **Evasion** in the "
            "sidebar first if you want stealth + proxies applied to this session. "
            "Dry-run is the default — the final place-order step is skipped unless "
            "you explicitly go live."
        ).classes("opacity-70")

        # -- status strip ---------------------------------------------------- #
        with ui.row().classes("items-center gap-3 w-full"):
            ev_lbl = ui.badge(
                "evasion ON" if evasion.get("enabled") else "evasion off",
                color="green" if evasion.get("enabled") else "grey",
            )
            eng_lbl = ui.label("").classes("text-sm opacity-70")
            proxy_lbl = ui.label("").classes("text-sm opacity-70")

        # -- product + mode -------------------------------------------------- #
        with ui.card().classes("w-full"):
            ui.label("Target product").classes("font-semibold")
            products = store.items
            options = {
                p["id"]: f"{p.get('site', '?')} — {p.get('name', p.get('url', ''))}"
                for p in products
            }
            if not options:
                ui.label("No products yet — add one on the Monitors page.").classes(
                    "text-sm opacity-60"
                )
                product_sel = None
            else:
                product_sel = ui.select(
                    options,
                    value=next(iter(options)),
                    label="Product",
                ).classes("w-full")

            mode = ui.toggle(
                {
                    "monitor": "Monitor (alert only)",
                    "checkout": "Checkout (dry-run / live)",
                },
                value="monitor",
            ).props("no-caps")

            live_sw = ui.switch(
                "LIVE — actually place the order (disables dry-run)",
                value=False,
            )
            ui.label(
                "LIVE only applies in Checkout mode. Monitor never buys."
            ).classes("text-xs opacity-50")

            with ui.row().classes("items-center gap-2 mt-2"):
                ui.button(
                    "Start",
                    icon="play_arrow",
                    color="primary",
                    on_click=lambda: _start(),
                ).props("unelevated")
                ui.button(
                    "Stop",
                    icon="stop",
                    color="red",
                    on_click=lambda: engine.stop(),
                ).props("outline")

        # -- stock detection + checkout steps -------------------------------- #
        with ui.card().classes("w-full"):
            ui.label("Stock detection (browser watch)").classes("font-semibold")
            ui.label(
                "Used when the bot polls the product page. Leave defaults if unsure."
            ).classes("text-xs opacity-60 mb-1")
            site = d.setdefault("site", {})
            in_stock = site.setdefault("in_stock", dict(_DEFAULT_IN_STOCK))
            sel_i = ui.input(
                "Buyable selector (optional CSS)",
                value=in_stock.get("selector") or "",
            ).classes("w-full")
            present_i = ui.input(
                "Text present when in stock",
                value=in_stock.get("text_present") or "",
            ).classes("w-full")
            absent_i = ui.input(
                "Text absent when in stock (e.g. Sold Out)",
                value=in_stock.get("text_absent") or "Sold Out",
            ).classes("w-full")

        with ui.card().classes("w-full"):
            ui.label("Checkout steps (JSON)").classes("font-semibold")
            ui.label(
                "Ordered actions after stock is found. Mark the place-order step "
                'with "final": true so dry-run stops before it.'
            ).classes("text-xs opacity-60 mb-1")
            steps_raw = json.dumps(site.get("checkout_steps") or [], indent=2)
            steps_ta = (
                ui.textarea(value=steps_raw)
                .classes("w-full font-mono text-xs")
                .props("rows=10 outlined")
            )

            def _save_site():
                in_stock.clear()
                if (sel_i.value or "").strip():
                    in_stock["selector"] = sel_i.value.strip()
                    in_stock["expect_enabled"] = True
                    in_stock["expect_visible"] = True
                if (present_i.value or "").strip():
                    in_stock["text_present"] = present_i.value.strip()
                if (absent_i.value or "").strip():
                    in_stock["text_absent"] = absent_i.value.strip()
                in_stock["timeout_ms"] = int(in_stock.get("timeout_ms") or 8000)
                try:
                    parsed = json.loads(steps_ta.value or "[]")
                    if not isinstance(parsed, list):
                        raise ValueError("checkout_steps must be a JSON array")
                    site["checkout_steps"] = parsed
                except Exception as e:  # noqa: BLE001
                    ui.notify(f"Invalid checkout steps JSON: {e}", type="negative")
                    return
                ctx.settings.save()
                ui.notify("Site / checkout config saved", type="positive")

            ui.button("Save checkout config", icon="save", on_click=_save_site).props(
                "outline"
            )

        # -- profile reminder ------------------------------------------------ #
        with ui.card().classes("w-full"):
            prof = d.get("browser", {}).get("user_data_dir") or "profile"
            ui.label(f"Browser profile: {prof}").classes("text-sm")
            ui.markdown(
                "Sign in once via **Login Session** so checkout reuses your cookies. "
                "When evasion is on, the same profile still applies — plus stealth "
                "and a sticky proxy for the account id set on the Evasion page."
            ).classes("text-xs opacity-60")

        def _build_cfg(product: dict) -> dict:
            # Persist stock fields into site before building bot config.
            stock = {}
            if (sel_i.value or "").strip():
                stock["selector"] = sel_i.value.strip()
                stock["expect_enabled"] = True
                stock["expect_visible"] = True
            if (present_i.value or "").strip():
                stock["text_present"] = present_i.value.strip()
            if (absent_i.value or "").strip():
                stock["text_absent"] = absent_i.value.strip()
            stock["timeout_ms"] = 8000
            # Prefer any product-level override, else site, else form values.
            in_stock_cfg = (
                product.get("in_stock")
                or d.get("site", {}).get("in_stock")
                or stock
                or dict(_DEFAULT_IN_STOCK)
            )
            target = {
                "name": product.get("name") or product.get("url", ""),
                "url": product.get("url", ""),
                "in_stock": in_stock_cfg,
            }
            return ctx.settings.bot_config(target)

        def _start():
            if product_sel is None:
                ui.notify("Add a product on Monitors first", type="warning")
                return
            pid = product_sel.value
            product = store.get(pid) if pid else None
            if not product or not product.get("url"):
                ui.notify("Select a product with a URL", type="warning")
                return
            # Keep factory in sync with latest toggle before launch.
            if ctx.browser_factory is not None:
                ctx.browser_factory.configure(
                    evasion_enabled=bool((d.get("evasion") or {}).get("enabled")),
                    proxy_manager=ctx.proxy_manager,
                )
                engine.browser_factory = ctx.browser_factory
                engine.account_id = (d.get("evasion") or {}).get("account_id") or "default"

            cfg = _build_cfg(product)
            m = mode.value or "monitor"
            live = bool(live_sw.value) and m == "checkout"
            if live:
                # Extra confirmation for live mode.
                ui.notify(
                    "LIVE mode — the final place-order step WILL run if selectors match",
                    type="warning",
                )
            ok = engine.start(cfg, mode=m, live=live)
            if ok:
                ui.notify(
                    f"Started {m}"
                    + (" LIVE" if live else " (safe)" if m == "checkout" else ""),
                    type="positive",
                )
            else:
                ui.notify("Could not start — see live log", type="warning")

        def _tick():
            on = bool((d.get("evasion") or {}).get("enabled"))
            # also reflect live factory flag
            if ctx.browser_factory is not None:
                on = bool(ctx.browser_factory.evasion_enabled)
            ev_lbl.set_text("evasion ON" if on else "evasion off")
            ev_lbl.props(f"color={'green' if on else 'grey'}")
            st = engine.state
            if engine.is_running:
                live_s = " LIVE" if st.get("live") else ""
                eng_lbl.set_text(f"● engine {st.get('mode') or 'running'}{live_s}")
            else:
                eng_lbl.set_text("engine idle")
            n = ctx.proxy_manager.count if ctx.proxy_manager else 0
            proxy_lbl.set_text(f"{n} proxy(ies) loaded")

        ui.timer(0.5, _tick)
        _tick()
