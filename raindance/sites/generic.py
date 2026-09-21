"""Generic site handler — selector-heuristic ATC + form fill from profile.

Every selector, wait and confirmation rule comes from the override layer
(`SiteHandler.effective`, see base.py); the module constants below are only
the *generic defaults* that layer starts from. A retailer subclass states its
own `DEFAULT_SITE_PARAMS`; a store overrides any key from the Catalog page.
"""
from __future__ import annotations

import re
from typing import Callable, Optional

from raindance.sites.base import SiteHandler, as_list, profile_value

_STOCK_OUT = re.compile(r"out of stock|sold out|currently unavailable|notify me", re.I)
_STOCK_IN = re.compile(r"add to cart|add to bag|in stock|buy now", re.I)

# Embedded <script>/<style> blocks carry SPA state, config, and error-dialog
# copy dictionaries that ship on every PDP regardless of the product's real
# state — confirmed on a live Best Buy page, where a hydration script's
# `"item_not_sellable":{"title":"sold out"}}` (an add-to-cart error dialog's
# boilerplate, unrelated to this product) made detect_stock_signals() return
# False on EVERY Best Buy listing, since GenericHandler checks the OUT
# pattern with no positional or provenance filtering and Best Buy inherits
# it unmodified. A second false positive on the same page ("notify me",
# from an unrelated rewards-program message template) sat inside the same
# script block under different JS-string escaping — which is why this strips
# the blocks outright rather than pattern-matching around each escaping
# style: genuine rendered DOM text (what page.content() returns after JS
# runs) lives in ordinary tags, never inside <script>/<style>.
_SCRIPT_STYLE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.I | re.S)


def _visible_text(html: str) -> str:
    return _SCRIPT_STYLE.sub(" ", html or "")

_ATC_SELECTORS = [
    "button:has-text('Add to Cart')",
    "button:has-text('Add to Bag')",
    "button:has-text('Add To Cart')",
    "[data-test*='addToCart']",
    "[data-testid*='add-to-cart']",
    "button#add-to-cart",
    "input[value*='Add to Cart']",
]

_CHECKOUT_SELECTORS = [
    "a:has-text('Checkout')",
    "button:has-text('Checkout')",
    "a:has-text('Proceed to Checkout')",
    "button:has-text('Proceed to Checkout')",
    "[data-test*='checkout']",
]

_PLACE_ORDER = [
    "button:has-text('Place Order')",
    "button:has-text('Place order')",
    "button:has-text('Submit Order')",
    "button:has-text('Pay now')",
    "button:has-text('Complete order')",
    "button#place-order",
]

_QUANTITY = [
    "input[name*='quantity']",
    "select[name*='quantity']",
    "input[id*='qty']",
    "input[name='qty']",
]

# Exported so a retailer handler can *extend* rather than replace the generic
# lists (e.g. TargetHandler prepends its data-test hooks).
GENERIC_ATC = tuple(_ATC_SELECTORS)
GENERIC_CHECKOUT = tuple(_CHECKOUT_SELECTORS)
GENERIC_PLACE_ORDER = tuple(_PLACE_ORDER)


