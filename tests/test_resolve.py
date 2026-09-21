"""Search-results → product-page resolution.

The gate refuses a search page for the right reason (no single price, seller
or buy box), so the resolver must earn the product page: retailer domain only,
dedicated PDP URL shapes, the result must match the selected set AND product
line, zero matches refuse, ambiguity refuses, and the landed page is
re-validated before anything downstream sees it.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from raindance.core import resolve as R

SET = "Surging Sparks"
LINE = "Elite Trainer Box"

TARGET_SEARCH = ("https://www.target.com/s?searchTerm="
                 "Surging+Sparks+Elite+Trainer+Box")
TARGET_PDP = ("https://www.target.com/p/pokemon-tcg-scarlet-violet-surging-"
              "sparks-elite-trainer-box/-/A-12345678")
TARGET_PDP_BUNDLE = ("https://www.target.com/p/pokemon-tcg-scarlet-violet-"
                     "surging-sparks-booster-bundle/-/A-87654321")


def _results_page(*links):
    body = "".join(
        f'<a href="{href}">{text}</a>' for href, text in links)
    return f"<html><body><div class='results'>{body}</div></body></html>"


class FakePage:
    """Playwright-ish page backed by a url → html map."""
    def __init__(self, pages):
        self._pages = pages
        self.url = ""
        self.visited = []

    def goto(self, url, wait_until=None):
        if url not in self._pages:
            raise RuntimeError(f"no such page in fixture: {url}")
        self.url = url
        self.visited.append(url)

    def content(self):
        return self._pages[self.url]["html"]

    def title(self):
        return self._pages[self.url]["title"]

    def wait_for_timeout(self, ms):
        pass


# --------------------------------------------------------------------------- #
# url classification
# --------------------------------------------------------------------------- #
def test_search_urls_are_detected():
    assert R.is_search_url(TARGET_SEARCH) is True
    assert R.is_search_url("https://www.walmart.com/search?q=surging+sparks") is True
    assert R.is_search_url("https://www.bestbuy.com/site/searchpage.jsp?st=pokemon") is True
    assert R.is_search_url("https://www.pokemoncenter.com/search/surging-sparks") is True


def test_product_pages_are_not_search_urls():
    assert R.is_search_url(TARGET_PDP) is False
    assert R.is_search_url("https://www.walmart.com/ip/12345678") is False
    assert R.is_search_url("") is False


def test_pdp_shapes_are_recognised_per_retailer():
    assert R.is_pdp_url(TARGET_PDP, "target") is True
    assert R.is_pdp_url("https://www.walmart.com/ip/pokemon-tcg/12345678", "walmart") is True
    assert R.is_pdp_url("https://www.bestbuy.com/site/pokemon-tcg/J123sku?skuId=1", "bestbuy") is True
    assert R.is_pdp_url("https://www.pokemoncenter.com/product/123", "pokemon_center") is True


def test_lookalike_domains_never_qualify():
    evil = TARGET_PDP.replace("www.target.com", "www.target.com.evil.example")
    assert R.is_pdp_url(evil, "target") is False
    assert R.site_for_url(evil) == ""


def test_cross_retailer_urls_do_not_qualify():
    assert R.is_pdp_url(TARGET_PDP, "walmart") is False


def test_bestbuy_search_pages_are_not_pdp_urls():
    assert R.is_pdp_url("https://www.bestbuy.com/site/searchpage.jsp?st=pokemon",
                        "bestbuy") is False


# --------------------------------------------------------------------------- #
# candidate extraction
# --------------------------------------------------------------------------- #
def test_only_pdp_links_on_the_retailer_domain_are_candidates():
    html = _results_page(
        (TARGET_PDP, "Pokemon TCG Scarlet & Violet Surging Sparks Elite Trainer Box"),
        (TARGET_PDP_BUNDLE, "Pokemon TCG Scarlet & Violet Surging Sparks Booster Bundle"),
        ("https://www.target.com/s?searchTerm=pokemon", "next page"),
        ("https://evil.example/p/x/-/A-1", "Pokemon TCG Scarlet & Violet Surging Sparks Elite Trainer Box"),
    )
    cands = R.extract_candidates(html, "target", TARGET_SEARCH)
    urls = [c["url"] for c in cands]
    assert TARGET_PDP in urls and TARGET_PDP_BUNDLE in urls
    assert len(urls) == 2


# --------------------------------------------------------------------------- #
# picking the result
# --------------------------------------------------------------------------- #
def test_the_exact_product_line_wins():
    cands = [
        {"url": TARGET_PDP, "text": "Pokemon TCG Scarlet & Violet Surging Sparks Elite Trainer Box"},
        {"url": TARGET_PDP_BUNDLE, "text": "Pokemon TCG Scarlet & Violet Surging Sparks Booster Bundle"},
    ]
    pick, reason = R.pick_result(cands, set_name=SET, line_name=LINE)
    assert pick["url"] == TARGET_PDP and reason == ""


def test_a_wrong_product_line_is_rejected():
    cands = [
        {"url": TARGET_PDP_BUNDLE, "text": "Pokemon TCG Scarlet & Violet Surging Sparks Booster Bundle"},
    ]
    pick, reason = R.pick_result(cands, set_name=SET, line_name=LINE)
    assert pick is None and "no search result matched" in reason


def test_a_wrong_set_is_rejected():
    wrong_set_pdp = ("https://www.target.com/p/pokemon-tcg-scarlet-violet-"
                     "prismatic-evolutions-elite-trainer-box/-/A-99999999")
    cands = [
        {"url": wrong_set_pdp, "text": "Pokemon TCG Scarlet & Violet Prismatic Evolutions Elite Trainer Box"},
    ]
    pick, reason = R.pick_result(cands, set_name=SET, line_name=LINE)
    assert pick is None


def test_zero_candidates_refuses():
    pick, reason = R.pick_result([], set_name=SET, line_name=LINE)
    assert pick is None and "no search result matched" in reason


def test_an_ambiguous_tie_refuses():
    cands = [
        {"url": TARGET_PDP, "text": "Pokemon TCG Scarlet & Violet Surging Sparks Elite Trainer Box"},
        {"url": TARGET_PDP + "?x=2", "text": "Pokemon TCG Scarlet & Violet Surging Sparks Elite Trainer Box"},
    ]
    pick, reason = R.pick_result(cands, set_name=SET, line_name=LINE)
    assert pick is None and "ambiguous" in reason


# --------------------------------------------------------------------------- #
# landing validation
# --------------------------------------------------------------------------- #
def test_a_matching_pdp_validates():
    html = ("<html><head><title>Pokemon TCG: Scarlet & Violet-Surging Sparks "
            "Elite Trainer Box : Target</title></head><body></body></html>")
    ok, why = R.validate_pdp(TARGET_PDP, html, site="target",
                             set_name=SET, line_name=LINE)
    assert ok and why == ""


def test_a_mismatched_title_fails_validation():
    html = ("<html><head><title>Pokemon TCG: Scarlet & Violet-Surging Sparks "
            "Booster Bundle : Target</title></head><body></body></html>")
    ok, why = R.validate_pdp(TARGET_PDP, html, site="target",
                             set_name=SET, line_name=LINE)
    assert not ok and "wrong product" in why


def test_a_non_pdp_landing_url_fails_validation():
    ok, why = R.validate_pdp(TARGET_SEARCH, "<html></html>", site="target",
                             set_name=SET, line_name=LINE)
    assert not ok and "not a target product page" in why


# --------------------------------------------------------------------------- #
# end to end through a fake page
# --------------------------------------------------------------------------- #
def _pdp_html(title):
    return f"<html><head><title>{title}</title></head><body>Add to cart</body></html>"


def test_happy_path_lands_on_the_validated_pdp():
    pdp_title = "Pokemon TCG: Scarlet & Violet-Surging Sparks Elite Trainer Box : Target"
    pages = {
        TARGET_SEARCH: {"html": _results_page(
            (TARGET_PDP, "Pokemon TCG Scarlet & Violet Surging Sparks Elite Trainer Box"),
            (TARGET_PDP_BUNDLE, "Pokemon TCG Scarlet & Violet Surging Sparks Booster Bundle"),
        ), "title": "search"},
        TARGET_PDP: {"html": _pdp_html(pdp_title), "title": pdp_title},
    }
    page = FakePage(pages)
    res = R.resolve_search_to_pdp(page, TARGET_SEARCH, site="target",
                                  set_name=SET, line_name=LINE)
    assert res["ok"] is True
    assert res["url"] == TARGET_PDP
    assert page.url == TARGET_PDP  # left ON the product page
    assert TARGET_SEARCH in page.visited


def test_no_match_leaves_a_clear_refusal():
    pages = {
        TARGET_SEARCH: {"html": _results_page(
            (TARGET_PDP_BUNDLE, "Pokemon TCG Scarlet & Violet Surging Sparks Booster Bundle"),
        ), "title": "search"},
    }
    page = FakePage(pages)
    res = R.resolve_search_to_pdp(page, TARGET_SEARCH, site="target",
                                  set_name=SET, line_name=LINE)
    assert res["ok"] is False
    assert "no search result matched" in res["reason"]


def test_a_non_search_url_is_rejected_without_navigation():
    page = FakePage({})
    res = R.resolve_search_to_pdp(page, TARGET_PDP, site="target",
                                  set_name=SET, line_name=LINE)
    assert res["ok"] is False
    assert page.visited == []


def test_an_unknown_site_is_rejected():
    page = FakePage({})
    res = R.resolve_search_to_pdp(
        page, "https://shop.example.com/search?q=pokemon", site="custom_shop",
        set_name=SET, line_name=LINE)
    assert res["ok"] is False
