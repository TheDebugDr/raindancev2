"""Results — what actually happened.

Outcomes currently scroll past in the log and are gone. A drop is worth a ledger:
what succeeded, what failed and why, and which order ids exist.
"""
from __future__ import annotations

from nicegui import ui

from raindance.core.registry import ToolPlugin, register_tool
from raindance.tasks import models as m
from raindance.ui import theme

# `idle` is technically terminal, but an untouched task is not a result.
_FINISHED = (m.STATUS_SUCCESS, m.STATUS_FAILED, m.STATUS_STOPPED)

_BORDER = {
    m.STATUS_SUCCESS: "var(--rd-ok)",
    m.STATUS_FAILED: "var(--rd-live)",
    # a deliberate refusal reads amber, not the red of something breaking
    "denied": "var(--rd-accent)",
    m.STATUS_STOPPED: "var(--rd-idle)",
}


def _initials(name: str) -> str:
    """Up to two letters standing in for a product image."""
    words = [w for w in str(name or "").replace("-", " ").split() if w]
    if not words:
        return "??"
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][:1] + words[1][:1]).upper()


def _explain(task: dict) -> str:
    status = task.get("status")
    if status == m.STATUS_SUCCESS:
        if task.get("dry_run", True):
            return "Dry run reached the final step — stopped before paying."
        oid = task.get("order_id")
        return f"Order placed · {oid}" if oid else "Order placed."
    if status == m.STATUS_FAILED:
        reason = task.get("last_error") or task.get("message")
        if task.get("denied"):
            # The gate refused the page on purpose. Say so plainly, without
            # repeating the "aborted before add-to-cart" prefix already in reason.
            why = (reason or "").split("—", 1)[-1].strip() or "the page was refused"
            return (f"Denied before add-to-cart: {why}. "
                    "Nothing was carted and nothing was bought.")
        return reason or "Failed — no reason recorded."
    if status == m.STATUS_STOPPED:
        return "Stopped before it finished."
    return task.get("message") or ""


@register_tool
class ResultsTool(ToolPlugin):
    id = "results"
    name = "Results"
    icon = "receipt_long"
    order = 5
    stage = True
    description = "The outcome of every task, once the drop is over."

    def render(self, ctx) -> None:
        theme.inject()
        theme.title("Results")
        theme.lede("What each task actually did. Successes, failures, and the reason "
                   "for every one.")

        def _clear() -> None:
            for t in [x for x in ctx.task_store.list() if x.get("status") in _FINISHED]:
                ctx.task_store.remove(t["id"])
            ui.notify("Cleared finished tasks", type="positive")
            ledger.refresh()

        @ui.refreshable
        def ledger() -> None:
            tasks = [t for t in (ctx.task_store.list() if ctx.task_store else [])
                     if t.get("status") in _FINISHED]
            if not tasks:
                theme.note("Nothing has finished yet. Results appear here after a run — "
                           "including a dry run, which records how far it got.")
                return

            by_url = {p["url"]: p for p in ctx.store.items}
            # A finished task names a product; where the catalog knows that
            # product's picture, show it. It may not — an older CatalogStore has
            # no image methods and a context may carry no catalog at all — so
            # `product_art` degrades to the set logo, then to the initials.
            catalog = getattr(ctx, "catalog", None)
            with theme.card():
                for t in tasks:
                    status = t.get("status")
                    product = by_url.get(t.get("url")) or {}
                    site = product.get("site") or "—"
                    with ui.element("div").classes("rd-item").style(
                            f"border-left:2px solid {_BORDER.get(status, 'transparent')};"
                            "padding-left:14px"):
                        theme.chip(status)
                        theme.thumb(theme.product_art(catalog,
                                                      product.get("set_id") or "",
                                                      product.get("line_id") or ""),
                                    _initials(t.get("name") or site),
                                    size="24px", classes="rd-thumb-in")
                        with ui.column().classes("gap-0 grow min-w-0"):
                            ui.label(t.get("name") or t.get("url")).classes("rd-pname")
                            ui.label(_explain(t)).classes("rd-pmeta")
                        ui.label(site).classes("rd-lab")

            with ui.row().classes("gap-2 mt-4"):
                ui.button("Clear results", on_click=_clear).props("outline no-caps")

        ledger()
        ui.timer(2.0, ledger.refresh)
