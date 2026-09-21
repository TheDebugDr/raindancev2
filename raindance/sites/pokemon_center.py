"""Pokémon Center site handler — guest checkout focused.

Selectors are best-effort against public PC markup and will need updates when
the site changes; every one of them (and every wait) is a default in
`DEFAULT_SITE_PARAMS`, overridable from the Catalog page via
retailer_configs.pokemon_center.site_params. Queue + CAPTCHA paths pause for
manual solve in-window. All lookups go through the frame-aware `find_first`.

Extra contract keys this handler honours beyond the standard set:
    selectors.guest     — "checkout as guest" controls (task mode == guest)
    selectors.continue  — multi-step checkout "Continue / Next / Review" buttons
    selectors.variant   — templates with {variant} for task["variant"]
    selectors.sold_out  — used only to log a clearer failure reason
"""
from __future__ import annotations

import re
import time
from typing import Callable, Optional

from raindance.sites.base import as_list
from raindance.sites.generic import GenericHandler

_ATC = [
    "button:has-text('Add to Cart')",
    "button:has-text('Add To Cart')",
    "[data-testid='add-to-cart']",
    "button.add-to-cart",
    "button[aria-label*='Add to Cart']",
]

_CHECKOUT = [
    "a:has-text('Checkout')",
    "button:has-text('Checkout')",
    "a:has-text('View Cart')",
    "a[href*='checkout']",
    "a[href*='cart']",
]

_GUEST = [
    "button:has-text('Guest Checkout')",
    "a:has-text('Guest Checkout')",
    "button:has-text('Checkout as Guest')",
    "a:has-text('Checkout as Guest')",
    "button:has-text('Continue as Guest')",
]

# Unchanged from the previous implementation (queue wait must stay as it is).
_QUEUE_HINTS = [
    "text=You are in line",
    "text=virtual queue",
    "text=waiting room",
    "text=queue",
    "text=We'll be with you soon",
    "#queue-it",
    ".queue-it",
]

_PLACE = [
    "button:has-text('Place Order')",
    "button:has-text('Place order')",
    "button:has-text('Submit Order')",
    "button:has-text('Pay Now')",
    "button[type='submit']:has-text('Order')",
]

_CONTINUE = [
    "role=button[name=/Continue to payment/i]",
    "role=button[name=/Continue/i]",
    "role=button[name=/Next/i]",
    "role=button[name=/Review/i]",
]

_VARIANT = [
    "button:has-text('{variant}')",
    "label:has-text('{variant}')",
    "option:has-text('{variant}')",
    "[data-value='{variant}']",
]


