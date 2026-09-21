"""The anti-scalper gate, end to end.

Every case here is anchored to something observed on a live page on 2026-09-04
rather than invented, because the whole value of this gate is that its numbers
match reality. The listings, prices and markup are recorded in
``raindance/core/msrp_catalog.py``.

The invariant these tests exist to hold:

    A purchase is authorised ONLY when an anchored, product-scoped price sits
    at or under that product's era-correct MSRP ceiling.

Price is decisive. Seller identity annotates the decision and, for Target,
supplies a structural marketplace signal — but a third party charging MSRP is
charging the right number, and a "national distributor" charging $183 for a
$161.64 box is refused on the number regardless of what it calls itself.
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from raindance.core import classify as C
from raindance.core import leases as L
from raindance.core import msrp
from raindance.core import msrp_catalog as CAT
from raindance.core import price as P
from raindance.core import seller as S
from raindance.sites import get_handler


# --------------------------------------------------------------------------- #
# fixtures shaped like the real pages
# --------------------------------------------------------------------------- #
def ld(name: str, price) -> str:
    return ('<script type="application/ld+json">' + json.dumps(
        {"@type": "Product", "name": name,
         "offers": {"@type": "Offer", "price": str(price)}}) + "</script>")


def walmart(name: str, price, seller_html: str = "") -> str:
    payload = {"props": {"pageProps": {"initialData": {"data": {"product": {
        "name": name, "priceInfo": {"currentPrice": {"price": price}}}}}}}}
    return ('<script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(payload) + "</script>" + seller_html + " Add to cart")


# Verbatim from the live Target Plus PDP for Prismatic Evolutions ETB, $219.99.
TARGET_PLUS_MARKUP = (
    '<a aria-label="Sold &amp; shipped by Collectors Emporium. View partner details"'
    ' data-test="targetPlusExtraInfoSection" href="/sp/collectors-emporium/-/N-10026644">'
    '<span>Sold &amp; shipped by</span><span>Collectors Emporium</span></a>'
)

ETB = "Pokemon TCG: Scarlet & Violet-Surging Sparks Elite Trainer Box"
SWSH_ETB = "Pokemon TCG: Sword & Shield-Brilliant Stars Elite Trainer Box"


# --------------------------------------------------------------------------- #
# 1. price anchoring
# --------------------------------------------------------------------------- #
def test_unrelated_cheap_offer_cannot_price_the_listing():
    """The exact defect: a $9.00 protection plan pricing a $179.99 listing."""
    html = (ld("Pokemon Booster Box", 179.99)
            + '<div class="addon">{"price":9.00,"name":"Protection Plan"}</div>'
            + "<span>$179.99</span><span>$4.99</span>")
    r = P.product_price(html, product_name="Pokemon Booster Box")
    assert r["price"] == 179.99
    assert r["scoped"] is True


def test_unanchored_fallback_takes_the_maximum_not_the_first():
    """Failing high costs a missed buy; failing low buys the wrong thing."""
    r = P.product_price("<div>$9.00</div><div>$179.99</div><div>$4.99</div>")
    assert r["price"] == 179.99
    assert r["scoped"] is False


def test_cents_denominated_keys_are_converted():
    html = ('<script id="__NEXT_DATA__" type="application/json">'
            + json.dumps({"props": {"pageProps": {"initialData": {"data": {
                "product": {"priceInCents": 5999}}}}}}) + "</script>")
    assert P.product_price(html)["price"] == 59.99


def test_no_price_reports_none_rather_than_guessing():
    r = P.product_price("<html><body>nothing priced here</body></html>")
    assert r["price"] is None and r["scoped"] is False


# --------------------------------------------------------------------------- #
# 2. seller identity — evidence only
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("markup,expected,label", [
    (TARGET_PLUS_MARKUP, False, "live Target Plus PDP"),
    ('<a aria-label="Sold &amp; shipped by Collectors Emporium." href="/x"></a>',
     False, "seller named only in an attribute"),
    ('<a data-test="targetPlusExtraInfoSection" href="/sp/x/-/N-1"></a>',
     False, "structural marketplace marker, no name"),
    ('Sold and shipped by <a href="/plus/x">ScalperCo</a>', False, "name inside a tag"),
    ('<span>Sold and shipped by</span><span>ScalperCo</span>', False, "split across spans"),
    ('Sold and shipped by&nbsp;ScalperCo', False, "nbsp separator"),
    ('Sold and shipped by Targeted Deals', False, "lookalike prefix name"),
    ('<span>Sold &amp; shipped by</span><span>Target</span>', True, "genuine first-party"),
    ('<script>__TGT_DATA__={}</script><h1 data-test="product-title">x</h1>',
     None, "Target markers but NO seller statement"),
    ('Sold by Target<div>Sold by ScalperCo</div>', None, "conflicting statements"),
])
def test_target_seller_requires_positive_evidence(markup, expected, label):
    assert S.verdict(markup, "target")["is_official"] is expected, label


def test_observed_seller_name_is_never_overwritten():
    v = S.verdict(TARGET_PLUS_MARKUP, "target")
    assert v["seller"] == "Collectors Emporium"


@pytest.mark.parametrize("url,expected", [
    ("https://www.pokemoncenter.com/product/1", True),
    ("https://shop.pokemoncenter.com/x", True),
    ("https://pokemoncenter.com.evil.example/x", False),
    ("https://notpokemoncenter.com/x", False),
    ("https://www.pokemoncenter.store/x", False),
])
def test_pokemon_center_identity_is_the_domain(url, expected):
    assert S.official_host(url, "pokemoncenter.com") is expected


# --------------------------------------------------------------------------- #
# 3. MSRP judgement — real observed prices
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("title,price,buyable,note", [
    ("Pokemon TCG: Mega Evolution Chaos Rising Elite Trainer Box", 59.99, True,
     "Target first-party, exactly MSRP"),
    ("Pokemon TCG: Mega Evolution - Perfect Order Pokemon Center Elite Trainer Box",
     149.99, False, "observed Target Plus resale, 2.5x"),
    ("Pokemon TCG: Mega Evolution - Pitch Black Pokemon Center Elite Trainer Box",
     179.99, False, "observed Target Plus resale, 3.0x"),
    ("Pokemon TCG Scarlet & Violet Elite Trainer Box - Prismatic Evolutions",
     219.99, False, "observed Target Plus resale, 3.7x"),
    ("Pokemon TCG: 30th Celebration Booster Bundle (6 Packs)", 26.94, True,
     "Pokemon Center MSRP"),
    ("Pokemon TCG: Scarlet & Violet Terapagos ex Ultra-Premium Collection",
     119.99, True, "Pokemon Center MSRP"),
    ("Pokemon TCG: 30th Celebration Booster Box 36 packs", 183.00, False,
     "the distributor markup that used to pass"),
    ("Pokemon TCG: 30th Celebration Booster Box 36 packs", 161.64, True,
     "same box at MSRP"),
])
def test_price_verdict_matches_observed_reality(title, price, buyable, note):
    v = msrp.price_verdict(price, title)
    assert (v["at_msrp"] is True) is buyable, f"{note}: {v}"


def test_the_183_case_is_refused_and_the_old_ceiling_was_the_bug():
    v = msrp.price_verdict(183.00, "Pokemon TCG: 30th Celebration Booster Box")
    assert v["at_msrp"] is False
    assert v["threshold"] < 183.00
    # The old band was tolerance .15 + $12 fixed → a $197.89 ceiling.
    assert 161.64 * 1.15 + 12 > 183.00, "the old settings really did admit it"


def test_era_changes_the_ceiling():
    """A Sword & Shield ETB is $49.99; today's is $59.99. Both are on shelves."""
    assert msrp.price_verdict(49.99, SWSH_ETB)["at_msrp"] is True
    assert msrp.price_verdict(59.99, SWSH_ETB)["at_msrp"] is False
    assert msrp.price_verdict(59.99, ETB)["at_msrp"] is True


