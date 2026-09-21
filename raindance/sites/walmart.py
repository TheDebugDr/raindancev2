"""Walmart.com handler — first-party vs marketplace seller detection.

Inherits the generic browser checkout flow; adds Walmart-specific seller
detection so the classifier can tell "Sold and shipped by Walmart.com" (retail)
from a third-party marketplace listing (frequently a reseller above MSRP).

Seller identity is only ever used as corroboration — the price-vs-MSRP check is
the primary signal — so this never needs to be perfect, only honest: it returns
is_official=None when it cannot tell, and the classifier fails safe on that.

CAVEAT — best-effort defaults. The selectors below follow Walmart's public
`data-automation-id` / `data-testid` conventions and are NOT verified against
the live site in this build. Override any of them from the Catalog page:
retailer_configs.walmart.site_params.selectors.*.
"""
from __future__ import annotations

import re

from raindance.sites.generic import (
    GENERIC_ATC, GENERIC_CHECKOUT, GENERIC_PLACE_ORDER, GenericHandler,
)

# First-party seller identifiers seen in Walmart's __NEXT_DATA__ payload.
_WALMART_1P_IDS = {"0", "f55cdc31ab754bb68fe0b39041159d63"}

_WALMART_ATC = [
    "button[data-automation-id='atc']",
    "button[data-testid='add-to-cart-btn']",
    "button:has-text('Add to cart')",
]

_WALMART_CHECKOUT = [
    "button[data-automation-id='checkout']",
    "button[data-testid='checkout-button']",
    "button:has-text('Continue to checkout')",
]

_WALMART_PLACE = [
    "button[data-automation-id='place-order']",
    "button[data-testid='place-order-btn']",
    "button:has-text('Place order')",
]

# Specific on purpose — a loose probe would hold the run on any page that
# mentions the word "queue".
_WALMART_QUEUE = [
    "text=You're in line",
    "text=virtual queue",
    "text=waiting room",
    "#queue-it",
    ".queue-it",
]

_WALMART_CONFIRMATION = [
    "text=Thanks for your order",
    "text=Order number",
    "[data-testid='order-confirmation']",
]


class WalmartHandler(GenericHandler):
    name = "walmart"

    DEFAULT_SITE_PARAMS = {
        "selectors": {
            "atc": _WALMART_ATC + list(GENERIC_ATC),
            "checkout": _WALMART_CHECKOUT + list(GENERIC_CHECKOUT),
            "place_order": _WALMART_PLACE + list(GENERIC_PLACE_ORDER),
            "queue": list(_WALMART_QUEUE),
            "confirmation": list(_WALMART_CONFIRMATION),
        },
        "confirmation": {
            "order_id_regex": r"order\s*(?:number|#)[:\s#]*([0-9]{6,}-?[0-9]{5,})",
        },
    }

    def detect_seller(self, html: str, url: str) -> dict:
        low = (html or "").lower()
        # Prefer the embedded JSON seller fields (server-rendered in the page).
        if re.search(r'"sellertype"\s*:\s*"internal"', low):
            return {"is_official": True, "seller": "Walmart.com"}
        m = re.search(r'"seller(?:name|displayname)"\s*:\s*"([^"]+)"', html, re.I)
        if m:
            name = m.group(1)
            official = name.strip().lower() in ("walmart.com", "walmart")
            return {"is_official": official, "seller": name}
        m = re.search(r'"sellerid"\s*:\s*"([^"]+)"', low)
        if m and m.group(1) in _WALMART_1P_IDS:
            return {"is_official": True, "seller": "Walmart.com"}
        # Fall back to the rendered "Sold by" text. Delegated so the tag- and
        # entity-tolerant matcher and the exact-name allowlist are shared;
        # `startswith("walmart")` used to admit "Walmart Resellers LLC".
        from raindance.core import seller as _seller
        return _seller.verdict(html, "walmart")
