"""CAPTCHA handling — manual harvester windows + optional solver API keys.

Preferred path (manual): when a challenge is detected, the task pauses in
STATUS_CAPTCHA, a headed browser window stays open, and we poll until the
challenge disappears (user solved it). Optional Capsolver/2Captcha keys can
be stored for future auto-solve hooks.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Optional

# Common selectors / frame names that indicate a CAPTCHA is present.
_CAPTCHA_HINTS = [
    "iframe[src*='recaptcha']",
    "iframe[src*='hcaptcha']",
    "iframe[title*='reCAPTCHA']",
    "iframe[title*='hCaptcha']",
    ".g-recaptcha",
    # Confirmed live against a real, active PerimeterX challenge on Target:
    # the element is <iframe id="px-captcha-modal" style="display: block;
    # ...z-index: 2147483647">. The exact-id selector "#px-captcha" cannot
    # match "px-captcha-modal" — CSS id selectors require an exact match,
    # not a prefix — so this returned False while the challenge sat fully
    # visible on screen, and add_to_cart() sailed past it. [id*='px-captcha']
    # catches that id and any other px-captcha-prefixed variant.
    "[id*='px-captcha' i]",
    "[class*='px-captcha' i]",
    "iframe[src*='px-captcha']",
    "iframe[src*='perimeterx']",
    "[data-callback*='captcha']",
    "text=Verify you are human",
    "text=Are you a human",
    "text=Access denied",
    "text=Press & Hold",
    "text=Press and hold",
]


class CaptchaService:
    def __init__(self, settings_data: dict, bus=None):
        self.bus = bus
        cfg = (settings_data.get("captcha") or {})
        self.mode = cfg.get("mode") or "manual"  # manual | api
        self.timeout_s = float(cfg.get("timeout_seconds") or 300)
        self.poll_s = float(cfg.get("poll_seconds") or 1.5)
        self.capsolver_key = (cfg.get("capsolver_key") or "").strip()
        self.twocaptcha_key = (cfg.get("twocaptcha_key") or "").strip()

    def configure(self, settings_data: dict) -> None:
        cfg = (settings_data.get("captcha") or {})
        self.mode = cfg.get("mode") or "manual"
        self.timeout_s = float(cfg.get("timeout_seconds") or 300)
        self.poll_s = float(cfg.get("poll_seconds") or 1.5)
        self.capsolver_key = (cfg.get("capsolver_key") or "").strip()
        self.twocaptcha_key = (cfg.get("twocaptcha_key") or "").strip()

    def log(self, msg: str, level: str = "info") -> None:
        if self.bus:
            self.bus.log(msg, level)

    def is_present(self, page) -> bool:
        """Best-effort CAPTCHA detection on the current page."""
        for sel in _CAPTCHA_HINTS:
            try:
                if sel.startswith("text="):
                    if page.get_by_text(sel[5:], exact=False).count() > 0:
                        loc = page.get_by_text(sel[5:], exact=False).first
                        if loc.is_visible():
                            return True
                else:
                    el = page.query_selector(sel)
                    if el and el.is_visible():
                        return True
            except Exception:
                continue
        return False

    def wait_manual(
        self,
        page,
        *,
        task_id: str = "",
        should_stop: Optional[Callable[[], bool]] = None,
        on_waiting: Optional[Callable[[], None]] = None,
    ) -> bool:
        """Block until CAPTCHA is gone or timeout/stop. Returns True if cleared."""
        self.log(
            f"[captcha] task={task_id or '?'} challenge detected — "
            f"solve it in the browser window (manual harvester)",
            "warn",
        )
        if on_waiting:
            try:
                on_waiting()
            except Exception:
                pass
        deadline = time.monotonic() + self.timeout_s
        while time.monotonic() < deadline:
            if should_stop and should_stop():
                self.log(f"[captcha] task={task_id} stop requested during solve", "warn")
                return False
            try:
                if not self.is_present(page):
                    self.log(f"[captcha] task={task_id} cleared", "ok")
                    return True
            except Exception as e:
                self.log(f"[captcha] poll error: {e}", "warn")
            time.sleep(self.poll_s)
        self.log(f"[captcha] task={task_id} timed out after {self.timeout_s:.0f}s", "err")
        return False

    def handle_if_present(
        self,
        page,
        *,
        task_id: str = "",
        should_stop: Optional[Callable[[], bool]] = None,
        on_waiting: Optional[Callable[[], None]] = None,
    ) -> bool:
        """If CAPTCHA present, wait for manual solve. Returns False if failed."""
        try:
            present = self.is_present(page)
        except Exception:
            present = False
        if not present:
            return True
        if self.mode == "api" and (self.capsolver_key or self.twocaptcha_key):
            self.log(
                f"[captcha] task={task_id} API mode configured but auto-solve "
                f"injection is site-specific — falling back to manual window",
                "warn",
            )
        return self.wait_manual(
            page, task_id=task_id, should_stop=should_stop, on_waiting=on_waiting
        )