def test_unidentifiable_set_refuses_rather_than_guessing_an_era():
    v = msrp.price_verdict(59.99, "Pokemon Elite Trainer Box")
    assert v["era"] is None and v["band"] == "era_unknown" and v["at_msrp"] is None


def test_price_below_the_floor_is_a_misread_not_a_deal():
    v = msrp.price_verdict(9.00, ETB)
    assert v["at_msrp"] is None and v["band"] == "implausible"


def test_specific_tiers_are_matched_before_generic_ones():
    assert msrp.classify_sku("Terapagos ex Ultra-Premium Collection") == \
        "ultra_premium_collection"
    assert msrp.classify_sku("Mega Zygarde ex Premium Collection") == \
        "premium_collection"
    assert msrp.classify_sku("Surging Sparks Booster Bundle (6 Packs)") == \
        "booster_bundle"


def test_catalog_records_its_own_provenance():
    health = CAT.catalog_health()
    assert health["tiers"] >= 15
    assert health["by_confidence"].get("verified", 0) >= 8
    for entry in CAT.CATALOG:
        assert entry.get("source"), f"{entry['type']} has no source"
        assert entry["confidence"] in ("verified", "probable", "unverified")


# --------------------------------------------------------------------------- #
# 4. the gate itself
# --------------------------------------------------------------------------- #
def _gate(html, url, site, name, signals=None):
    return C.classify(html, url, {"name": name}, source="browser",
                      handler=get_handler(site), signals=signals)


def test_live_target_plus_scalper_listing_is_refused():
    """Prismatic Evolutions ETB at $219.99 against a $59.99 MSRP."""
    html = ("<script>__TGT_DATA__={}</script>" + ld(ETB, 219.99)
            + TARGET_PLUS_MARKUP + " Add to cart")
    v = _gate(html, "https://www.target.com/p/x/-/A-2", "target", ETB)
    assert v["status"] == C.IN_STOCK_RESELLER
    assert v["status"] != C.BUYABLE
    assert v["seller"] == "Collectors Emporium"


