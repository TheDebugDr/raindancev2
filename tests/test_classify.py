"""Fixture tests for the seller + MSRP classifier.

These exercise the decision table deterministically on canned page bodies — no
network, no browser, no bot-wall bypass. Run: `.venv/bin/python tests/test_classify.py`
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from raindance.core import classify as C
from raindance.sites import get_handler

ETB = "Pokemon TCG: Scarlet & Violet-Surging Sparks Elite Trainer Box"
BUNDLE = "Pokemon TCG: Scarlet & Violet-Surging Sparks Booster Bundle (6 Packs)"

# Fixtures carry STRUCTURED product data, because real PDPs do and because the
# gate now refuses a price it cannot anchor to this product. A bare "$49.99" in
# page text is exactly the unanchored reading that let a $9.00 protection-plan
# offer price a $179.99 listing, so it must NOT be sufficient on its own.
def _ld(name, price):
    return ('<script type="application/ld+json">'
            f'{{"@type":"Product","name":"{name}",'
            f'"offers":{{"@type":"Offer","price":"{price}"}}}}</script>')


def _walmart(name, price, seller_json):
    import json as _json
    payload = {"props": {"pageProps": {"initialData": {"data": {"product": {
        "name": name, "priceInfo": {"currentPrice": {"price": price}}}}}}}}
    return ('<script id="__NEXT_DATA__" type="application/json">'
            + _json.dumps(payload) + '</script>' + seller_json)


CASES = [
    # -- price is the decisive signal ------------------------------------- #
    ("walmart 1P at MSRP",
     _walmart(ETB, 59.99, '<div>Sold and shipped by Walmart.com</div> Add to cart'),
     "https://www.walmart.com/ip/x/1", "walmart", ETB, C.IN_STOCK_RETAIL),

    ("walmart 3P far over MSRP -> reseller, refused",
     _walmart(ETB, 149.99, '<div>Sold by GT Collectibles and Toys</div> Add to cart'),
     "https://www.walmart.com/ip/x/2", "walmart", ETB, C.IN_STOCK_RESELLER),

    ("walmart 3P AT MSRP -> buyable: the number is what protects you",
     _walmart(ETB, 59.99, '<div>Sold by MJ Holdings</div> Add to cart'),
     "https://www.walmart.com/ip/x/3", "walmart", ETB, C.IN_STOCK_RETAIL),

    ("bot wall -> blocked, never in stock",
     'Robot or human? Press & Hold to confirm. px-captcha Add to cart $49.99',
     "https://www.walmart.com/ip/x/4", "walmart", ETB, C.BLOCKED),

    ("sold out",
     _walmart(ETB, 59.99, '<div>Sold and shipped by Walmart.com</div> Sold out'),
     "https://www.walmart.com/ip/x/5", "walmart", ETB, C.OUT_OF_STOCK),

    ("unknown SKU tier -> unverified",
     _walmart("Mystery Grab Bag", 49.99,
              '<div>Sold and shipped by Walmart.com</div> Add to cart'),
     "https://www.walmart.com/ip/x/6", "walmart", "Mystery Grab Bag",
     C.IN_STOCK_UNVERIFIED),

    ("set not identifiable -> unverified, era is not guessed",
     _walmart("Pokemon Elite Trainer Box", 59.99,
              '<div>Sold and shipped by Walmart.com</div> Add to cart'),
     "https://www.walmart.com/ip/x/9", "walmart", "Pokemon Elite Trainer Box",
     C.IN_STOCK_UNVERIFIED),

    ("unanchored page-text price alone -> unverified, never buyable",
     '<div>Sold and shipped by Walmart.com</div> Add to cart $59.99',
     "https://www.walmart.com/ip/x/10", "walmart", ETB, C.IN_STOCK_UNVERIFIED),

    ("implausible low price (the $9 protection-plan misread) -> unverified",
     _walmart(ETB, 9.00, '<div>Sold and shipped by Walmart.com</div> Add to cart'),
     "https://www.walmart.com/ip/x/11", "walmart", ETB, C.IN_STOCK_UNVERIFIED),

    ("first-party but over MSRP -> over_msrp, not buyable",
     _walmart(ETB, 99.99, '<div>Sold and shipped by Walmart.com</div> Add to cart'),
     "https://www.walmart.com/ip/x/7", "walmart", ETB, C.IN_STOCK_OVER_MSRP),

    ("bundle 1P at MSRP",
     _walmart(BUNDLE, 26.94, '<div>Sold and shipped by Walmart.com</div> Add to cart'),
     "https://www.walmart.com/ip/x/8", "walmart", BUNDLE, C.IN_STOCK_RETAIL),

    # -- Target: the marketplace listings that look first-party ------------ #
    ("target 1P at MSRP",
     '<script>__TGT_DATA__={}</script>' + _ld(ETB, 59.99)
     + '<span>Sold &amp; shipped by</span><span>Target</span> Add to cart',
     "https://www.target.com/p/x/-/A-1", "target", ETB, C.IN_STOCK_RETAIL),

    ("target Plus 3P over MSRP -> reseller (LIVE case: $219.99 vs $59.99)",
     '<script>__TGT_DATA__={}</script>' + _ld(ETB, 219.99)
     + '<a aria-label="Sold &amp; shipped by Collectors Emporium. View partner details"'
       ' data-test="targetPlusExtraInfoSection" href="/sp/collectors-emporium/-/N-10026644">'
       '<span>Sold &amp; shipped by</span><span>Collectors Emporium</span></a> Add to cart',
     "https://www.target.com/p/x/-/A-2", "target", ETB, C.IN_STOCK_RESELLER),

    ("target: a distributor at $183 on a $161.64 box is still refused",
     '<script>__TGT_DATA__={}</script>'
     + _ld("Pokemon TCG: 30th Celebration Booster Box", 183.00)
     + '<span>Sold &amp; shipped by</span><span>National Distributor</span> Add to cart',
     "https://www.target.com/p/x/-/A-3", "target",
     "Pokemon TCG: 30th Celebration Booster Box", C.IN_STOCK_RESELLER),

    # -- Pokemon Center: no marketplace, so identity is the domain --------- #
    ("pokemon center at MSRP",
     _ld(ETB, 59.99) + ' Add to cart',
     "https://www.pokemoncenter.com/product/x", "pokemon_center", ETB,
     C.IN_STOCK_RETAIL),

    ("pokemon center lookalike domain -> not official, and over MSRP",
     _ld(ETB, 89.99) + ' Add to cart',
     "https://www.pokemoncenter.store/product/x", "pokemon_center", ETB,
     C.IN_STOCK_RESELLER),
]


def run() -> int:
    failures = 0
    for label, html, url, site, pname, expected in CASES:
        handler = get_handler(site)
        v = C.classify(html, url, {"name": pname}, source="browser", handler=handler)
        ok = v["status"] == expected
        # in_stock_retail is the ONLY status the checkout gate may act on.
        buyable = v["status"] == C.BUYABLE
        must_be_buyable = expected == C.IN_STOCK_RETAIL
        gate_ok = buyable == must_be_buyable
        mark = "ok " if (ok and gate_ok) else "FAIL"
        if not (ok and gate_ok):
            failures += 1
        print(f"[{mark}] {label}\n       -> {v['status']}  (expected {expected})  "
              f"| {v['reason']}")
    print(f"\n{len(CASES) - failures}/{len(CASES)} passed")
    return failures


if __name__ == "__main__":
    sys.exit(1 if run() else 0)


# -- pytest entry ---------------------------------------------------------- #
# The script-style run() above stays for `python tests/test_classify.py`; these
# make the SAME cases visible to `pytest`, one node per case with a real assert.
try:
    import pytest

    @pytest.mark.parametrize("label,html,url,site,pname,expected", CASES,
                             ids=[c[0] for c in CASES])
    def test_classify_case(label, html, url, site, pname, expected):
        handler = get_handler(site)
        v = C.classify(html, url, {"name": pname}, source="browser", handler=handler)
        assert v["status"] == expected, f"{label}: {v['status']} != {expected} ({v['reason']})"
        # in_stock_retail is the ONLY status the checkout gate may act on.
        assert (v["status"] == C.BUYABLE) == (expected == C.IN_STOCK_RETAIL), \
            f"{label}: gate mismatch for {v['status']}"
except ImportError:
    pass
