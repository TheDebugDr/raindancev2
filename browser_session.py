"""
browser_session — a small, reliable lifecycle wrapper around a persistent
Playwright browser profile.

Its whole job is to start/reuse/stop a browser cleanly so the rest of the
codebase doesn't have to juggle the Playwright driver process by hand. A
persistent profile keeps you logged in and holds cookies across runs, which
is the legitimately useful part of "reliable automation" here.

This class does NOT spoof the browser to look non-automated (no
`navigator.webdriver` patching, no `AutomationControlled` flag, no fake
`window.chrome`). Keeping a login warm is fine; defeating a retailer's
bot-detection is a different thing, and it isn't this module's job.

Usage:

    from browser_session import BrowserSession

    with BrowserSession(profile_dir="profile") as session:
        page = session.start(url="https://www.target.com").page
        # ... normal automation ...
        session.wait()   # polite pacing between requests, not human-mimicry
"""
from __future__ import annotations

import logging
import random
import time
from typing import Optional, Tuple

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    sync_playwright,
)

logger = logging.getLogger(__name__)


class BrowserSession:
    """Own one browser + its Playwright driver, and tear both down cleanly."""

    def __init__(
        self,
        profile_dir: str = "browser_profile",
        proxy: dict | None = None,
        headless: bool = False,
        channel: str | None = None,
        viewport: dict | None = None,
        timeout_ms: int = 30_000,
        args: list[str] | None = None,
    ):
        self.profile_dir = profile_dir
        self.proxy = proxy
        self.headless = headless
        self.channel = channel
        self.viewport = viewport
        self.timeout_ms = timeout_ms
        # No stealth/automation-hiding flags baked in — pass your own if you
        # have a legitimate need (e.g. "--disable-dev-shm-usage" in CI).
        self.args = list(args or [])

        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None

    # -- lifecycle --------------------------------------------------------- #
    def start(self, url: str | None = None) -> "BrowserSession":
        """Start a session, or reuse the running one. Optionally navigate.

        Returns self so callers can chain (`session.start().page`). Reusing an
        existing session opens a fresh page only if every page was closed — it
        never spins up a second browser on top of the first.
        """
        if self._context is not None:
            page = self._pick_page()
            if url:
                page.goto(url)
            return self

        self._playwright = sync_playwright().start()
        try:
            self._launch()
            page = self._pick_page()
            page.set_default_timeout(self.timeout_ms)
            if url:
                page.goto(url)
        except BaseException:
            # Launch failed after the driver started — don't leak the process.
            self._stop_driver()
            self._browser = self._context = None
            raise
        return self

    def _launch(self) -> None:
        opts: dict = {"headless": self.headless}
        if self.channel:
            opts["channel"] = self.channel
        if self.proxy:
            opts["proxy"] = self.proxy
        if self.viewport:
            opts["viewport"] = self.viewport
        if self.args:
            opts["args"] = self.args

        assert self._playwright is not None
        if self.profile_dir:
            logger.info("opening persistent profile %s", self.profile_dir)
            self._context = self._playwright.chromium.launch_persistent_context(
                self.profile_dir, **opts
            )
            # In Playwright 1.60 context.browser is a real Browser for a
            # persistent context too (older versions returned None). Closing
            # either the context or the browser tears the profile down.
            self._browser = self._context.browser
        else:
            logger.info("launching ephemeral chromium")
            ctx_opts = {"viewport": self.viewport} if self.viewport else {}
            self._browser = self._playwright.chromium.launch(**opts)
            self._context = self._browser.new_context(**ctx_opts)

    def _pick_page(self) -> Page:
        assert self._context is not None
        return self._context.pages[0] if self._context.pages else self._context.new_page()

    def close(self) -> None:
        """Idempotent: safe to call twice, and the object is reusable after."""
        for label, obj in (("context", self._context), ("browser", self._browser)):
            try:
                if obj is not None:
                    obj.close()
            except Exception as e:  # already-closed / crashed browser, etc.
                logger.debug("error closing %s: %s", label, e)
        self._stop_driver()
        self._browser = self._context = None

    def _stop_driver(self) -> None:
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception as e:
                logger.debug("error stopping playwright driver: %s", e)
            self._playwright = None

    # -- convenience ------------------------------------------------------- #
    @property
    def page(self) -> Page:
        if self._context is None:
            raise RuntimeError("session not started; call start() first")
        return self._pick_page()

    @property
    def context(self) -> BrowserContext:
        if self._context is None:
            raise RuntimeError("session not started; call start() first")
        return self._context

    def unpack(self) -> Tuple[Optional[Browser], BrowserContext, Page]:
        """Back-compat 3-tuple: (browser, context, page)."""
        return self._browser, self.context, self.page

    def wait(self, min_seconds: float = 0.4, max_seconds: float = 1.8) -> None:
        """Polite pause between requests. Plain uniform jitter — this is
        courtesy pacing / rate-limiting, not an attempt to look human."""
        time.sleep(random.uniform(min_seconds, max_seconds))

    def screenshot(self, path: str) -> None:
        self.page.screenshot(path=path)

    # -- context manager --------------------------------------------------- #
    def __enter__(self) -> "BrowserSession":
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