def test_third_party_at_msrp_is_buyable_because_price_decides():
    html = walmart(ETB, 59.99, "<div>Sold by MJ Holdings</div>")
    v = _gate(html, "https://www.walmart.com/ip/x/1", "walmart", ETB)
    assert v["status"] == C.BUYABLE
    assert v["is_official"] is False


def test_unanchored_price_alone_never_authorises_a_purchase():
    v = _gate("<div>Sold and shipped by Walmart.com</div> Add to cart $59.99",
              "https://www.walmart.com/ip/x/2", "walmart", ETB)
    assert v["status"] == C.IN_STOCK_UNVERIFIED
    assert v["price_scoped"] is False


def test_retailer_api_signals_are_treated_as_anchored():
    """On a live Target PDP the price is not in the HTML at all."""
    v = _gate("<html>no price anywhere</html>",
              "https://www.target.com/p/x/-/A-1", "target", ETB,
              signals={"price": 59.99, "stock": True,
                       "is_official": True, "seller": "Target"})
    assert v["status"] == C.BUYABLE
    assert v["price_scoped"] is True and v["price_source"] == "retailer-api"


def test_bot_wall_is_never_read_as_stock():
    v = _gate("Robot or human? px-captcha Add to cart $59.99",
              "https://www.target.com/p/x/-/A-1", "target", ETB)
    assert v["status"] == C.BLOCKED


def test_only_one_status_is_ever_buyable():
    assert C.BUYABLE == C.IN_STOCK_RETAIL
    for status in (C.IN_STOCK_RESELLER, C.IN_STOCK_OVER_MSRP,
                   C.IN_STOCK_UNVERIFIED, C.OUT_OF_STOCK, C.BLOCKED,
                   C.UNKNOWN, C.ERROR):
        assert status != C.BUYABLE


def test_gate_reports_the_evidence_behind_every_decision():
    """A refusal has to be auditable after the fact."""
    html = walmart(ETB, 219.99, "<div>Sold by ScalperCo</div>")
    v = _gate(html, "https://www.walmart.com/ip/x/3", "walmart", ETB)
    for key in ("price", "price_source", "price_scoped", "msrp", "threshold",
                "sku_type", "era", "tier_confidence", "seller", "is_official"):
        assert key in v, f"missing {key}"
    assert v["reason"]


# --------------------------------------------------------------------------- #
# 5. independence — one cart per profile
# --------------------------------------------------------------------------- #
def test_two_tasks_never_hold_the_same_profile():
    leases = L.ProfileLeases()
    assert leases.acquire("prof_a", "t1") is True
    assert leases.acquire("prof_a", "t2") is False
    assert leases.acquire("prof_b", "t2") is True


def test_distinct_profiles_run_fully_in_parallel():
    leases = L.ProfileLeases()
    guard, state = threading.Lock(), {"now": 0, "peak": 0}

    def work(i):
        with leases.hold(f"prof_{i}", f"task_{i}", timeout=5):
            with guard:
                state["now"] += 1
                state["peak"] = max(state["peak"], state["now"])
            threading.Event().wait(0.05)
            with guard:
                state["now"] -= 1

    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert state["peak"] == 8, "profiles must not block each other"


