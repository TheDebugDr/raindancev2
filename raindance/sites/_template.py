"""TEMPLATE — copy to raindance/sites/<retailer>.py, rename the class, and add
it to `_REGISTRY` in raindance/sites/__init__.py. Nothing in this file is
registered; importing it has no side effects.

Three levels of effort, pick the smallest that works:

  1. MINIMAL  — selectors only. Subclass GenericHandler, fill DEFAULT_SITE_PARAMS.
                Queue, ATC, checkout, form fill, dry-run stop, confirmation all
                come from the generic flow, driven by your defaults, and every
                one of them stays overridable per store from the Catalog page.
  2. MEDIUM   — plus a custom handle_queue (or add_to_cart) when the retailer's
                flow differs. Keep using the shared helpers so per-store
                overrides still apply.
  3. FULL     — every phase custom. Keep the runner contract exactly:
                navigate(page, task) -> None
                handle_queue(page, task, *, should_stop=, on_status=) -> bool
                add_to_cart(page, task, *, should_stop=, on_status=) -> bool
                checkout(page, task, profile, *, dry_run=, should_stop=,
                         on_status=) -> {ok, order_id, error, dry_run?}
                detect_seller(html, url) -> {is_official, seller}
                detect_stock_signals(html) -> bool | None

Rules that apply to every level:
  * Never call page.locator / page.query_selector directly — use
    self.find_first / find_any / wait_for_first so iframes and per-store
    `frames` hints work. Never read a module constant for a selector or wait;
    read self.selectors(task, key) / self.wait_s(task, key) / self.effective().
  * Call self.bind_task(task) at the top of any phase you override.
  * A missing place-order button is {ok: False, error: ...} — never invent an
    order id. dry_run=True must return before place-order is clicked.
  * Nothing here may exist to defeat bot detection, waiting rooms or
    challenges. Queue handling is "poll until the queue page goes away".
"""
from __future__ import annotations

from typing import Optional

from raindance.sites.generic import (
    GENERIC_ATC, GENERIC_CHECKOUT, GENERIC_PLACE_ORDER, GenericHandler,
)


# --------------------------------------------------------------------------- #
# 1. MINIMAL — selectors only
# --------------------------------------------------------------------------- #
class MinimalRetailerHandler(GenericHandler):
    name = "my_retailer"          # the site id used in config/tasks

    DEFAULT_SITE_PARAMS = {
        "selectors": {
            # Retailer-specific hooks first, generic text buttons as fallback.
            "atc": ["button[data-test='add-to-cart']"] + list(GENERIC_ATC),
            "checkout": ["a[href*='/checkout']"] + list(GENERIC_CHECKOUT),
            "place_order": ["button#place-order"] + list(GENERIC_PLACE_ORDER),
            # Leave empty when the retailer has no waiting room.
            "queue": [],
            # Optional: an element that only exists on the confirmation page.
            "confirmation": [],
        },
        # Only state what differs from the generic defaults.
        "waits": {"atc_enable_s": 30},
        "confirmation": {
            "order_id_regex": r"order\s*(?:number|#)[:\s#]*([0-9]{6,})",
        },
    }


# --------------------------------------------------------------------------- #
# 2. MEDIUM — custom queue step, everything else generic
# --------------------------------------------------------------------------- #
class MediumRetailerHandler(MinimalRetailerHandler):
    name = "my_retailer_with_queue"

    DEFAULT_SITE_PARAMS = {
        "selectors": {"queue": ["text=You're in line", "#queue-it"]},
        "waits": {"queue_max_s": 45 * 60},
    }

    def handle_queue(self, page, task, *, should_stop=None, on_status=None) -> bool:
        self.bind_task(task)
        # Retailer-specific pre-step goes here (e.g. dismiss a region modal),
        # still through the frame-aware lookup so store overrides apply:
        loc, _ = self.find_any(page, self.selectors(task, "dismiss"), visible=True)
        if loc is not None:
            self.human_click(page, loc)
        # Then the shared patient poll (selectors.queue / waits.queue_max_s).
        return self.wait_out_queue(page, task, should_stop=should_stop,
                                   on_status=on_status)

    # Report the extra key so the Catalog editor can show it.
    HONORS = GenericHandler.HONORS + ("selectors.dismiss",)


# --------------------------------------------------------------------------- #
# 3. FULL — every phase custom (skeleton)
# --------------------------------------------------------------------------- #
class FullRetailerHandler(GenericHandler):
    name = "my_full_retailer"

    DEFAULT_SITE_PARAMS = {
        "selectors": {
            "atc": ["button.buy"],
            "checkout": ["a.cart"],
            "place_order": ["button.pay"],
            "queue": [],
            "confirmation": ["h1:has-text('Order placed')"],
        },
    }

    def navigate(self, page, task: dict) -> None:
        self.bind_task(task)
        url = task.get("url") or ""
        if not url:
            raise ValueError("task has no url")
        page.goto(url, wait_until="domcontentloaded", timeout=45000)

    def handle_queue(self, page, task, *, should_stop=None, on_status=None) -> bool:
        self.bind_task(task)
        return self.wait_out_queue(page, task, should_stop=should_stop,
                                   on_status=on_status)

    def add_to_cart(self, page, task, *, should_stop=None, on_status=None) -> bool:
        self.bind_task(task)
        if on_status:
            on_status("atc")
        loc, sel = self.wait_for_first(
            page, self.selectors(task, "atc"),
            timeout_s=self.wait_s(task, "atc_enable_s", 30), should_stop=should_stop)
        if loc is None:
            return False
        self.human_click(page, loc)
        return True

    def default_checkout_fields(self, profile: dict, task: Optional[dict] = None) -> list:
        # This retailer's own markup; store `fields` are layered on top by
        # GenericHandler.checkout_fields.
        ship = profile.get("shipping") or {}
        return [
            ("#email", profile.get("email")),
            ("#zip", ship.get("zip")),
        ]

    def checkout(self, page, task, profile, *, dry_run=True, should_stop=None,
                 on_status=None) -> dict:
        self.bind_task(task)
        if on_status:
            on_status("checkout")
        self.enter_checkout(page, task)
        self.fill_checkout_fields(page, profile, task)
        if dry_run:
            return {"ok": True, "order_id": "", "error": "", "dry_run": True}
        if on_status:
            on_status("submitting")
        # place_order() = click selectors.place_order, then confirm_order():
        # order_id_regex / success_text / selectors.confirmation, up to
        # waits.confirm_s. Missing button → ok False, never a fake id.
        return self.place_order(page, task)

    def detect_seller(self, html: str, url: str) -> dict:
        return {"is_official": None, "seller": None}   # only assert with evidence
