"""The execution engine.

It *wraps* the existing `auto_checkout.CheckoutBot` — it does not reimplement
any of its logic. It runs the bot on a daemon worker thread (Playwright's sync
API can't share the UI's asyncio loop), pipes the bot's log lines onto the event
bus, and stops it cooperatively via the backend's `should_stop` hook. The UI
thread stays fully responsive.

Alerts are redirected: the bot's built-in notifier is swapped for an adapter
that forwards to the provider-agnostic NotificationHub, so every configured
provider (Discord today, Slack tomorrow) receives the bot's events unchanged.

Browser launches (checkout + login session) go through an optional
BrowserFactory so the evasion layer (stealth/proxies) can be toggled live.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import auto_checkout as backend
from raindance.core.notifications import NotificationEvent


class _HubNotifier:
    """Same call surface as auto_checkout.Notifier, but fans out to the hub."""

    def __init__(self, hub):
        self.hub = hub

    def notify(self, event, title, message, url=None):
        self.hub.notify(NotificationEvent(kind=event, title=title, message=message, url=url))


class CheckoutEngine:
    def __init__(self, bus, hub, browser_factory=None, account_id: str = "default"):
        self.bus = bus
        self.hub = hub
        self.browser_factory = browser_factory
        self.account_id = account_id or "default"
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._login_thread: threading.Thread | None = None
        self._login_stop = threading.Event()
        self.state = {"running": False, "mode": None, "live": False}

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _sink(self, msg: str, level: str) -> None:
        self.bus.log(msg, level)

    def start(self, cfg: dict, mode: str = "monitor", live: bool = False) -> bool:
        """mode='monitor' → watch + alert only; mode='checkout' → watch + buy.
        In checkout mode, live=False keeps the dry-run safety (stop before the
        final place-order step)."""
        if self.is_running:
            self.bus.log("already running", "warn")
            return False
        if not cfg.get("product", {}).get("url"):
            self.bus.log("no product URL set — add a target first", "warn")
            return False

        self._stop.clear()
        is_live = bool(live and mode == "checkout")
        dry_run = not is_live
        account_id = (
            (cfg.get("browser") or {}).get("account_id")
            or self.account_id
            or "default"
        )
        bot = backend.CheckoutBot(
            cfg,
            dry_run=dry_run,
            should_stop=self._stop.is_set,
            browser_factory=self.browser_factory,
            account_id=account_id,
        )
        bot.notifier = _HubNotifier(self.hub)   # route alerts through providers
        self.state.update(running=True, mode=mode, live=is_live)

        def worker():
            backend.add_log_sink(self._sink)
            banner = mode.upper() + (" (LIVE)" if is_live else " (dry-run)" if mode == "checkout" else "")
            evasion_on = bool(
                self.browser_factory and getattr(self.browser_factory, "evasion_enabled", False)
            )
            self.bus.log(
                f"engine started — {banner}"
                + (" · evasion ON" if evasion_on else " · evasion off"),
                "info",
            )
            try:
                bot.run(monitor=(mode == "monitor"))
            except SystemExit as e:            # e.g. Playwright not installed
                self.bus.log(str(e), "err")
            except Exception as e:             # noqa: BLE001
                self.bus.log(f"engine error: {e}", "err")
            finally:
                backend.remove_log_sink(self._sink)
                self.state.update(running=False)
                self.bus.log("engine stopped", "info")

        self._thread = threading.Thread(target=worker, daemon=True, name="raindance-engine")
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        self.bus.log("stop requested", "warn")

    # -- persistent login window ------------------------------------------- #
    def open_login(self, url: str, user_data_dir: str, headless: bool = False) -> None:
        """Open a real browser on the persistent profile so the user signs into
        their OWN account. We never see or store the password — the browser keeps
        the session. Call close_login() (or close the window) when done.

        Uses BrowserFactory when available so evasion (stealth/proxy) applies
        the same way it does during checkout.
        """
        if self._login_thread and self._login_thread.is_alive():
            self.bus.log("login window already open", "warn")
            return
        self._login_stop.clear()
        factory = self.browser_factory
        account_id = self.account_id or "default"
        profile = user_data_dir or "profile"

        def worker():
            try:
                from playwright.sync_api import sync_playwright
            except ImportError:
                self.bus.log("Playwright not installed — run: pip install -r requirements.txt", "err")
                return
            Path(profile).mkdir(parents=True, exist_ok=True)
            browser = None
            context = None
            try:
                with sync_playwright() as p:
                    if factory is not None:
                        evasion_on = bool(getattr(factory, "evasion_enabled", False))
                        self.bus.log(
                            f"login window via BrowserFactory "
                            f"(evasion={'ON' if evasion_on else 'off'}, account={account_id})",
                            "info",
                        )
                        browser, context, page = factory.create_context(
                            p,
                            account_id=account_id,
                            headless=headless,
                            user_data_dir=profile,
                            # Label for the per-launch JSONL verdict (launch_log.py).
                            task_id="login_window",
                        )
                        # Same session-first read as tasks/runner.py: on a
                        # factory that publishes per-context session info, ask
                        # THIS context what it launched with rather than the
                        # factory-wide last_stealth, which a concurrent task
                        # launch can overwrite between the launch and this read.
                        # Without session_info() this is the original getattr,
                        # so the older evasion layer behaves identically.
                        _sess = (factory.session_info(context)
                                 if hasattr(factory, "session_info") else None)
                        _applied = ((_sess.stealth if _sess is not None
                                     else getattr(factory, "last_stealth", ""))
                                    or "")
                        if evasion_on and "NOT APPLIED" in _applied:
                            self.bus.log(
                                "this login window is NOT actually patched — "
                                "see the Evasion page", "warn")
                    else:
                        context = p.chromium.launch_persistent_context(
                            profile, headless=headless
                        )
                        page = context.pages[0] if context.pages else context.new_page()

                    if url:
                        page.goto(url, wait_until="domcontentloaded")
                    self.bus.log("login window open — sign in, then click 'Done'", "info")
                    while not self._login_stop.is_set():
                        time.sleep(0.4)
                    if context is not None:
                        context.close()
                    if browser is not None:
                        browser.close()
            except Exception as e:  # noqa: BLE001
                self.bus.log(f"login window error: {e}", "err")
            finally:
                self.bus.log("login window closed — session saved to profile", "info")

        self._login_thread = threading.Thread(target=worker, daemon=True, name="raindance-login")
        self._login_thread.start()

    def close_login(self) -> None:
        self._login_stop.set()