def test_tasks_sharing_a_profile_serialise_instead_of_sharing_a_cart():
    leases = L.ProfileLeases()
    guard, state = threading.Lock(), {"inside": 0, "overlaps": 0}

    def work(i):
        try:
            with leases.hold("shared", f"task_{i}", timeout=5):
                with guard:
                    state["inside"] += 1
                    if state["inside"] > 1:
                        state["overlaps"] += 1
                threading.Event().wait(0.03)
                with guard:
                    state["inside"] -= 1
        except L.LeaseBusy:
            pass

    threads = [threading.Thread(target=work, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert state["overlaps"] == 0


def test_a_non_holder_cannot_release_someone_elses_profile():
    leases = L.ProfileLeases()
    leases.acquire("p", "owner")
    leases.release("p", "impostor")
    assert leases.holder("p") == "owner"


def test_queue_rows_are_spread_across_profiles_not_piled_on_the_first():
    rows = [{"profile_id": ""} for _ in range(5)]
    got = L.assign_profiles(rows, ["p1", "p2", "p3"])
    assert len(set(got)) == 3


def test_an_explicit_profile_choice_is_respected_and_not_duplicated():
    rows = [{"profile_id": "p2"}, {"profile_id": ""}, {"profile_id": ""}]
    got = L.assign_profiles(rows, ["p1", "p2", "p3"])
    assert got[0] == "p2" and "p2" not in got[1:]


def test_runner_acquires_and_releases_the_lease():
    src = Path("raindance/tasks/runner.py").read_text()
    assert "LEASES.acquire(" in src, "runner does not take a profile lease"
    assert "LEASES.release(" in src, "runner does not release the profile lease"
    assert src.index("LEASES.acquire(") < src.index("create_context"), \
        "the lease must be held BEFORE a browser opens on the profile dir"


@pytest.mark.parametrize("title,tier", [
    ("Pokémon TCG: Terapagos ex Ultra-Premium Collection", "ultra_premium_collection"),
    ("Pokémon TCG: Crown Zenith Elite Trainer Box Plus", "elite_trainer_box_plus"),
    ("Pokémon TCG: 30th Celebration Pokémon Center Elite Trainer Box",
     "pokemon_center_elite_trainer_box"),
    ("Pokemon TCG: Mega Evolution Chaos Rising Elite Trainer Box", "elite_trainer_box"),
    ("Pokemon TCG: Mega Zygarde ex Premium Collection", "premium_collection"),
    ("Pokémon TCG: 30th Celebration Booster Bundle (6 Packs)", "booster_bundle"),
    ("Pokémon TCG: Trick or Trade BOOster Bundle", "trick_or_trade"),
    ("Pokemon TCG: Mega Evolution Ascended Heroes Mega Emboar ex Box", "ex_box"),
    ("Pokemon TCG: Surging Sparks Booster Display Box", "booster_box_36"),
    ("Pokémon TCG: 30th Celebration Mini Tins (10-Pack)", "mini_tin_10pack"),
    ("Pokemon TCG: White Flare 3-Booster Blister", "blister_3pack"),
    ("Pokemon TCG: Black Bolt Checklane Blister", "checklane_blister"),
])
def test_specific_tiers_win_over_generic_ones(title, tier):
    """Catalog order is load-bearing. 'Trick or Trade BOOster Bundle' contains
    'booster bundle', so a generic-first ordering priced a $14.99 product
    against a $26.94 ceiling and would have admitted it at nearly 2x."""
    assert msrp.classify_sku(title) == tier


# --------------------------------------------------------------------------- #
# 6. the first-party band — the false-negative side
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("title,price,note", [
    ("Pokemon TCG: Mega Evolution Ascended Heroes Booster Bundle (6 Packs)",
     31.99, "Target's own price, +18.7% over the $26.94 MSRP"),
    ("Pokemon TCG: Mega Evolution Phantasmal Flames Booster Bundle (6 Packs)",
     29.99, "Target's own price, +11.3%"),
    ("Pokemon TCG: 30th Celebration Elite Trainer Box", 69.99,
     "Target's own price, +16.7% over the $59.99 MSRP"),
])
def test_legitimate_first_party_markup_is_bought(title, price, note):
    """Target marks up its OWN stock 11-19%. Refusing that is a missed drop.

    This is the half of the gate that a single tight band would break, and the
    reason the band is chosen by seller identity.
    """
    v = msrp.price_verdict(price, title, first_party=True)
    assert v["at_msrp"] is True, f"{note}: {v}"
    assert v["band_used"] == "first_party"


@pytest.mark.parametrize("title,price", [
    ("Pokemon TCG: Mega Evolution Ascended Heroes Booster Bundle (6 Packs)", 31.99),
    ("Pokemon TCG: 30th Celebration Elite Trainer Box", 69.99),
])
def test_the_same_price_from_a_marketplace_seller_is_refused(title, price):
    """The identical number is fine from Target and not fine from a reseller —
    which is precisely what one shared band cannot express."""
    assert msrp.price_verdict(price, title, first_party=False)["at_msrp"] is False
    assert msrp.price_verdict(price, title, first_party=None)["at_msrp"] is False


def test_unconfirmed_seller_gets_the_tight_band_not_the_generous_one():
    """Absence of evidence must not buy the benefit of the doubt."""
    title = "Pokemon TCG: 30th Celebration Booster Box 36 packs"
    assert msrp.price_verdict(183.00, title, first_party=None)["band_used"] == "tight"
    assert msrp.price_verdict(183.00, title, first_party=None)["at_msrp"] is False


@pytest.mark.parametrize("name,retailer,expected", [
    ("Target", "target", True),
    ("Target.com", "target", True),
    ("Targeted Deals", "target", False),
    ("Target Resellers LLC", "target", False),
    ("Walmart.com", "walmart", True),
    ("Walmart Resellers LLC", "walmart", False),
    ("Best Buy", "bestbuy", True),
    ("BestBuy Deals Outlet", "bestbuy", False),
])
def test_first_party_names_match_exactly_never_by_prefix(name, retailer, expected):
    """`startswith("target")` admitted 'Targeted Deals'. Membership is exact."""
    assert S.is_first_party_name(name, retailer) is expected


# --------------------------------------------------------------------------- #
# 7. the money path — what the cart actually holds
# --------------------------------------------------------------------------- #
def test_dry_run_stops_before_the_cart_by_default():
    """A rehearsal must not leave a unit in a persistent cart.

    The old order was add_to_cart -> enter checkout -> fill the card -> return
    "DRY-RUN OK", on a persistent profile nothing ever clears. Each rehearsal
    and each retry added another unit that a later live run would pay for.
    """
    src = Path("raindance/tasks/runner.py").read_text()
    assert 'dry_run_depth", "gate"' in src, "the safe default is not 'gate'"
    gate_return = src.index('"dry_run_depth": "gate"')
    atc = src.index("handler.add_to_cart(")
    assert gate_return < atc, "the dry-run exit must come BEFORE add_to_cart"


def test_cart_is_cleared_before_adding_to_it():
    src = Path("raindance/tasks/runner.py").read_text()
    assert "handler.clear_cart(page)" in src
    assert src.index("handler.clear_cart(page)") < src.index("handler.add_to_cart(")


def test_cart_total_is_checked_against_the_approved_price():
    src = Path("raindance/tasks/runner.py").read_text()
    assert "read_cart_total(page)" in src
    assert src.index("read_cart_total(page)") < src.index("handler.checkout(")


def test_a_handler_that_cannot_clear_the_cart_says_so_loudly():
    """Silently assuming an empty cart is how the accumulation bug hid."""
    from raindance.sites.base import SiteHandler
    import inspect
    src = inspect.getsource(SiteHandler.clear_cart)
    assert "NotImplementedError" in src


def test_cheapest_product_node_cannot_win_over_the_real_one():
    """A PDP often carries several schema.org Product nodes — the item, plus
    accessories and 'frequently bought together' entries. Picking the cheapest
    scoped node is the same defect as picking the first loose dollar amount,
    just harder to see. Found by mutation testing: reverting the selection to
    min() left the rest of the suite green."""
    html = (ld("Pokemon TCG: Surging Sparks Elite Trainer Box", 59.99)
            + ld("Card Sleeves (65 Sleeves)", 7.99)
            + ld("Deck Box", 12.99) + " Add to cart")
    r = P.product_price(html, product_name="Pokemon TCG: Surging Sparks Elite Trainer Box")
    assert r["price"] == 59.99, r
    assert r["scoped"] is True


def test_with_no_usable_name_the_highest_scoped_node_wins():
    """Failing high costs a missed buy; failing low buys an accessory's price."""
    html = ld("Elite Trainer Box", 59.99) + ld("Sleeves", 7.99) + " Add to cart"
    assert P.product_price(html)["price"] == 59.99
    assert P.product_price(html, product_name="")["price"] == 59.99


def test_accessory_priced_listing_is_refused_end_to_end():
    """The whole point: a cheap sibling node must not authorise a purchase."""
    html = (ld("Pokemon TCG: Surging Sparks Elite Trainer Box", 59.99)
            + ld("Card Sleeves", 7.99)
            + "<div>Sold and shipped by Walmart.com</div> Add to cart")
    v = _gate(html, "https://www.walmart.com/ip/x/99", "walmart", ETB)
    assert v["price"] == 59.99
    assert v["status"] == C.BUYABLE


# --------------------------------------------------------------------------- #
# 8. bugs found by driving RainDance's own evasion stack against real,
#    live, non-Pokemon listings (Target/Walmart/Best Buy usb-cable/charger
#    PDPs, 2026-09-04) — not fixtures, actual page content.
# --------------------------------------------------------------------------- #
def test_stock_out_regex_ignores_boilerplate_inside_script_tags():
    """Real Best Buy page: a hydration <script> ships an add-to-cart error
    dialog's copy dictionary — `"item_not_sellable":{"title":"sold out"}}` —
    on every PDP regardless of the product's actual availability. Because
    GenericHandler checked that pattern with no provenance filtering, and
    Best Buy inherits detect_stock_signals unmodified, EVERY Best Buy
    listing read as out-of-stock via HTML scanning."""
    from raindance.sites.generic import GenericHandler

    html = (
        '<script>{"errors":{"item_not_sellable":{"title":"sold out"}}}</script>'
        '<button data-test="addToCart">Add to Cart</button>'
    )
    h = GenericHandler("generic")
    assert h.detect_stock_signals(html) is True


def test_stock_regex_ignores_differently_escaped_json_too():
    """A second false positive on the same real page: an unrelated
    rewards-program message template ("notify me about reward progress")
    sat in the same script block under JS-string escaping a lookbehind
    would not catch — proving script/style stripping, not pattern
    excluding, is the right fix."""
    from raindance.sites.generic import GenericHandler

    html = (
        r'<script>var x = "{\"notifymessage\":\"notify me about reward '
        r'progress and more\"}";</script>'
        '<button>Add to Cart</button>'
    )
    h = GenericHandler("generic")
    assert h.detect_stock_signals(html) is True


def test_a_real_out_of_stock_badge_in_the_dom_still_reads_as_out():
    from raindance.sites.generic import GenericHandler
    html = '<div class="status">Sold Out</div><script>{"unrelated":"add to cart"}</script>'
    h = GenericHandler("generic")
    assert h.detect_stock_signals(html) is False


def test_sold_by_label_does_not_reach_across_a_blank_run_for_a_value():
    """Real Best Buy page: a SECOND 'Sold by' fulfillment-info label had no
    adjacent value (JS-rendered elsewhere as a logo image, not text), so
    [\\s:]* skipped past ~40 blank lines from THAT second label and grabbed
    'Return & Exchange Policy' — an unrelated footer link — as if it were a
    seller name. The bug needs the second, empty 'sold by' to trigger; a
    single well-formed one was never the failure."""
    html = (
        "Sold by\n\nBest Buy\n\n"
        "Sold by"                      # second label, value JS-rendered blank
        + ("\n" * 40)
        + "Return & Exchange Policy"
    )
    names = S.sold_by_names(html)
    assert "Return & Exchange Policy" not in names
    assert "Best Buy" in names


def test_bestbuy_pdp_with_the_real_double_sold_by_structure_reads_official():
    """End-to-end: the exact structural shape of the live page — a genuine
    'Sold by Best Buy' plus a second blank 'Sold by' label far above an
    unrelated link — must resolve to a clean first-party verdict, not
    'ambiguous' from a phantom third-party name."""
    html = (
        "Add to cart Sold by\n\nBest Buy"
        + ("\n" * 40)
        + "Return & Exchange Policy More options"
    )
    v = S.verdict(html, "bestbuy")
    assert v["is_official"] is True
    assert v["seller"] == "Best Buy"


# --------------------------------------------------------------------------- #
# 9. cross-process profile guard — the gap ProfileLeases cannot see
# --------------------------------------------------------------------------- #
def test_no_lock_file_is_a_clean_pass():
    import tempfile
    from raindance.core.leases import CrossProcessGuard
    with tempfile.TemporaryDirectory() as d:
        assert CrossProcessGuard.check(d) is None


def test_our_own_prior_lock_never_blocks_us():
    import tempfile
    from raindance.core.leases import CrossProcessGuard
    with tempfile.TemporaryDirectory() as d:
        CrossProcessGuard.acquire(d)
        assert CrossProcessGuard.check(d) is None


def test_a_genuinely_live_other_process_is_refused():
    import json, os, subprocess, sys, tempfile, time
    from raindance.core.leases import CrossProcessGuard
    with tempfile.TemporaryDirectory() as d:
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
        try:
            time.sleep(0.3)
            with open(os.path.join(d, CrossProcessGuard.FILENAME), "w") as f:
                json.dump({"pid": proc.pid, "acquired": time.time()}, f)
            refusal = CrossProcessGuard.check(d)
            assert refusal is not None
            assert str(proc.pid) in refusal
        finally:
            proc.terminate()
            proc.wait()


def test_a_dead_process_lock_is_stale_not_blocking():
    import json, os, subprocess, sys, tempfile, time
    from raindance.core.leases import CrossProcessGuard
    with tempfile.TemporaryDirectory() as d:
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        with open(os.path.join(d, CrossProcessGuard.FILENAME), "w") as f:
            json.dump({"pid": proc.pid, "acquired": time.time()}, f)
        assert CrossProcessGuard.check(d) is None


def test_release_never_deletes_a_lock_it_does_not_own():
    import json, os, tempfile, time
    from raindance.core.leases import CrossProcessGuard
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, CrossProcessGuard.FILENAME)
        with open(path, "w") as f:
            json.dump({"pid": 999999999, "acquired": time.time()}, f)
        CrossProcessGuard.release(d)
        assert os.path.exists(path), "release() deleted a lock it did not write"


def test_a_corrupt_lock_file_fails_open():
    import os, tempfile
    from raindance.core.leases import CrossProcessGuard
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, CrossProcessGuard.FILENAME), "w") as f:
            f.write("{not valid json at all")
        assert CrossProcessGuard.check(d) is None


