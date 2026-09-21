"""CAPTCHA settings — Phase 0/5 harvester + optional solver keys."""
from __future__ import annotations

from nicegui import ui

from raindance.core.registry import ToolPlugin, register_tool


@register_tool
class CaptchaTool(ToolPlugin):
    id = "captcha"
    name = "CAPTCHA"
    icon = "smart_toy"
    order = 36
    description = "Manual harvester windows or solver API keys."

    def render(self, ctx) -> None:
        d = ctx.settings.data
        cap = d.setdefault("captcha", {
            "mode": "manual",
            "timeout_seconds": 300,
            "poll_seconds": 1.5,
            "capsolver_key": "",
            "twocaptcha_key": "",
        })

        ui.label("CAPTCHA").classes("text-2xl font-bold")
        ui.markdown(
            "**Phase 5** — when a challenge appears, the task pauses and keeps the "
            "headed browser open so you can solve it (manual harvester). Optional "
            "API keys are stored for Capsolver / 2Captcha; site-specific token "
            "injection still falls back to the manual window when unsure."
        ).classes("opacity-70")

        with ui.card().classes("w-full"):
            mode = ui.select(
                {"manual": "Manual harvester (recommended)", "api": "API (when supported)"},
                value=cap.get("mode") or "manual",
                label="Mode",
            ).classes("w-full")
            timeout = ui.number(
                "Solve timeout (seconds)",
                value=float(cap.get("timeout_seconds") or 300),
                min=30, max=1800,
            ).classes("w-full")
            poll = ui.number(
                "Poll interval (seconds)",
                value=float(cap.get("poll_seconds") or 1.5),
                min=0.5, max=10,
            ).classes("w-full")

        with ui.card().classes("w-full"):
            ui.label("Solver API keys (optional)").classes("font-semibold")
            cap_key = ui.input(
                "Capsolver key",
                value=cap.get("capsolver_key") or "",
            ).props("type=password").classes("w-full")
            two_key = ui.input(
                "2Captcha key",
                value=cap.get("twocaptcha_key") or "",
            ).props("type=password").classes("w-full")

        def _save():
            cap["mode"] = mode.value or "manual"
            cap["timeout_seconds"] = float(timeout.value or 300)
            cap["poll_seconds"] = float(poll.value or 1.5)
            cap["capsolver_key"] = (cap_key.value or "").strip()
            cap["twocaptcha_key"] = (two_key.value or "").strip()
            ctx.settings.save()
            if ctx.orchestrator:
                ctx.orchestrator.captcha.configure(d)
            ui.notify("CAPTCHA settings saved", type="positive")

        ui.button("Save", icon="save", color="primary", on_click=_save)

        ui.markdown(
            "Tip: keep **browser.headless = false** (or leave CAPTCHA on manual) so "
            "harvester windows are visible. The runner forces headed mode for manual solves."
        ).classes("text-xs opacity-50 mt-2")