class GenericHandler(SiteHandler):
    name = "generic"

    DEFAULT_SITE_PARAMS = {
        "selectors": {
            "atc": list(_ATC_SELECTORS),
            "checkout": list(_CHECKOUT_SELECTORS),
            "place_order": list(_PLACE_ORDER),
            # Generic sites rarely have a virtual queue: no default queue
            # selectors means handle_queue returns at once, as before.
            "queue": [],
            "confirmation": [],
            "quantity": list(_QUANTITY),
        },
    }
    HONORS = SiteHandler.HONORS + ("selectors.quantity",)

    # -- selector seams ---------------------------------------------------- #
    # Kept as methods so a subclass may still compute a list, but the default
    # implementation is the override layer: per-store > handler > generic.
    def atc_selectors(self, task: dict | None = None) -> list:
        return self.selectors(task, "atc")

    def checkout_selectors(self, task: dict | None = None) -> list:
        return self.selectors(task, "checkout")

    def place_order_selectors(self, task: dict | None = None) -> list:
        return self.selectors(task, "place_order")

    def _captcha(self, page, task, should_stop, on_status) -> bool:
        if not self.captcha:
            return True
        return bool(self.captcha.handle_if_present(
            page, task_id=task.get("id", ""), should_stop=should_stop,
            on_waiting=lambda: on_status and on_status("captcha"),
        ))

    def navigate(self, page, task: dict) -> None:
        self.bind_task(task)
        url = task.get("url") or ""
        if not url:
            raise ValueError("task has no url")
        self.log(f"[{task.get('id')}] goto {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        self.human_pause(page, 200, 600)

    def handle_queue(
        self, page, task: dict, *, should_stop=None, on_status=None
    ) -> bool:
        self.bind_task(task)
        if self.captcha:
            if on_status:
                on_status("captcha")
            if not self.captcha.handle_if_present(
                page, task_id=task.get("id", ""), should_stop=should_stop
            ):
                return False
        # No-op unless selectors.queue is configured for this handler/store.
        return self.wait_out_queue(page, task, should_stop=should_stop,
                                   on_status=on_status)

    def set_quantity(self, page, task: dict) -> None:
        qty = int(task.get("quantity") or 1)
        if qty <= 1:
            return
        for sel in self.selectors(task, "quantity"):
            try:
                loc = self.find_first(page, sel)
                if loc.count():
                    tag = loc.evaluate("e => e.tagName").lower()
                    if tag == "select":
                        self.human_select(page, loc, by_value=str(qty))
                    else:
                        self.human_type(page, loc, str(qty))
                    break
            except Exception:
                continue

    def add_to_cart(
        self, page, task: dict, *, should_stop=None, on_status=None
    ) -> bool:
        self.bind_task(task)
        if on_status:
            on_status("atc")
        # Both calls' return values matter, not just handle_queue()'s — a
        # challenge that times out here must stop the run, not be treated as
        # cleared. Confirmed live: a real, fully visible PerimeterX "Press &
        # hold" overlay was present from page load through well past the ATC
        # click, yet the task reported "reached the final step" within ~30s —
        # far short of even one captcha timeout — because BOTH calls below
        # discarded what _captcha() returned and proceeded regardless.
        if not self._captcha(page, task, should_stop, on_status):
            self.log(f"[{task.get('id')}] captcha blocked add-to-cart", "err")
            return False
        self.set_quantity(page, task)

        # waits.atc_enable_s: how long to wait for the button to become
        # clickable (a PDP that hydrates late). Generic default: 30s.
        loc, sel = self.wait_for_first(
            page, self.atc_selectors(task),
            timeout_s=self.wait_s(task, "atc_enable_s", 30),
            should_stop=should_stop,
        )
        if loc is None:
            self.log(f"[{task.get('id')}] could not find ATC button", "err")
            return False
        self.human_click(page, loc)
        self.log(f"[{task.get('id')}] ATC via {sel}", "ok")
        self.human_pause(page, 400, 900)
        if not self._captcha(page, task, should_stop, on_status):
            self.log(f"[{task.get('id')}] captcha blocked after add-to-cart", "err")
            return False
        return True

    # -- checkout form ----------------------------------------------------- #
    def default_checkout_fields(self, profile: dict, task: dict | None = None) -> list:
        """[(selector, value)] a handler assumes for its checkout markup.

        Override this in a retailer handler. `checkout_fields` adds the store's
        `fields` on top (or replaces with them when `fields_only`).
        """
        ship = profile.get("shipping") or {}
        pay = profile.get("payment") or {}
        return [
            ("input[type='email'], input[name*='email' i], #email",
             profile.get("email") or ""),
            ("input[name*='phone' i], input[type='tel'], #phone",
             profile.get("phone") or ""),
            ("input[name*='first' i], #firstName, #first-name", ship.get("first_name")),
            ("input[name*='last' i], #lastName, #last-name", ship.get("last_name")),
            ("input[name*='address1' i], input[name*='address_1' i], #address1",
             ship.get("address1")),
            ("input[name*='address2' i], #address2", ship.get("address2")),
            ("input[name*='city' i], #city", ship.get("city")),
            ("input[name*='zip' i], input[name*='postal' i], #zip, #postalCode",
             ship.get("zip")),
            ("select[name*='state' i], #state, select[name*='province' i]",
             ship.get("state")),
            ("input[name*='card' i][name*='number' i], #card-number, "
             "input[autocomplete='cc-number']", pay.get("card_number")),
            ("input[name*='cvc' i], input[name*='cvv' i], #card-cvc, "
             "input[autocomplete='cc-csc']", pay.get("cvc")),
            ("input[name*='expir' i], #card-expiry, input[autocomplete='cc-exp']",
             pay.get("expiry")),
            ("input[name*='card' i][name*='name' i], #card-name, "
             "input[autocomplete='cc-name']", pay.get("card_name")),
        ]

    def custom_checkout_fields(self, profile: dict, task: dict | None = None) -> list:
        """`fields` from the override layer as [(selector, value)].

        Contract shape is {"<css>": "<profile.path>"}. The older per-store
        shape {"<profile.path>": "<css>" | [css, ...]} is still accepted: a key
        that resolves to a profile path is treated as one.
        """
        custom = self.effective(task, "fields", {}) or {}
        if not isinstance(custom, dict):
            return []
        out = []
        for key, val in custom.items():
            key_s = str(key).strip()
            if not key_s:
                continue
            looks_like_path = bool(re.fullmatch(r"[A-Za-z_][\w]*(\.[A-Za-z_][\w]*)*", key_s))
            if looks_like_path and isinstance(profile, dict) and \
                    key_s.split(".")[0] in profile:
                value = profile_value(profile, key_s)          # legacy path→css
                sels = as_list(val)
            else:
                sels = [key_s]                                  # css→path
                value = profile_value(profile, str(val)) if isinstance(val, str) else None
            if value in (None, ""):
                continue
            for one in sels:
                out.append((one, value))
        return out

    def checkout_fields(self, profile: dict, task: dict | None = None) -> list:
        """Final [(selector, value)] list: store fields first so they win when
        both would match; `fields_only` drops the handler defaults."""
        extra = self.custom_checkout_fields(profile, task)
        if self.effective(task, "fields_only", False):
            return extra
        return extra + self.default_checkout_fields(profile, task)

    def fill_checkout_fields(self, page, profile: dict, task: dict) -> int:
        """Type every non-empty field through the frame-aware lookup."""
        filled = 0
        for sel, val in self.checkout_fields(profile, task):
            if not val:
                continue
            try:
                loc = self.find_first(page, sel)
                if not loc.count():
                    continue
                tag = ""
                try:
                    tag = loc.evaluate("e => e.tagName").lower()
                except Exception:
                    pass
                if tag == "select":
                    self.human_select(page, loc, by_label=str(val))
                else:
                    self.human_type(page, loc, str(val))
                filled += 1
                self.human_pause(page, 40, 120)
            except Exception:
                continue
        return filled

    def enter_checkout(self, page, task: dict) -> bool:
        """Cart → checkout entry. False when no checkout control was seen
        (not fatal: the page may already be the checkout form)."""
        loc, sel = self.find_any(page, self.checkout_selectors(task), visible=True)
        if loc is None:
            return False
        self.human_click(page, loc)
        self.log(f"[{task.get('id')}] checkout via {sel}", "info")
        return True

    def place_order(self, page, task: dict) -> dict:
        """Click place-order and read the confirmation. A missing button is a
        hard failure — never a fabricated order id."""
        loc, sel = self.find_any(page, self.place_order_selectors(task),
                                 visible=True, enabled=True)
        if loc is None:
            self.log(f"[{task.get('id')}] place-order button not found", "err")
            return {"ok": False, "order_id": "",
                    "error": "place-order button not found (check selectors.place_order)"}
        self.human_click(page, loc)
        self.log(f"[{task.get('id')}] place order via {sel}", "ok")
        self.human_pause(page, 800, 1500)
        return self.confirm_order(page, task)

    def checkout(
        self, page, task, profile, *, dry_run=True, should_stop=None, on_status=None
    ) -> dict:
        self.bind_task(task)
        if on_status:
            on_status("checkout")
        self.enter_checkout(page, task)
        self.human_pause(page, 300, 700)
        # Same fix as add_to_cart(): a captcha here must stop checkout, not
        # be silently proceeded past. dry_run's whole safety story is "we
        # reached and filled a real form" — that claim is false if a
        # challenge sat on the checkout entry the entire time.
        if not self._captcha(page, task, should_stop, on_status):
            self.log(f"[{task.get('id')}] captcha blocked checkout entry", "err")
            return {"ok": False, "order_id": "",
                    "error": "captcha blocked checkout entry"}

        self.fill_checkout_fields(page, profile, task)

        if dry_run:
            self.log(f"[{task.get('id')}] DRY-RUN: stopping before place order", "warn")
            return {"ok": True, "order_id": "", "error": "", "dry_run": True}

        if should_stop and should_stop():
            return {"ok": False, "order_id": "", "error": "stopped"}
        if on_status:
            on_status("submitting")
        return self.place_order(page, task)

    def detect_stock_signals(self, html: str) -> Optional[bool]:
        """Text-heuristic stock read shared by every generic-derived handler.
        Runs only after the classifier has ruled out a bot-wall body."""
        low = _visible_text(html or "")
        if _STOCK_OUT.search(low):
            return False
        if _STOCK_IN.search(low):
            return True
        return None