def test_guard_is_wired_into_the_persistent_launch_before_any_launch_call():
    src = Path("raindance/evasion/browser_factory.py").read_text()
    assert "CrossProcessGuard.check(user_data_dir)" in src
    assert "CrossProcessGuard.acquire(user_data_dir)" in src
    check_pos = src.index("CrossProcessGuard.check(user_data_dir)")
    launch_pos = src.index("launch_persistent_context(")
    assert check_pos < launch_pos, "the guard must run BEFORE the launch, not after"


def test_guard_release_is_wired_into_runner_cleanup():
    src = Path("raindance/tasks/runner.py").read_text()
    assert "CrossProcessGuard.release(user_data_dir)" in src


def test_a_refused_guard_actually_aborts_the_launch(monkeypatch):
    """Behavioral, not textual: mock the guard to refuse and confirm
    create_context raises BEFORE any Playwright call, rather than the
    refusal being silently swallowed somewhere in the call chain."""
    import tempfile
    from raindance.evasion.browser_factory import BrowserFactory, EvasionError
    from raindance.evasion.fingerprint_manager import FingerprintManager
    from raindance.core.leases import CrossProcessGuard

    monkeypatch.setattr(CrossProcessGuard, "check", classmethod(
        lambda cls, d: "profile busy by mock"))

    class ExplodingPlaywright:
        class chromium:
            @staticmethod
            def launch_persistent_context(*a, **kw):
                raise AssertionError("must not reach the real launch call")

    class Bus:
        def log(self, *a, **k):
            pass

    with tempfile.TemporaryDirectory() as d:
        factory = BrowserFactory(proxy_manager=None,
                                 fingerprint_manager=FingerprintManager(seed=1),
                                 evasion_enabled=False, bus=Bus())
        try:
            factory.create_context(ExplodingPlaywright(), account_id="x",
                                   headless=True, user_data_dir=d)
            assert False, "expected EvasionError"
        except EvasionError as e:
            assert "mock" in str(e)


