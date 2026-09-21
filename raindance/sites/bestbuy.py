"""Best Buy handler — first-party vs Best Buy Marketplace seller detection.

Best Buy (marketplace relaunched Aug 2025, Mirakl-powered) exposes the seller
only as rendered text near the buy box — there is no stable embedded JSON key —
so seller detection is reliable on a browser-rendered body, not a plain fetch.
Inherits the generic browser checkout flow.

CAVEAT — best-effort defaults. The selectors below follow Best Buy's public
markup conventions (`.add-to-cart-button`, checkout button classes) and are
NOT verified against the live site in this build. Override any of them from
the Catalog page: retailer_configs.bestbuy.site_params.selectors.*.
"""
from __future__ import annotations

import re

from raindance.sites.generic import (
    GENERIC_ATC, GENERIC_CHECKOUT, GENERIC_PLACE_ORDER, GenericHandler,
)

_BESTBUY_ATC = [
    "button.add-to-cart-button",
    "button[data-button-state='ADD_TO_CART']",
    "button[data-sku-id]:has-text('Add to Cart')",
]

_BESTBUY_CHECKOUT = [
    "button.checkout-buttons__checkout",
    "button:has-text('Checkout')",
    "a[href*='/checkout/r/fast-track']",
]

_BESTBUY_PLACE = [
    "button.button__fast-track",
    "button:has-text('Place your order')",
    "button[data-track='Place your Order - Contact Card']",
]

# Specific on purpose — a loose probe would hold the run on any page that
# mentions the word "queue".
_BESTBUY_QUEUE = [
    "text=You're in line",
    "text=virtual queue",
    "text=waiting room",
    "#queue-it",
    ".queue-it",
]

_BESTBUY_CONFIRMATION = [
    "text=Thanks for your order",
    "text=Order Number",
]


class BestBuyHandler(GenericHandler):
    name = "bestbuy"

    DEFAULT_SITE_PARAMS = {
        "selectors": {
            "atc": _BESTBUY_ATC + list(GENERIC_ATC),
            "checkout": _BESTBUY_CHECKOUT + list(GENERIC_CHECKOUT),
            "place_order": _BESTBUY_PLACE + list(GENERIC_PLACE_ORDER),
            "queue": list(_BESTBUY_QUEUE),
            "confirmation": list(_BESTBUY_CONFIRMATION),
        },
        "confirmation": {
            "order_id_regex": r"order\s*(?:number|#)[:\s#]*(BBY01-[0-9]{9,}|[0-9]{10,})",
        },
    }

    def detect_seller(self, html: str, url: str) -> dict:
        """First-party Best Buy vs a Best Buy marketplace seller.

        Exact-name membership, not `startswith("best buy")` — that admitted
        partners trading as "BestBuy Deals Outlet".
        """
        from raindance.core import seller as _seller
        return _seller.verdict(html, "bestbuy")
