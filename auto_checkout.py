#!/usr/bin/env python3
"""
auto_checkout — a simple, config-driven e-commerce auto-checkout bot.

It watches a product page until the item is in stock, then walks a
configurable sequence of steps (add to cart, fill the checkout form, place
the order) using a real browser via Playwright.

Safety first:
  * Runs in DRY-RUN by default. Any step marked "final": true (i.e. the
    button that actually charges your card) is SKIPPED unless you pass --live.
  * Secrets (email, card number, ...) live in the environment / a .env file,
    never in the committed config.

Usage:
    python auto_checkout.py config.json                 # watch + dry-run
    python auto_checkout.py config.json --check-only     # one stock check
    python auto_checkout.py config.json --checkout-now   # test the form flow
    python auto_checkout.py config.json --live           # actually buy it

Only use this against sites you're allowed to automate, with your own
account and payment details, and keep the poll interval polite.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from notifier import Notifier

if TYPE_CHECKING:  # for type hints only; not needed to run the CLI
    from playwright.sync_api import Page


def _import_playwright():
    """Import Playwright lazily so --help / config checks work without it."""
    try:
        from playwright.sync_api import TimeoutError as PWTimeout, sync_playwright
    except ImportError:
        sys.exit(
            "Playwright is not installed.\n"
            "  pip install -r requirements.txt\n"
            "  python -m playwright install chromium"
        )
    return sync_playwright, PWTimeout


# --------------------------------------------------------------------------- #
# Config loading + ${ENV_VAR} interpolation
# --------------------------------------------------------------------------- #
# ${VAR} → required (errors if unset); ${VAR:-default} → optional (uses default if unset).
_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")


def load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader (no dependency on python-dotenv)."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _interpolate(value: Any) -> Any:
    """Recursively replace ${VAR} with environment values in strings."""
    if isinstance(value, str):
        def repl(m: "re.Match[str]") -> str:
            var, default = m.group(1), m.group(2)
            if var in os.environ:
                return os.environ[var]
            if default is not None:  # ${VAR:-default} → optional
                return default
            raise KeyError(
                f"Config references ${{{var}}} but it is not set. "
                f"Add it to your .env or export it."
            )

        return _ENV_PATTERN.sub(repl, value)
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    return value


def load_config(path: str) -> dict:
    raw = Path(path).read_text()
    data = json.loads(raw)
    return _interpolate(data)


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
# Extra sinks (e.g. a GUI log panel) can subscribe without changing behaviour;
# the console print always happens. Each sink is called with (msg, level).
_LOG_SINKS: list = []


def add_log_sink(fn) -> None:
    if fn not in _LOG_SINKS:
        _LOG_SINKS.append(fn)


def remove_log_sink(fn) -> None:
    if fn in _LOG_SINKS:
        _LOG_SINKS.remove(fn)


def log(msg: str, level: str = "info") -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    icon = {"info": "·", "ok": "✓", "warn": "!", "err": "✗", "hit": "★"}.get(level, "·")
    print(f"[{stamp}] {icon} {msg}", flush=True)
    for sink in list(_LOG_SINKS):
        try:
            sink(msg, level)
        except Exception:
            pass  # a broken sink must never break the bot


def parse_start_at(value: str) -> "datetime | None":
    """Parse a scheduled start time. Accepts ISO ('2026-07-12T09:59:30' or with
    a space) or a plain 'HH:MM[:SS]' meaning today. Empty → None (start now)."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        if "-" in value or "T" in value:
            return datetime.fromisoformat(value.replace("T", " "))
        parts = [int(x) for x in value.split(":")]
        parts += [0] * (3 - len(parts))
        h, m, s = parts[:3]
        return datetime.now().replace(hour=h, minute=m, second=s, microsecond=0)
    except (ValueError, IndexError):
        raise ValueError(f"bad schedule time {value!r} (use ISO or HH:MM[:SS])")


