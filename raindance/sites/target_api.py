"""Target product data — price + stock, read from the browser's own session.

Why this replaced the original design
---------------------------------------
This module used to call `redsky.target.com` directly with a `key=` query
param copied off a live network request — reading published product data,
the same JSON target.com's own frontend consumes. That premise held once.
Verified live tonight that it no longer does: a direct request to
`redsky.target.com`, even a copied key AND the same browser session that had
just cleared target.com's challenge, gets a 403 with an embedded captcha
URL. Target moved this data behind the *same* bot protection as the main
site, and — critically — `redsky.target.com` is a different origin from
`www.target.com`, so cookies from a cleared `www.target.com` session don't
carry over to it. No key fixes that; the origin itself is walled off now.

What actually works, verified live: target.com's own frontend no longer
calls `redsky.target.com` from the browser at all. It calls
`www.target.com/cdui_orchestrations/v1/pages/pdp/deferred_enrichment/modules`
— same-origin, so it rides on the exact session/cookies that already passed
whatever challenge the PDP navigation cleared. This module listens for that
response during the navigation the runner already performs; it does not
place a second, independent request, and there is nothing to "not
configure" — no key, no signup, no enable/disable toggle. If Target changes
this shape again, `signals_from_captures` returns an empty dict and the
caller falls back to the HTML path exactly as it always has.

Seller identity is deliberately NOT sourced here. Checked directly: the
seller name on a Target Plus listing ("Sold & shipped by X") does not
appear anywhere in these API payloads — Target renders it from the initial
page HTML, not this endpoint. `raindance.core.seller` already reads that
correctly (linearised markup, attribute scan, the /sp/…/-/N-… marketplace
marker), so this module leaves `is_official`/`seller` out of its signals
entirely; `classify()` already falls through to the handler's own
`detect_seller(html, url)` whenever those keys are absent.
"""
from __future__ import annotations

import re
from contextlib import contextmanager
from typing import Any, Optional

_TCIN_RE = re.compile(r"/A-(\d{6,10})", re.I)
_RESPONSE_MARKER = ("cdui_orchestrations", "deferred_enrichment")


def tcin_from_url(url: str) -> Optional[str]:
    """Pull the Target TCIN out of a product URL, or None if it isn't one."""
    m = _TCIN_RE.search(url or "")
    return m.group(1) if m else None


def is_configured(settings=None, *, site: str = "target") -> bool:
    """Always True for Target: this path needs no key, no signup, no toggle
    — it only listens to a request the page makes on its own. Kept as a
    function so callers that gate on it (`if target_api.is_configured(...)`)
    do not need to change."""
    return True


def _first_price(price: dict) -> Optional[float]:
    if not isinstance(price, dict):
        return None
    for k in ("current_retail", "reg_retail", "formatted_current_price"):
        v = price.get(k)
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            m = re.search(r"([0-9]+(?:\.[0-9]{1,2})?)", v)
            if m:
                return float(m.group(1))
    return None


def _dig(obj: Any, *path: str) -> Any:
    for p in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(p)
    return obj


def _matches(url: str) -> bool:
    return all(marker in (url or "") for marker in _RESPONSE_MARKER)


@contextmanager
def capture(page):
    """Context manager: yields a list that fills with every matching JSON
    response fired during the block. Use it around the SAME navigation the
    runner already performs — no extra request, no reload.

        with target_api.capture(page) as captured:
            handler.navigate(page, task)
            page.wait_for_timeout(1500)   # let async calls land
        signals = target_api.signals_from_captures(captured)

    A response that isn't valid JSON (an interstitial, a challenge page) is
    silently skipped — this only ever adds signal, never raises.
    """
    captured: list = []

    def _on_response(resp):
        if not _matches(resp.url):
            return
        try:
            captured.append(resp.json())
        except Exception:
            pass

    page.on("response", _on_response)
    try:
        yield captured
    finally:
        try:
            page.remove_listener("response", _on_response)
        except Exception:
            pass


def signals_from_captures(captured: list) -> dict:
    """Merge every captured module across every response into one signals
    dict. Target fires several requests per PDP load, each carrying a
    different subset of modules (price in one, fulfillment in another) —
    verified live: a single response is not guaranteed to carry both.

    Returns {"price": float|None, "stock": bool|None}. Deliberately no
    is_official/seller key — see the module docstring.
    """
    price: Optional[float] = None
    stock: Optional[bool] = None

    for body in captured or []:
        for module in (body or {}).get("modules") or []:
            product = _dig(module, "module_data", "data", "product")
            if not isinstance(product, dict):
                continue

            if price is None:
                p = _first_price(product.get("price") or {})
                if p is not None:
                    price = p

            if stock is None:
                status = _dig(product, "fulfillment", "shipping_options",
                              "availability_status")
                if isinstance(status, str):
                    s = status.upper()
                    if s in ("IN_STOCK", "IN_STOCK_ONLINE", "PRE_ORDER_SELLABLE",
                             "LIMITED_STOCK"):
                        stock = True
                    elif s in ("OUT_OF_STOCK", "UNAVAILABLE", "NOT_SOLD_IN_STORE",
                               "DISCONTINUED"):
                        stock = False

        if price is not None and stock is not None:
            break

    out = {}
    if price is not None:
        out["price"] = price
    if stock is not None:
        out["stock"] = stock
    return out
