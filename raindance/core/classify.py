"""The single seller + MSRP classifier, shared by the HTTP monitor and the
browser checkout gate.

The whole point of the gate is that this file is the ONLY place that decides
whether a listing is buyable. It never tries to defeat bot protection (a wall
is reported as `blocked`), it never invents data (missing price/seller/stock
is `unverified`/`unknown`, never assumed), and it never prices a listing off
an unanchored number (see raindance/core/price.py).

The invariant, from tests/test_gate.py:

    A purchase is authorised ONLY when an anchored, product-scoped price sits
    at or under that product's era-correct MSRP ceiling.

Price is decisive. Seller identity annotates the decision and, for Target,
supplies a structural marketplace signal — but a third party charging MSRP is
charging the right number, and a "national distributor" charging $183 for a
$161.64 box is refused on the number regardless of what it calls itself.
The one thing an unknown seller still blocks: without ANY seller evidence we
cannot confirm the anchored price belongs to this listing's buy box, so that
stays unverified.
"""
from __future__ import annotations

import re
from typing import Optional

from raindance.core import msrp as _msrp
from raindance.core import price as _price

IN_STOCK_RETAIL = "in_stock_retail"
IN_STOCK_RESELLER = "in_stock_reseller"
IN_STOCK_OVER_MSRP = "in_stock_over_msrp"
IN_STOCK_UNVERIFIED = "in_stock_unverified"
OUT_OF_STOCK = "out_of_stock"
BLOCKED = "blocked"
UNKNOWN = "unknown"
ERROR = "error"

BUYABLE = IN_STOCK_RETAIL

_WALL = re.compile(
    r"robot or human|px-captcha|captcha|are you a robot"
    r"|verify you are (?:a )?human"
    r"|press & ?hold|datadome|perimeterx|access denied|request blocked"
    r"|attention required|security check",
    re.I,
)

# Dormant boilerplate is not a challenge: a <style>#px-captcha-modal …</style>
# rule ships on every Target PDP whether or not a challenge is showing, and a
# vendor string inside a <script> config block is the same kind of noise.
# Strip both before matching — confirmed live, through the real app.
_STRIP_WALL_NOISE = re.compile(
    r"<(script|style)\b[^>]*>.*?</\1>", re.I | re.S)


def is_bot_wall(html: str) -> bool:
    """True when the body is a bot-protection challenge, not the product."""
    body = _STRIP_WALL_NOISE.sub(" ", html or "")
    return bool(_WALL.search(body))


def wall_match(html: str) -> str | None:
    """Which bot-wall pattern fired, or None when no wall is present.

    Forensics only — `is_bot_wall` remains the single decider. Returns the
    matched challenge text (e.g. "perimeterx", "Access Denied") so a
    failure record can say WHY the wall fired, not just that it did.
    """
    m = _WALL.search(_STRIP_WALL_NOISE.sub(" ", html or ""))
    return m.group(0).strip() if m else None


