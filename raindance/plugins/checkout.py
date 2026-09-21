"""Checkout — one card per live session.

Phases 3–8 of a task's life, five segments wide. Only one of those steps needs a
human, so CAPTCHA gets the accent and plain instructions; everything else is the
machine talking to itself.
"""
from __future__ import annotations

from nicegui import ui

from raindance.core import run_plan
from raindance.core.registry import ToolPlugin, register_tool
from raindance.tasks import models as m
from raindance.ui import theme

# The five segments of the phase bar, in order.
_BAR = [m.STATUS_LAUNCHING, m.STATUS_QUEUED, m.STATUS_ATC,
        m.STATUS_CHECKOUT, m.STATUS_SUBMITTING]

# Statuses that mean "a session is on screen right now".
_ACTIVE = {m.STATUS_TRIGGERED, m.STATUS_LAUNCHING, m.STATUS_QUEUED, m.STATUS_ATC,
           m.STATUS_CAPTCHA, m.STATUS_CHECKOUT, m.STATUS_SUBMITTING, m.STATUS_RETRYING}

_SEG = {
    "done": "var(--rd-watch)",
    "captcha": "var(--rd-accent)",
    "todo": "var(--rd-line)",
}


def _reached(status: str) -> int:
    """How many phase segments this status has completed."""
    if status == m.STATUS_CAPTCHA:
        return _BAR.index(m.STATUS_ATC) + 1
    if status in _BAR:
        return _BAR.index(status) + 1
    if status == m.STATUS_TRIGGERED:
        return 0
    if status == m.STATUS_RETRYING:
        return 1
    return 0


def _modifier(status: str) -> str:
    if status == m.STATUS_CAPTCHA:
        return "rd-s-captcha"
    if status == m.STATUS_SUCCESS:
        return "rd-s-success"
    if status == m.STATUS_FAILED:
        return "rd-s-failed"
    return "rd-s-active"


@register_tool
class CheckoutTool(ToolPlugin):
    id = "checkout"
    name = "Checkout"
    icon = "shopping_cart_checkout"
    order = 4
    stage = True
    description = "Live checkout sessions, one card each."

    def render(self, ctx) -> None:
        theme.inject()
        theme.title("Checkout")
        theme.lede("One card per live session. The bar is the task lifecycle: "
                   "launch, queue, add to cart, checkout, submit.")

        @ui.refreshable
        def sessions() -> None:
            tasks = [t for t in (ctx.task_store.list() if ctx.task_store else [])
                     if (t.get("status") or "") in _ACTIVE]
            if not tasks:
                theme.note("No sessions running. Sessions appear here the moment a "
                           "watched item hits stock — arm a plan on <b>Arm</b>.")
                return

            by_url = {p["url"]: p for p in ctx.store.items}
            with ui.element("div").classes("w-full").style(
                    "display:grid;gap:14px;"
                    "grid-template-columns:repeat(auto-fill,minmax(258px,1fr))"):
                for t in tasks:
                    status = t.get("status") or "idle"
                    product = by_url.get(t.get("url"), {})
                    site = product.get("site") or "Unknown"
                    with ui.element("div").classes(f"rd-card rd-sess {_modifier(status)}"):
                        ui.label(t.get("name") or t.get("url")).classes("font-semibold") \
                            .style("font-size:13.5px")
                        ui.label(f"{site} · {run_plan.handler_for(site)}").classes("rd-mono") \
                            .style("color:var(--rd-ink-faint);font-size:11.5px")

                        done = _reached(status)
                        with ui.row().classes("gap-1 w-full my-3 no-wrap"):
                            for i in range(len(_BAR)):
                                if status == m.STATUS_CAPTCHA and i == done - 1:
                                    colour = _SEG["captcha"]
                                elif i < done:
                                    colour = _SEG["done"]
                                else:
                                    colour = _SEG["todo"]
                                ui.element("div").classes("grow").style(
                                    f"height:3px;border-radius:2px;background:{colour}")

                        theme.chip(status)

                        if status == m.STATUS_CAPTCHA:
                            with ui.element("div").classes("rd-needyou mt-2"):
                                ui.label("Solve the challenge in the open browser window. "
                                         "The task resumes on its own once it clears.")
                        elif t.get("message"):
                            ui.label(t["message"]).classes("rd-pmeta mt-2")

        sessions()
        ui.timer(1.0, sessions.refresh)