# --------------------------------------------------------------------------- #
# The bot
# --------------------------------------------------------------------------- #
class CheckoutBot:
    def __init__(self, config: dict, *, dry_run: bool = True, should_stop=None,
                 browser_factory=None, account_id: str = "default"):
        self.cfg = config
        self.dry_run = dry_run
        # Optional callable returning True to request a cooperative stop (used by
        # the GUI to halt a background run). Default None → runs as before.
        self.should_stop = should_stop
        # Optional BrowserFactory (evasion layer). When set, browser launch goes
        # through it so stealth/proxy settings apply. CLI leaves this None.
        self.browser_factory = browser_factory
        self.account_id = account_id or "default"
        self.shots_dir = Path(config.get("screenshots_dir", "screenshots"))
        self.shots_dir.mkdir(parents=True, exist_ok=True)
        self.notifier = Notifier(config.get("notifications"))
        self._PWTimeout: Any = None  # set in run() after Playwright is imported

    def _stop_requested(self) -> bool:
        return bool(self.should_stop and self.should_stop())

    def _interruptible_sleep(self, seconds: float) -> None:
        """Sleep in small slices so a stop request is honoured quickly."""
        end = seconds
        step = 0.25
        slept = 0.0
        while slept < end:
            if self._stop_requested():
                return
            time.sleep(min(step, end - slept))
            slept += step

    # -- helpers ----------------------------------------------------------- #
    def _screenshot(self, page: Page, name: str) -> None:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = self.shots_dir / f"{ts}-{name}.png"
        try:
            page.screenshot(path=str(path), full_page=True)
            log(f"screenshot → {path}", "info")
        except Exception as e:  # screenshots are best-effort
            log(f"could not screenshot ({e})", "warn")

    def _run_step(self, page: Page, step: dict, index: int) -> None:
        action = step.get("action", "").lower()
        desc = step.get("description") or f"{action} {step.get('selector', step.get('url', ''))}"
        timeout = int(step.get("timeout_ms", 15000))

        if action == "goto":
            page.goto(step["url"], timeout=timeout, wait_until="domcontentloaded")
        elif action == "click":
            page.click(step["selector"], timeout=timeout)
        elif action == "fill":
            page.fill(step["selector"], str(step.get("value", "")), timeout=timeout)
        elif action == "type":
            page.type(step["selector"], str(step.get("value", "")),
                      delay=int(step.get("delay_ms", 40)), timeout=timeout)
        elif action == "select":
            page.select_option(step["selector"], str(step.get("value", "")), timeout=timeout)
        elif action == "check":
            page.check(step["selector"], timeout=timeout)
        elif action == "press":
            page.press(step["selector"], step.get("key", "Enter"), timeout=timeout)
        elif action == "wait_for":
            page.wait_for_selector(step["selector"],
                                   state=step.get("state", "visible"), timeout=timeout)
        elif action == "wait_ms":
            page.wait_for_timeout(int(step.get("value", 1000)))
        elif action == "screenshot":
            self._screenshot(page, step.get("name", f"step{index}"))
            return
        else:
            raise ValueError(f"Unknown step action: {action!r}")

        log(f"step {index}: {desc}", "ok")

    def run_steps(self, page: Page, steps: list[dict], label: str) -> None:
        for i, step in enumerate(steps, 1):
            if step.get("final") and self.dry_run:
                log(f"DRY-RUN: stopping before final step ({step.get('description', 'place order')}). "
                    f"Re-run with --live to complete the purchase.", "warn")
                self._screenshot(page, "dryrun-before-final")
                return
            self._run_step(page, step, i)
        log(f"{label} complete", "hit" if not self.dry_run else "ok")

    # -- stock check ------------------------------------------------------- #
    def is_in_stock(self, page: Page) -> bool:
        cond = self.cfg["product"]["in_stock"]
        timeout = int(cond.get("timeout_ms", 8000))

        if "text_absent" in cond:
            try:
                page.wait_for_selector(f"text={cond['text_absent']}", timeout=1500)
                return False  # out-of-stock marker present
            except self._PWTimeout:
                pass  # marker absent → good sign, keep checking

        if "text_present" in cond:
            try:
                page.wait_for_selector(f"text={cond['text_present']}", timeout=timeout)
            except self._PWTimeout:
                return False

        selector = cond.get("selector")
        if selector:
            try:
                el = page.wait_for_selector(selector, timeout=timeout, state="attached")
            except self._PWTimeout:
                return False
            if cond.get("expect_enabled", True) and not el.is_enabled():
                return False
            if cond.get("expect_visible", True) and not el.is_visible():
                return False

        return True

    # -- scheduled start --------------------------------------------------- #
    def wait_for_scheduled_start(self) -> None:
        """Idle until schedule.start_at (be armed a moment before a known drop)."""
        target = parse_start_at(self.cfg.get("schedule", {}).get("start_at", ""))
        if not target:
            return
        delay = (target - datetime.now()).total_seconds()
        if delay <= 0:
            log(f"scheduled time {target:%Y-%m-%d %H:%M:%S} already passed — starting now", "warn")
            return
        log(f"scheduled: waiting until {target:%Y-%m-%d %H:%M:%S} ({delay:.0f}s)…")
        self._interruptible_sleep(delay)  # Ctrl-C or a stop request ends it

    # -- optional login ---------------------------------------------------- #
    def login(self, page: Page) -> None:
        login = self.cfg.get("login")
        if not login or not login.get("enabled"):
            return
        log("logging in…")
        if login.get("url"):
            page.goto(login["url"], wait_until="domcontentloaded")
        self.run_steps(page, login.get("steps", []), "login")

    # -- main loop --------------------------------------------------------- #
    def watch_and_buy(self, page: Page, *, check_only: bool = False,
                      checkout_now: bool = False, monitor: bool = False) -> bool:
        product_url = self.cfg["product"]["url"]
        product_name = self.cfg["product"].get("name", product_url)
        poll = self.cfg.get("poll", {})
        interval = float(poll.get("interval_seconds", 30))
        jitter = float(poll.get("jitter_seconds", 10))
        max_attempts = int(poll.get("max_attempts", 0))  # 0 = unlimited

        if checkout_now:
            log("checkout-now: skipping stock check, running checkout steps")
            self.run_steps(page, self.cfg["checkout_steps"], "checkout")
            self.notifier.notify("checkout_success", "Checkout finished",
                                 product_name, product_url)
            return True

        if not check_only:
            self.wait_for_scheduled_start()

        attempt = 0
        was_in_stock = False  # for monitor mode: only alert on the flip to in-stock
        while True:
            if self._stop_requested():
                log("stop requested — halting watch loop", "warn")
                return was_in_stock
            attempt += 1
            page.goto(product_url, wait_until="domcontentloaded")
            in_stock = self.is_in_stock(page)

            if in_stock:
                if monitor:
                    # Alert once per restock event, then keep watching.
                    if not was_in_stock:
                        log(f"IN STOCK (attempt {attempt})", "hit")
                        self._screenshot(page, "in-stock")
                        self.notifier.notify(
                            "in_stock", "🟢 IN STOCK",
                            f"{product_name} — go buy it now", product_url)
                    was_in_stock = True
                else:
                    log(f"IN STOCK (attempt {attempt}) → starting checkout", "hit")
                    self._screenshot(page, "in-stock")
                    self.notifier.notify(
                        "in_stock", "🟢 IN STOCK",
                        f"{product_name} — starting checkout", product_url)
                    if check_only:
                        return True
                    self.run_steps(page, self.cfg["checkout_steps"], "checkout")
                    self.notifier.notify(
                        "checkout_success",
                        "✅ Checkout finished" if not self.dry_run else "🧪 Dry-run reached checkout",
                        product_name, product_url)
                    return True
            else:
                was_in_stock = False
                log(f"not available (attempt {attempt})", "info")

            if check_only:
                return in_stock
            if max_attempts and attempt >= max_attempts:
                log(f"reached max_attempts ({max_attempts}), giving up", "warn")
                return was_in_stock

            wait = interval + random.uniform(0, jitter)
            log(f"next check in {wait:.0f}s")
            self._interruptible_sleep(wait)

    # -- browser launch ---------------------------------------------------- #
    def _launch_browser(self, playwright):
        """Open browser/context/page, optionally via BrowserFactory (evasion).

        Returns (browser, context, page). browser is None for persistent
        profiles (context owns the process).
        """
        browser_cfg = self.cfg.get("browser", {})
        user_data_dir = browser_cfg.get("user_data_dir") or None
        if user_data_dir == "":
            user_data_dir = None
        headless = browser_cfg.get("headless", False)
        slow_mo = int(browser_cfg.get("slow_mo_ms", 0))
        viewport = browser_cfg.get("viewport")
        user_agent = browser_cfg.get("user_agent")
        account_id = (
            browser_cfg.get("account_id")
            or self.account_id
            or "default"
        )

        if self.browser_factory is not None:
            if getattr(self.browser_factory, "evasion_enabled", False):
                log(f"evasion ON — launching via BrowserFactory (account={account_id})")
            else:
                log("evasion OFF — launching via BrowserFactory (plain browser)")
            if user_data_dir:
                log(f"using persistent profile: {user_data_dir}")
            return self.browser_factory.create_context(
                playwright,
                account_id=account_id,
                headless=headless,
                user_data_dir=user_data_dir,
                slow_mo=slow_mo,
                viewport=viewport,
                user_agent=user_agent,
            )

        # Legacy / CLI path — no factory wired in.
        if user_data_dir:
            Path(user_data_dir).mkdir(parents=True, exist_ok=True)
            log(f"using persistent profile: {user_data_dir}")
            context = playwright.chromium.launch_persistent_context(
                user_data_dir, headless=headless, slow_mo=slow_mo,
                user_agent=user_agent,
                viewport=viewport,
            )
            browser = None
            page = context.pages[0] if context.pages else context.new_page()
        else:
            browser = playwright.chromium.launch(headless=headless, slow_mo=slow_mo)
            context = browser.new_context(
                user_agent=user_agent,
                viewport=viewport,
            )
            page = context.new_page()
        return browser, context, page

    # -- entrypoint -------------------------------------------------------- #
    def run(self, *, check_only: bool = False, checkout_now: bool = False,
            monitor: bool = False) -> bool:
        browser_cfg = self.cfg.get("browser", {})
        sync_playwright, self._PWTimeout = _import_playwright()
        with sync_playwright() as p:
            browser, context, page = self._launch_browser(p)
            try:
                self.login(page)
                return self.watch_and_buy(
                    page, check_only=check_only, checkout_now=checkout_now,
                    monitor=monitor,
                )
            except Exception as e:
                log(f"error: {e}", "err")
                self._screenshot(page, "error")
                self.notifier.notify("error", "⚠️ Bot error", str(e))
                raise
            finally:
                if browser_cfg.get("keep_open"):
                    log("keep_open set — leaving browser up; Ctrl-C to quit")
                    try:
                        page.wait_for_timeout(10 * 60 * 1000)
                    except KeyboardInterrupt:
                        pass
                context.close()
                if browser is not None:
                    browser.close()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Config-driven e-commerce auto-checkout bot (dry-run by default).",
    )
    parser.add_argument("config", nargs="?", default="config.json",
                        help="Path to config JSON (default: config.json)")
    parser.add_argument("--monitor", action="store_true",
                        help="Watch and alert on restocks only — never checkout (buy manually)")
    parser.add_argument("--check-only", action="store_true",
                        help="Check stock once and exit without buying")
    parser.add_argument("--checkout-now", action="store_true",
                        help="Skip stock polling and run the checkout steps immediately")
    parser.add_argument("--live", action="store_true",
                        help="Actually place the order (disables dry-run safety)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Force dry-run even if the config disables it")
    parser.add_argument("--headful", action="store_true", help="Force a visible browser")
    parser.add_argument("--headless", action="store_true", help="Force a headless browser")
    parser.add_argument("--start-at", default=None,
                        help="Wait until this time before watching (ISO or HH:MM[:SS])")
    parser.add_argument("--env", default=".env", help="Path to env file (default: .env)")
    args = parser.parse_args()

    load_dotenv(args.env)

    try:
        config = load_config(args.config)
    except FileNotFoundError:
        log(f"config not found: {args.config} (copy config.example.json)", "err")
        return 1
    except (json.JSONDecodeError, KeyError) as e:
        log(f"config problem: {e}", "err")
        return 1

    # Resolve dry-run: --live wins to go live, but --dry-run always overrides back to safe.
    dry_run = not args.live
    if args.dry_run:
        dry_run = True

    if args.headful:
        config.setdefault("browser", {})["headless"] = False
    if args.headless:
        config.setdefault("browser", {})["headless"] = True
    if args.start_at is not None:
        config.setdefault("schedule", {})["start_at"] = args.start_at

    if args.monitor:
        mode = "MONITOR (alert only, no checkout)"
    elif dry_run:
        mode = "DRY-RUN"
    else:
        mode = "LIVE — WILL PLACE ORDER"
    log(f"mode: {mode}", "warn" if not dry_run and not args.monitor else "info")

    bot = CheckoutBot(config, dry_run=dry_run)
    try:
        ok = bot.run(check_only=args.check_only, checkout_now=args.checkout_now,
                     monitor=args.monitor)
    except KeyboardInterrupt:
        log("interrupted", "warn")
        return 130
    except Exception:
        return 1
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
