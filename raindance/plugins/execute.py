"""Execute page — run stock/price checks and watch results.

Trigger a scan across all products or one retailer; progress and per-product
results stream to the live log, and the results table below updates in place.
"""
from __future__ import annotations

from nicegui import ui

from raindance.core.registry import ToolPlugin, register_tool
from raindance.core.scanner import STATUS_META

_COLUMNS = [
    {"name": "name", "label": "Product", "field": "name", "align": "left"},
    {"name": "site", "label": "Retailer", "field": "site", "align": "left"},
    {"name": "status", "label": "Status", "field": "status", "align": "left"},
    {"name": "price", "label": "Price", "field": "price", "align": "right"},
    {"name": "threshold", "label": "Threshold", "field": "threshold", "align": "right"},
    {"name": "checked", "label": "Last check", "field": "checked", "align": "left"},
]


@register_tool
class ExecuteTool(ToolPlugin):
    id = "execute"
    name = "Execute"
    icon = "bolt"
    order = 15
    description = "Run stock/price checks across your monitored products."

    def render(self, ctx) -> None:
        store = ctx.store
        scanner = ctx.scanner

        ui.label("Execute").classes("text-2xl font-bold")
        ui.label("Run checks across your monitored products. Results stream to the "
                 "live log and the table below.").classes("opacity-70")

        with ui.card().classes("w-full"):
            with ui.row().classes("items-center gap-3 w-full"):
                site_sel = ui.select(["All sites"] + store.sites(), value="All sites",
                                     label="Scope").classes("w-56")
                ui.button("Run once", icon="play_arrow", color="primary",
                          on_click=lambda: _run()).props("unelevated")
                ui.button("Stop", icon="stop", color="red",
                          on_click=lambda: _stop_all()).props("outline")
                progress = ui.label("").classes("text-sm opacity-70 ml-2")
            with ui.row().classes("items-center gap-3 w-full"):
                auto_sw = ui.switch("Auto-monitor — keep re-checking on each product's frequency",
                                    value=scanner.is_watching,
                                    on_change=lambda e: _toggle_watch(e.value))
                watch_lbl = ui.label("").classes("text-sm opacity-70")
            counts = ui.row().classes("items-center gap-4 text-sm")

        with ui.card().classes("w-full"):
            table = ui.table(columns=_COLUMNS, rows=[], row_key="id").classes("w-full").props("flat")

        ui.label("Scans do a real HTTP fetch and read stock/price from the page. "
                 "JS-heavy or bot-protected retailers may block it — reported honestly "
                 "as Unknown/Error rather than faked.").classes("text-xs opacity-50")

        def _toggle_watch(on):
            if on:
                scanner.start_watch(
                    lambda: [p for p in store.items
                             if site_sel.value in ("All sites", None) or p["site"] == site_sel.value])
            else:
                scanner.stop_watch()

        def _stop_all():
            # the red Stop halts everything: a one-shot scan AND the auto-monitor
            scanner.stop()
            scanner.stop_watch()
            auto_sw.value = False

        def _rows():
            rows = []
            for p in store.items:
                if site_sel.value not in ("All sites", None) and p["site"] != site_sel.value:
                    continue
                price = p.get("price")
                thr = p.get("price_threshold")
                rows.append({
                    "id": p["id"],
                    "name": p["name"],
                    "site": p["site"],
                    "status": STATUS_META.get(p["status"], (p["status"], ""))[0],
                    "price": f"${price:.2f}" if isinstance(price, (int, float)) else "—",
                    "threshold": f"${thr}" if thr not in (None, "") else "—",
                    "checked": p.get("last_checked") or "—",
                })
            return rows

        def _run():
            site = site_sel.value
            items = [p for p in store.items if site in ("All sites", None) or p["site"] == site]
            scanner.scan(items)

        def _tick():
            # keep the scope options in sync with dynamically-added retailers
            opts = ["All sites"] + store.sites()
            if site_sel.options != opts:
                site_sel.options = opts
                if site_sel.value not in opts:
                    site_sel.value = "All sites"
                site_sel.update()
            table.rows = _rows()
            table.update()
            st = scanner.state
            progress.set_text(f"scanning {st['done']}/{st['total']}…" if scanner.is_running
                              else ("done" if st["total"] else ""))
            watch_lbl.set_text("● auto-monitoring" if scanner.is_watching else "")
            # status counts
            tally: dict[str, int] = {}
            for p in store.items:
                tally[p["status"]] = tally.get(p["status"], 0) + 1
            counts.clear()
            with counts:
                if not tally:
                    ui.label("no products yet").classes("opacity-50")
                for status, n in tally.items():
                    lbl, color = STATUS_META.get(status, (status, "grey"))
                    ui.badge(f"{lbl}: {n}", color=color)

        ui.timer(0.8, _tick)
        _tick()