# --------------------------------------------------------------------------- #
# 10. is_bot_wall — dormant boilerplate vs an actual active challenge
# --------------------------------------------------------------------------- #
def test_dormant_captcha_css_is_not_a_bot_wall():
    """Real Target page for 'Ascended Heroes — Booster Bundle': a dormant
    <style>#px-captcha-modal {...}</style> rule ships on EVERY Target PDP
    regardless of whether a challenge is showing, and made the gate report
    `blocked` on a page that had rendered completely normally — confirmed
    live, through the actual app, with a real 'Add to cart' button present."""
    html = ('<style>#px-captcha-modal { background-color: white !important; }'
            '</style><div>Add to cart</div>')
    assert C.is_bot_wall(html) is False


def test_an_actually_rendered_challenge_iframe_is_still_caught():
    """The fix must not become a blanket 'never blocked' — a genuinely
    active, full-screen challenge overlay (confirmed live: Target's
    anti-bot system fired for real on a later run of the same URL) has to
    keep reading as blocked."""
    html = ('<iframe id="px-captcha-modal" style="display: block; position: fixed; '
            'top: 0; left: 0; width: 100%; height: 100%; z-index: 2147483647;">'
            '</iframe><div>Add to cart</div>')
    assert C.is_bot_wall(html) is True


