"""Target.com site handler — Target selectors on top of the generic flow.

CAVEAT — best-effort defaults. The data-test hooks below were taken from
Target's public PDP/cart/checkout markup at the time of writing and are NOT
verified against the live site in this build (no browser, no network). When
they drift, override any of them from the Catalog page:
retailer_configs.target.site_params.selectors.{atc,checkout,place_order,queue}.
"""
from __future__ import annotations

import re
from typing import Optional

from raindance.sites.generic import (
    GENERIC_ATC, GENERIC_CHECKOUT, GENERIC_PLACE_ORDER, GenericHandler,
)

# Target PDP buy-box buttons (data-test hooks are Target's own test ids).
_TARGET_ATC = [
    "button[data-test='shipItButton']",
    "button[data-test='add-to-cart']",
    "button[data-test='orderPickupButton']",
    "button:has-text('Add to cart')",
    "button:has-text('Ship it')",
]

# Cart → checkout entry, then the final place-order control.
_TARGET_CHECKOUT = [
    "button[data-test='checkout-button']",
    "a[data-test='checkout-button']",
    "button:has-text('Check out')",
    "a[href*='/checkout']",
]

_TARGET_PLACE = [
    "button[data-test='placeOrderButton']",
    "button:has-text('Place your order')",
    "button:has-text('Place order')",
]

# Target's high-demand waiting room. Unverified. Deliberately specific: a
# loose probe like "text=queue" would hold the run for queue_max_s on any page
# that merely mentions the word.
_TARGET_QUEUE = [
    "text=You're in line",
    "text=you are in line",
    "text=virtual queue",
    "text=waiting room",
    "#queue-it",
    ".queue-it",
]

_TARGET_CONFIRMATION = [
    "[data-test='orderConfirmation']",
    "text=Thanks for your order",
    "text=Order number",
]


class TargetHandler(GenericHandler):
    """Target.com: Target-specific selectors on top of the generic browser flow.

    add_to_cart / checkout come from GenericHandler (quantity, CAPTCHA hook,
    profile form fill, dry-run stop before place-order, real confirmation
    check — never a fabricated order id). Only the defaults and seller/stock
    detection are Target's; every default is overridable per store.
    """

    name = "target"

    DEFAULT_SITE_PARAMS = {
        "selectors": {
            # Target hooks first, generic text buttons as the fallback.
            "atc": _TARGET_ATC + list(GENERIC_ATC),
            "checkout": _TARGET_CHECKOUT + list(GENERIC_CHECKOUT),
            "place_order": _TARGET_PLACE + list(GENERIC_PLACE_ORDER),
            "queue": list(_TARGET_QUEUE),
            "confirmation": list(_TARGET_CONFIRMATION),
        },
        "confirmation": {
            "order_id_regex": r"order\s*(?:number|#)[:\s#]*([0-9]{6,})",
        },
    }

    def navigate(self, page, task: dict) -> None:
        self.bind_task(task)
        url = task.get("url")
        pid = task.get("product_id")
        if pid and not url:
            url = f"https://www.target.com/p/-/A-{pid}"
        if not url:
            raise ValueError("Target task needs url or product_id")
        page.goto(url, wait_until="domcontentloaded")
        self.log(f"Navigated to Target product {pid or url}", "info")

    def handle_queue(self, page, task, *, should_stop=None, on_status=None) -> bool:
        """Wait out Target's waiting room: poll `selectors.queue` until it is
        gone (up to waits.queue_max_s). Same patient poll as every handler."""
        self.bind_task(task)
        self.log("Checking for Target queue...", "info")
        if not self._captcha(page, task, should_stop, on_status):
            return False
        return self.wait_out_queue(page, task, should_stop=should_stop,
                                   on_status=on_status)

    def detect_stock_signals(self, html: str) -> Optional[bool]:
        low = (html or "").lower()
        if "out of stock" in low or "sold out" in low or "notify me" in low:
            return False
        if "add to cart" in low or "ship it" in low or "add for shipping" in low:
            return True
        return None

    def detect_seller(self, html: str, url: str) -> dict:
        """Target Plus (marketplace) vs first-party Target.

        Delegates to the shared evidence-only detector. The previous version
        asserted first-party whenever the page carried Target's own hydration
        markers and no third-party line parsed — but `__tgt_data__` and
        `data-test="product-title"` are on EVERY Target PDP, marketplace ones
        included, and the third-party line failed to parse whenever the partner
        name sat inside a tag. A live Target Plus listing (Prismatic Evolutions
        ETB, $219.99 against a $59.99 MSRP) read as "sold by Target" because of
        exactly that pair of bugs.
        """
        from raindance.core import seller as _seller
        return _seller.verdict(html, "target")
