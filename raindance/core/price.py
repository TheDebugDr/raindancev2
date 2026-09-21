"""Product-scoped price extraction — the anchor the MSRP gate stands on.

The defect this replaces: ``parse_price`` took the leftmost price-shaped match
in the *entire* rendered document. A PDP carries many prices that are not the
buy-box price — a $9.00 protection plan, a "customers also bought" carousel, a
struck-through list price, a financing instalment, a shipping threshold. Taking
the first one meant a $179.99 listing could be judged against a $9.00 read and
pass an MSRP gate it should have failed.

Two rules fix that, and both matter:

1. PREFER A PRODUCT-SCOPED SOURCE. A price lifted from a structured node that
   describes *this* product — a retailer API payload, a JSON-LD ``Offer``, the
   Walmart ``__NEXT_DATA__`` product node — is anchored. A dollar amount found
   loose in the page text is not. Only an anchored price may authorise a
   purchase; see ``require_scoped`` below.

2. WHEN UNANCHORED, FAIL HIGH. If we must fall back to page text we take the
   MAXIMUM plausible candidate, never the first. Reading a price too high can
   only ever cause a missed buy. Reading it too low causes a wrong purchase.
   The asymmetry is the whole point: this gate protects money, not throughput.

Everything returns provenance alongside the number so the log can say exactly
where a price came from when a gate decision is questioned afterwards.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

# Absolute plausibility bounds for sealed TCG product. Anything outside is not
# a product price — it is a gift-card value, a cents-denominated integer, a
# phone number fragment, or a financing figure.
MIN_PLAUSIBLE = 0.99
MAX_PLAUSIBLE = 2000.0

_LD_JSON = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.I | re.S)
_NEXT_DATA = re.compile(
    r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', re.I | re.S)
_TGT_DATA = re.compile(
    r'__TGT_DATA__\s*[:=]\s*(\{.*?\})\s*[;<]', re.I | re.S)

# Cents-denominated keys must be divided by 100. Keeping them in the same
# alternation as dollar keys is how a 17999 became "$17,999" and was discarded,
# or worse, how a stray 900 became $900.
_CENTS_KEYS = ("priceincents", "price_in_cents", "current_price_in_cents",
               "pricecents", "amountincents")
_DOLLAR_KEYS = ("currentprice", "current_price", "current_retail", "price",
                "listprice", "list_price", "reg_retail", "formatted_current_price",
                "customerprice", "saleprice", "sale_price", "finalprice")

_TEXT_PRICE = re.compile(r"\$\s?([0-9]{1,4}(?:,[0-9]{3})*(?:\.[0-9]{2})?)")


def _plausible(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if MIN_PLAUSIBLE <= f <= MAX_PLAUSIBLE:
        return round(f, 2)
    return None


def _walk(node: Any, depth: int = 0):
    """Yield every (key, value) pair in a nested JSON structure."""
    if depth > 12:
        return
    if isinstance(node, dict):
        for k, v in node.items():
            yield k, v
            yield from _walk(v, depth + 1)
    elif isinstance(node, list):
        for v in node[:200]:
            yield from _walk(v, depth + 1)


def _prices_in(node: Any) -> list[float]:
    """Every plausible price inside one JSON subtree, cents keys converted."""
    found: list[float] = []
    for k, v in _walk(node):
        key = str(k).lower()
        if isinstance(v, (dict, list)):
            continue
        if key in _CENTS_KEYS:
            try:
                p = _plausible(float(v) / 100.0)
            except (TypeError, ValueError):
                p = None
            if p is not None:
                found.append(p)
            continue
        if key in _DOLLAR_KEYS:
            raw = v
            if isinstance(raw, str):
                raw = raw.replace("$", "").replace(",", "").strip()
            p = _plausible(raw)
            if p:
                found.append(p)
    return found


def _json_blocks(html: str) -> list[tuple[str, Any]]:
    """(source_label, parsed_json) for each structured block we understand."""
    out: list[tuple[str, Any]] = []
    for m in _LD_JSON.finditer(html or ""):
        try:
            out.append(("json-ld", json.loads(m.group(1).strip())))
        except Exception:
            continue
    m = _NEXT_DATA.search(html or "")
    if m:
        try:
            out.append(("__NEXT_DATA__", json.loads(m.group(1).strip())))
        except Exception:
            pass
    m = _TGT_DATA.search(html or "")
    if m:
        try:
            out.append(("__TGT_DATA__", json.loads(m.group(1))))
        except Exception:
            pass
    return out


def _ld_product_offers(doc: Any) -> list[dict]:
    """Every schema.org Product node carrying an offer price."""
    found: list[dict] = []

    def visit(node: Any, depth: int = 0):
        if depth > 10:
            return
        if isinstance(node, list):
            for n in node[:100]:
                visit(n, depth + 1)
            return
        if not isinstance(node, dict):
            return
        types = node.get("@type")
        types = [types] if isinstance(types, str) else (types or [])
        if any(str(t).lower() == "product" for t in types):
            offers = node.get("offers")
            for off in (offers if isinstance(offers, list) else [offers]):
                if not isinstance(off, dict):
                    continue
                raw = off.get("price", off.get("lowPrice"))
                if isinstance(raw, str):
                    raw = raw.replace("$", "").replace(",", "").strip()
                p = _plausible(raw)
                if p is not None:
                    found.append({"price": p, "name": str(node.get("name") or "")[:120]})
        for v in node.values():
            visit(v, depth + 1)

    visit(doc)
    return found


def _walmart_product_price(doc: Any) -> Optional[float]:
    """__NEXT_DATA__ → the product node's own current price, not the page's."""
    node: Any = doc
    for key in ("props", "pageProps", "initialData", "data", "product"):
        if not isinstance(node, dict):
            return None
        node = node.get(key)
        if node is None:
            return None
    info = node.get("priceInfo") if isinstance(node, dict) else None
    if isinstance(info, dict):
        cur = info.get("currentPrice")
        if isinstance(cur, dict):
            p = _plausible(cur.get("price"))
            if p is not None:
                return p
    prices = _prices_in(node)
    return max(prices) if prices else None


def extract_candidates(html: str) -> list[dict]:
    """All price readings with provenance.

    Each item: {"price": float, "source": str, "scoped": bool, "name": str}
    ``scoped`` marks a reading that came from a node describing this product,
    as opposed to a dollar amount found loose in the page.
    """
    out: list[dict] = []
    if not html:
        return out

    for label, doc in _json_blocks(html):
        if label == "json-ld":
            for off in _ld_product_offers(doc):
                out.append({"price": off["price"], "source": "json-ld:Offer",
                            "scoped": True, "name": off["name"]})
        elif label == "__NEXT_DATA__":
            p = _walmart_product_price(doc)
            if p is not None:
                out.append({"price": p, "source": "__NEXT_DATA__:product",
                            "scoped": True, "name": ""})
        elif label == "__TGT_DATA__":
            for p in _prices_in(doc)[:6]:
                out.append({"price": p, "source": "__TGT_DATA__",
                            "scoped": True, "name": ""})

    for m in _TEXT_PRICE.finditer(html):
        p = _plausible(m.group(1).replace(",", ""))
        if p is not None:
            out.append({"price": p, "source": "page-text", "scoped": False, "name": ""})
    return out


def product_price(html: str, *, signals: dict | None = None,
                  product_name: str | None = None) -> dict:
    """Resolve the price that describes THIS product.

    Returns {"price": float|None, "source": str, "scoped": bool,
             "candidates": int, "spread": tuple|None}.

    ``scoped`` False means the number came from unanchored page text and must
    not, on its own, authorise a purchase.
    """
    signals = signals or {}
    if signals.get("price") is not None:
        p = _plausible(signals["price"])
        if p is not None:
            return {"price": p, "source": "retailer-api", "scoped": True,
                    "candidates": 1, "spread": None}

    cands = extract_candidates(html or "")
    if not cands:
        return {"price": None, "source": "none", "scoped": False,
                "candidates": 0, "spread": None}

    scoped = [c for c in cands if c["scoped"]]
    if scoped:
        # Prefer an offer whose name resembles the product we are buying; with
        # no usable name, or a tie, take the highest scoped reading — a page
        # with several product nodes is usually the PDP plus accessories, and
        # the accessory is the cheap one.
        best = None
        if product_name:
            want = re.sub(r"[^a-z0-9 ]+", " ", product_name.lower()).split()
            want = [w for w in want if len(w) > 2][:8]
            if want:
                def score(c: dict) -> int:
                    hay = c["name"].lower()
                    return sum(1 for w in want if w in hay)
                ranked = sorted(scoped, key=lambda c: (score(c), c["price"]), reverse=True)
                if score(ranked[0]) > 0:
                    best = ranked[0]
        if best is None:
            best = max(scoped, key=lambda c: c["price"])
        vals = [c["price"] for c in scoped]
        return {"price": best["price"], "source": best["source"], "scoped": True,
                "candidates": len(cands),
                "spread": (min(vals), max(vals)) if len(vals) > 1 else None}

    # Unanchored fallback: highest plausible amount on the page. Failing high
    # can only cost a missed buy; failing low buys the wrong thing.
    vals = [c["price"] for c in cands]
    return {"price": max(vals), "source": "page-text:max", "scoped": False,
            "candidates": len(cands),
            "spread": (min(vals), max(vals)) if len(vals) > 1 else None}