def classify(html, url, product=None, *, source="http", handler=None,
             settings=None, signals=None) -> dict:
    """Seller + MSRP + stock verdict for one listing.

    `signals` is the retailer-API fast path (Target RedSky captures): price,
    stock, is_official and seller read from the retailer's own product data
    instead of the rendered HTML. When signals are present the HTML is not
    parsed for price or stock at all.

    Returns a dict carrying the full evidence trail: status, reason, price,
    price_source, price_scoped, msrp, threshold, sku_type, era,
    tier_confidence, at_msrp, band, seller, is_official, source.
    """
    signals = signals or {}
    result = {
        "status": UNKNOWN,
        "seller": None,
        "is_official": None,
        "price": None,
        "price_source": "none",
        "price_scoped": False,
        "msrp": None,
        "threshold": None,
        "sku_type": None,
        "era": None,
        "tier_confidence": "none",
        "at_msrp": None,
        "band": "unknown",
        "source": source,
        "reason": "",
    }

    # A challenge body is never read as a product. Retailer-API signals come
    # from the retailer's own product endpoint, so they bypass this.
    if not signals and is_bot_wall(html):
        result.update(
            status=BLOCKED,
            reason="page is a bot-protection challenge, not the product",
        )
        return result

    product = product or {}
    name = product.get("name") or url or ""

    # -- seller ---------------------------------------------------------- #
    seller_info = {"is_official": None, "seller": None}
    if "is_official" in signals or "seller" in signals:
        seller_info = {"is_official": signals.get("is_official"),
                       "seller": signals.get("seller")}
    elif handler is not None:
        try:
            seller_info = handler.detect_seller(html, url) or seller_info
        except Exception:
            pass
    is_official = seller_info.get("is_official")
    seller = seller_info.get("seller")
    result.update(is_official=is_official, seller=seller)

    # -- stock ----------------------------------------------------------- #
    if "stock" in signals:
        stock = signals.get("stock")
    else:
        stock = None
        if handler is not None:
            try:
                stock = handler.detect_stock_signals(html)
            except Exception:
                stock = None

    # -- price: anchored or nothing -------------------------------------- #
    pp = _price.product_price(html, signals=signals, product_name=name or None)
    price = pp["price"]
    scoped = bool(pp["scoped"])
    result.update(price=price, price_source=pp["source"], price_scoped=scoped)

    # -- MSRP judgement --------------------------------------------------- #
    # A confirmed first-party listing is judged on the retail band (the
    # retailer's own markup is a real price the buyer accepts); everyone
    # else — marketplace, distributor, or unconfirmed — gets the tight band.
    # See the calibration note in msrp_catalog.DEFAULTS.
    pv = _msrp.price_verdict(price, name, allin=False, settings=settings,
                             first_party=is_official)
    result.update(msrp=pv["msrp"], threshold=pv["threshold"],
                  sku_type=pv["sku_type"], era=pv["era"],
                  tier_confidence=pv["confidence"],
                  at_msrp=pv["at_msrp"], band=pv["band"])

    # -- the gate ---------------------------------------------------------- #
    if stock is False:
        result.update(status=OUT_OF_STOCK,
                      reason="sold out / not purchasable")
        return result
    if stock is None:
        result.update(status=UNKNOWN,
                      reason="no clear stock signal on the page")
        return result
    if price is None:
        result.update(status=IN_STOCK_UNVERIFIED,
                      reason="in stock but price unreadable")
        return result
    if not scoped:
        # A dollar amount found loose in the page text is not the buy-box
        # price — it could be a protection plan, a carousel, a financing
        # figure. It must never authorise a purchase on its own.
        result.update(status=IN_STOCK_UNVERIFIED,
                      reason="in stock but the price is not anchored to this "
                             "product (unverified read)")
        return result
    if pv["at_msrp"] is None:
        if pv["band"] == "implausible":
            reason = (f"in stock but price ${price:.2f} is implausibly far "
                      f"below MSRP (${pv['msrp']:.2f}) — likely a misread, "
                      f"not a deal")
        else:
            reason = ("in stock but product tier unknown for MSRP check "
                      f"('{pv['sku_type']}')")
        result.update(status=IN_STOCK_UNVERIFIED, reason=reason)
        return result
    if not pv["at_msrp"]:
        pct = int((price / pv["msrp"] - 1) * 100) if pv["msrp"] else 0
        if is_official is False:
            result.update(
                status=IN_STOCK_RESELLER,
                reason=f"third-party seller {seller or '(unknown)'} at "
                       f"{pct}% over MSRP")
            return result
        result.update(
            status=IN_STOCK_OVER_MSRP,
            reason=f"{pct}% over MSRP (${price:.2f} vs ${pv['msrp']:.2f})")
        return result

    # At or under the ceiling the price decides. The seller annotates.
    if is_official is True:
        reason = (f"sold by the retailer at MSRP "
                  f"(${price:.2f} vs ${pv['msrp']:.2f})")
    elif is_official is False:
        reason = (f"third-party seller {seller or '(unknown)'} at MSRP "
                  f"(${price:.2f} vs ${pv['msrp']:.2f})")
    else:
        # No seller evidence at all: we cannot confirm the anchored price
        # belongs to this listing's buy box. Fail safe.
        result.update(
            status=IN_STOCK_UNVERIFIED,
            reason=f"at MSRP (${price:.2f}) but the seller could not be "
                   f"confirmed")
        return result
    result.update(status=IN_STOCK_RETAIL, reason=reason)
    return result