def test_bot_wall_text_inside_a_script_tag_does_not_count():
    html = '<script>var cfg = {"vendor": "perimeterx"};</script><div>Add to cart</div>'
    assert C.is_bot_wall(html) is False


def test_bot_wall_text_in_real_page_text_still_counts():
    html = '<div>Please verify you are a human before continuing.</div>'
    assert C.is_bot_wall(html) is True


# --------------------------------------------------------------------------- #
# 11. catalog store id -> handler registry key
#
# Confirmed live, through the actual app: a task created via the product
# wizard for Target carried site="store_target" (the catalog's own id) all
# the way into the runner. get_handler("store_target", ...) does not match
# the registry key "target", so it silently fell back to GenericHandler —
# no Target-specific seller/queue handling, and retailer_config.resolve()
# fell back to the "generic" config block too. Same bug, same shape, for
# every seeded store (Walmart, Best Buy, Pokémon Center).
# --------------------------------------------------------------------------- #
import raindance.core.catalog as CATALOG
from raindance.sites import get_handler as _get_handler
from raindance.sites.target import TargetHandler
from raindance.sites.walmart import WalmartHandler
from raindance.sites.bestbuy import BestBuyHandler
from raindance.sites.pokemon_center import PokemonCenterHandler
from raindance.sites.generic import GenericHandler


@pytest.mark.parametrize("store_id,handler_key", [
    ("store_target", "target"),
    ("store_walmart", "walmart"),
    ("store_best_buy", "bestbuy"),
    ("store_pokemon_center", "pokemon_center"),
])
def test_every_seeded_store_id_maps_to_its_handler_key(store_id, handler_key):
    assert CATALOG.handler_for_store(store_id) == handler_key


def test_a_custom_store_id_passes_through_to_generic():
    """A user-added Shopify/Wix store has no seeded handler — GenericHandler
    is the correct, intended outcome, not a gap to fix."""
    custom = "store_my_shop_abc123"
    assert CATALOG.handler_for_store(custom) == custom
    assert get_handler(CATALOG.handler_for_store(custom)).__class__ is GenericHandler


@pytest.mark.parametrize("store_id,expected_cls", [
    ("store_target", TargetHandler),
    ("store_walmart", WalmartHandler),
    ("store_best_buy", BestBuyHandler),
    ("store_pokemon_center", PokemonCenterHandler),
])
def test_handler_dispatch_reaches_the_real_retailer_handler_not_generic(store_id, expected_cls):
    """This is the actual observable consequence: get_handler() on the
    TRANSLATED key must land on the retailer-specific class, not
    GenericHandler — Target's seller detection, Walmart's __NEXT_DATA__
    parsing, none of it runs otherwise."""
    resolved = CATALOG.handler_for_store(store_id)
    handler = _get_handler(resolved)
    assert isinstance(handler, expected_cls)
    assert not (type(handler) is GenericHandler)