class PokemonCenterHandler(GenericHandler):
    name = "pokemon_center"

    DEFAULT_SITE_PARAMS = {
        "selectors": {
            "atc": list(_ATC),
            "checkout": list(_CHECKOUT),
            "place_order": list(_PLACE),
            "queue": list(_QUEUE_HINTS),
            "confirmation": [],
            "guest": list(_GUEST),
            "continue": list(_CONTINUE),
            "variant": list(_VARIANT),
            "sold_out": ["text=Sold Out"],
        },
        # 45 min max queue wait, 30 s for the ATC button to enable — the
        # values the previous hard-coded implementation used.
        "waits": {"atc_enable_s": 30, "queue_max_s": 45 * 60, "confirm_s": 20},
        "confirmation": {
            "order_id_regex": r"order\s*(?:number|#|id)[:\s]*([A-Z0-9-]{5,})",
            "success_text": ["thank you", "order confirmed", "confirmation"],
        },
    }
    HONORS = GenericHandler.HONORS + (
        "selectors.guest", "selectors.continue", "selectors.variant",
        "selectors.sold_out",
    )

    def navigate(self, page, task: dict) -> None:
        self.bind_task(task)
        url = task.get("url") or ""
        pid = (task.get("product_id") or "").strip()
        if not url and pid:
            # PID-only: best-effort product URL pattern (may need adjustment)
            url = f"https://www.pokemoncenter.com/product/{pid}"
        if not url:
            raise ValueError("Pokémon Center task needs url or product_id")
        self.log(f"[{task.get('id')}] PC navigate → {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        self.human_pause(page, 300, 800)

    def handle_queue(
        self, page, task: dict, *, should_stop=None, on_status=None
    ) -> bool:
        """Phase 4: detect virtual queue and wait patiently.

        Same loop as before (poll every 2.5 s until the queue markers are gone,
        CAPTCHA check each pass, bail on "Access denied"), now shared in
        `SiteHandler.wait_out_queue` with selectors/queue_max_s configurable.
        """
        self.bind_task(task)
        return self.wait_out_queue(page, task, should_stop=should_stop,
                                   on_status=on_status)

    def _queue_visible(self, page, task: Optional[dict] = None) -> bool:
        return self.any_visible(page, self.selectors(task, "queue"))

    def select_variant(self, page, task: dict) -> None:
        variant = (task.get("variant") or "").strip()
        if not variant:
            return
        sels = [s.replace("{variant}", variant) for s in self.selectors(task, "variant")]
        loc, _ = self.find_any(page, sels, visible=True)
        if loc is not None:
            self.human_click(page, loc)
            self.human_pause(page, 100, 300)

    def add_to_cart(
        self, page, task: dict, *, should_stop=None, on_status=None
    ) -> bool:
        self.bind_task(task)
        tid = task.get("id", "")
        if on_status:
            on_status("atc")
        # Same class of bug fixed in generic.py's add_to_cart(): a challenge
        # here must stop the run, not be silently proceeded past.
        if not self._captcha(page, task, should_stop, on_status):
            self.log(f"[{tid}] captcha blocked add-to-cart", "err")
            return False
        self.select_variant(page, task)

        # Wait for ATC to be enabled (waits.atc_enable_s, default 30 s).
        loc, sel = self.wait_for_first(
            page, self.atc_selectors(task),
            timeout_s=self.wait_s(task, "atc_enable_s", 30),
            should_stop=should_stop, poll_ms=500,
        )
        if loc is None:
            if should_stop and should_stop():
                return False
            if self.any_visible(page, self.selectors(task, "sold_out")):
                self.log(f"[{tid}] Sold Out on product page", "warn")
            self.log(f"[{tid}] ATC button not clickable", "err")
            return False
        self.human_click(page, loc)
        self.log(f"[{tid}] PC ATC clicked via {sel}", "ok")
        self.human_pause(page, 500, 1200)
        if not self._captcha(page, task, should_stop, on_status):
            self.log(f"[{tid}] captcha blocked after add-to-cart", "err")
            return False
        return True

    def default_checkout_fields(self, profile: dict, task: dict | None = None) -> list:
        ship = profile.get("shipping") or {}
        pay = profile.get("payment") or {}
        email = profile.get("email") or ""
        phone = profile.get("phone") or ""
        return [
            ("input[type='email'], input[name*='email' i]", email),
            ("input[type='tel'], input[name*='phone' i]", phone),
            ("input[name*='firstName' i], input[id*='firstName' i], input[name*='first' i]",
             ship.get("first_name")),
            ("input[name*='lastName' i], input[id*='lastName' i], input[name*='last' i]",
             ship.get("last_name")),
            ("input[name*='address1' i], input[id*='address1' i], input[autocomplete='address-line1']",
             ship.get("address1")),
            ("input[name*='address2' i], input[autocomplete='address-line2']",
             ship.get("address2")),
            ("input[name*='city' i], input[autocomplete='address-level2']",
             ship.get("city")),
            ("input[name*='postal' i], input[name*='zip' i], input[autocomplete='postal-code']",
             ship.get("zip")),
            ("select[name*='state' i], select[autocomplete='address-level1']",
             ship.get("state")),
            ("input[autocomplete='cc-number'], input[name*='cardNumber' i]",
             pay.get("card_number")),
            ("input[autocomplete='cc-exp'], input[name*='expir' i]",
             pay.get("expiry")),
            ("input[autocomplete='cc-csc'], input[name*='cvv' i], input[name*='cvc' i]",
             pay.get("cvc")),
            ("input[autocomplete='cc-name'], input[name*='cardholder' i]",
             pay.get("card_name")),
        ]

    def _dry_run_screenshot(self, page, tid: str) -> str:
        try:
            from pathlib import Path
            from datetime import datetime
            shots = Path("data/screenshots")
            shots.mkdir(parents=True, exist_ok=True)
            path = str(shots / f"{datetime.now():%Y%m%d-%H%M%S}-{tid}-dryrun.png")
            page.screenshot(path=path, full_page=True)
            return path
        except Exception:
            return ""

    def checkout(
        self, page, task, profile, *, dry_run=True, should_stop=None, on_status=None
    ) -> dict:
        self.bind_task(task)
        tid = task.get("id", "")
        if on_status:
            on_status("checkout")

        # Cart / checkout entry
        if self.enter_checkout(page, task):
            self.human_pause(page, 400, 900)

        # Guest path
        if (task.get("mode") or "guest") == "guest":
            loc, _ = self.find_any(page, self.selectors(task, "guest"), visible=True)
            if loc is not None:
                self.human_click(page, loc)
                self.log(f"[{tid}] guest checkout selected", "ok")
                self.human_pause(page, 300, 700)

        if not self._captcha(page, task, should_stop, on_status):
            self.log(f"[{tid}] captcha blocked checkout entry", "err")
            return {"ok": False, "order_id": "",
                    "error": "captcha blocked checkout entry"}

        self.fill_checkout_fields(page, profile, task)

        # Continue buttons through multi-step checkout: each selector is tried
        # once, in order, clicking whichever is visible+enabled (as before).
        for sel in self.selectors(task, "continue"):
            try:
                loc = self.find_first(page, sel)
                if loc.count() and loc.is_visible() and loc.is_enabled():
                    self.human_click(page, loc)
                    self.human_pause(page, 400, 900)
                    if not self._captcha(page, task, should_stop, on_status):
                        self.log(f"[{tid}] captcha blocked mid-checkout", "err")
                        return {"ok": False, "order_id": "",
                                "error": "captcha blocked mid-checkout"}
            except Exception:
                continue

        if dry_run:
            self.log(f"[{tid}] DRY-RUN: not placing Pokémon Center order", "warn")
            return {"ok": True, "order_id": "", "error": "", "dry_run": True,
                    "screenshot": self._dry_run_screenshot(page, tid)}

        if should_stop and should_stop():
            return {"ok": False, "order_id": "", "error": "stopped"}
        if on_status:
            on_status("submitting")
        return self.place_order(page, task)

    def detect_stock_signals(self, html: str) -> Optional[bool]:
        low = (html or "").lower()
        if "sold out" in low or "out of stock" in low or "currently unavailable" in low:
            return False
        if "add to cart" in low or "add to bag" in low:
            return True
        return None

    def detect_seller(self, html: str, url: str) -> dict:
        """Pokémon Center sells only its own stock, so identity is the domain.

        There is no marketplace here — which is why this store needs no seller
        parsing at all, only proof that the host really is pokemoncenter.com.
        Lookalike domains are explicitly not official.
        """
        from raindance.core import seller as _seller
        official = _seller.official_host(url, "pokemoncenter.com")
        if official is True:
            return {"is_official": True, "seller": "Pokémon Center"}
        if official is False:
            from urllib.parse import urlparse
            return {"is_official": False,
                    "seller": (urlparse(url or "").hostname or "").lower()}
        return {"is_official": None, "seller": None}
