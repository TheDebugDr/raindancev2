"""Set up — pick a retailer, see exactly what you're watching there.

The first lifecycle stage. A single dropdown of every retailer RainDance can
detect sits above one flat product list; choosing a retailer re-renders the list
underneath instantly. This is where a watched product's retailer becomes a
checkout handler, so every row names the handler it would route to.
"""
from __future__ import annotations

from nicegui import ui

from raindance.core import run_plan
from raindance.core.registry import ToolPlugin, register_tool
from raindance.core.sites import known_retailers
from raindance.ui import theme

_ALL = "__all__"


@register_tool
class SetupTool(ToolPlugin):
    id = "setup"
    name = "Set up"
    icon = "tune"
    order = 1
    stage = True
    description = "Every supported retailer, and what you're watching at each."

    def render(self, ctx) -> None:
        theme.inject()
        store = ctx.store

        known = known_retailers()
        grouped = store.by_site()
        active = sum(1 for s in known if grouped.get(s))
        watched = len(store.items)

        # -- header ---------------------------------------------------------- #
        theme.title("Set up")
        theme.lede("Pick a retailer to see what you're watching there and which "
                   "handler would check it out. Everything is added on Monitors.")

        with ui.row().classes("items-center justify-between w-full"):
            theme.lab(f"Retailers · {active} active of {len(known)} supported")
            theme.lab(f"{watched} items watched")

        # -- retailer dropdown ---------------------------------------------- #
        def _count_label(site: str) -> str:
            n = len(grouped.get(site, []))
            if n == 0:
                return f"{site} (no items)"
            return f"{site} ({n} item{'s' if n != 1 else ''})"

        options = {_ALL: f"All retailers ({watched} item{'s' if watched != 1 else ''})"}
        for site in known:
            options[site] = _count_label(site)

        retailer_select = (ui.select(options, value=_ALL)
                           .props("outlined dense")
                           .style("width:18rem"))

        # -- product list (re-renders on select change) --------------------- #
        def _row(p: dict, show_retailer: bool) -> None:
            handler = run_plan.handler_for(p.get("site", ""))
            freq = theme.esc(p.get("frequency") or "—")
            if show_retailer:
                meta = (f"{theme.esc(p.get('site', 'Unknown'))} · every {freq} · "
                        f"handler <code>{handler}</code>")
            else:
                meta = f"every {freq} · handler <code>{handler}</code>"
            with ui.element("div").classes("rd-row"):
                with ui.column().classes("gap-1 min-w-0 grow"):
                    ui.label(p.get("name") or p.get("url") or "Untitled").classes("rd-pname")
                    ui.html(meta, sanitize=False).classes("rd-pmeta")   # escaped above
                theme.chip(p.get("status", "unknown"))

        @ui.refreshable
        def product_list() -> None:
            value = retailer_select.value or _ALL
            if value == _ALL:
                items = store.items
                show_retailer = True
            else:
                items = store.for_site(value)
                show_retailer = False

            if not items:
                if value == _ALL:
                    theme.note("No items watched yet. Add a product on Monitors — paste "
                               "a URL and RainDance detects the retailer automatically.")
                else:
                    handler = run_plan.handler_for(value)
                    theme.note(
                        "No items yet. Paste a product URL and RainDance detects the "
                        f"retailer automatically — it would route here as <code>{handler}</code>."
                    )
                return

            with theme.card():
                for p in items:
                    _row(p, show_retailer)

        retailer_select.on_value_change(lambda e: product_list.refresh())
        product_list()

        # -- live-order blocker --------------------------------------------- #
        blockers = run_plan.live_blockers(ctx.profile_store)
        if blockers:
            theme.block("<b>Live is blocked.</b> " + blockers[0] + " Dry runs still work.")