def test_execute_queue_translates_store_id_before_using_it_as_site():
    src = Path("raindance/core/execute_queue.py").read_text()
    assert "CAT.handler_for_store(store_id)" in src  # execute_queue.py aliases it as CAT internally
    assign_pos = src.index("site = CAT.handler_for_store(store_id)")
    add_pos = src.index("task_store.add(")
    assert assign_pos < add_pos, "translation must happen before the task is built"


def test_execute_queue_still_preserves_the_raw_store_id_field():
    """The catalog id must still be stored somewhere for catalog-linking —
    only the handler-dispatch `site` field needed the translation."""
    src = Path("raindance/core/execute_queue.py").read_text()
    assert 'store_id=p.get("store_id", "")' in src


# --------------------------------------------------------------------------- #
# 12. captcha detection and enforcement — watched live, through the real app:
# a fully visible PerimeterX "Press & hold" challenge sat on a Target PDP
# from page load onward, yet the task reported "reached the final step"
# within ~30 seconds — far short of even one captcha timeout. Two distinct,
# compounding bugs, both confirmed against real captured challenge markup.
# --------------------------------------------------------------------------- #
from raindance.captcha.service import CaptchaService

REAL_PX_CHALLENGE_HTML = '''
<html><body>
<div class="ReactModalPortal"></div>
<iframe id="px-captcha-modal" style="display: block; position: fixed; top: 0;
 left: 0; width: 100%; height: 100%; border: none; z-index: 2147483647;"
 src="https://www.target.com/px-captcha"></iframe>
</body></html>
'''


def _svc():
    return CaptchaService({}, bus=None)


def test_real_target_challenge_markup_is_detected():
    """The exact bug: id="px-captcha-modal" does not match the CSS id
    selector "#px-captcha" — id selectors require an exact match, not a
    prefix. Confirmed live: is_present() returned False while the challenge
    was fully visible on screen."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(REAL_PX_CHALLENGE_HTML)
        try:
            assert _svc().is_present(page) is True
        finally:
            browser.close()


def test_a_clean_page_with_no_challenge_reads_as_absent():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content("<html><body><button>Add to cart</button></body></html>")
        try:
            assert _svc().is_present(page) is False
        finally:
            browser.close()


@pytest.mark.parametrize("filename,pattern", [
    ("raindance/sites/generic.py", "add_to_cart"),
    ("raindance/sites/pokemon_center.py", "add_to_cart"),
])
def test_add_to_cart_checks_the_captcha_result_at_every_call_site(filename, pattern):
    """A discarded return value here is the second half of the live bug:
    even with detection fixed, a captcha that never clears must stop the
    run rather than let add_to_cart() return True regardless."""
    src = Path(filename).read_text()
    start = src.index(f"def {pattern}(")
    end = src.index("\n    def ", start + 10)
    body = src[start:end]
    bare_calls = body.count("self._captcha(page, task, should_stop, on_status)")
    guarded_calls = body.count("if not self._captcha(page, task, should_stop, on_status):")
    assert bare_calls == guarded_calls, (
        f"{filename}::{pattern} has {bare_calls} captcha calls but only "
        f"{guarded_calls} check the result")
    assert guarded_calls >= 2, f"{filename}::{pattern} should guard both ATC captcha checks"


@pytest.mark.parametrize("filename", [
    "raindance/sites/generic.py",
    "raindance/sites/pokemon_center.py",
])
def test_checkout_checks_the_captcha_result(filename):
    src = Path(filename).read_text()
    start = src.index("def checkout(")
    end = src.index("\n    def ", start + 10)
    body = src[start:end]
    bare_calls = body.count("self._captcha(page, task, should_stop, on_status)")
    guarded_calls = body.count("if not self._captcha(page, task, should_stop, on_status):")
    assert bare_calls == guarded_calls, (
        f"{filename}::checkout has {bare_calls} captcha calls but only "
        f"{guarded_calls} check the result")
    assert guarded_calls >= 1


def test_no_bare_captcha_call_anywhere_in_a_site_handler():
    """Comprehensive sweep: every self._captcha(...) call across every site
    handler must have its result checked. A future handler that adds a bare
    call reintroduces exactly this bug."""
    import re
    for path in Path("raindance/sites").glob("*.py"):
        src = path.read_text()
        for m in re.finditer(r'^(\s*)self\._captcha\(page, task, should_stop, on_status\)$',
                             src, re.M):
            line_start = m.start(1)
            preceding = src[max(0, line_start - 12):line_start]
            assert "if not " in preceding, (
                f"{path}: bare, unchecked captcha call at "
                f"{src[:m.start()].count(chr(10)) + 1}")
