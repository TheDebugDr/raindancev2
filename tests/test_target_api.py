"""Tests for Target's product-data path — TCIN parsing, the capture() context
manager, and merging signals across multiple captured responses.

The module shape below (`modules[].module_data.data.product...`) is not
invented — it is the real shape captured live from
`www.target.com/cdui_orchestrations/v1/pages/pdp/deferred_enrichment/modules`
on 2026-09-04, verified against both a first-party in-stock ETB and a known
Target Plus third-party listing. No network here: everything is replayed
from that captured shape.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from raindance.core import classify as C
from raindance.sites import get_handler, target_api
from raindance.tasks import models as M

ETB = "Pokemon TCG: Mega Evolution Chaos Rising Elite Trainer Box"


def price_module(retail: float) -> dict:
    """Shape of a ProductDetail*/price-bearing module, as actually captured."""
    return {"module_type": "ProductDetailWebDatasourceWithStore", "module_data": {
        "data": {"product": {"price": {
            "formatted_current_price": f"${retail}",
            "current_retail": retail, "reg_retail": retail}}}}}


def fulfillment_module(status: str) -> dict:
    """Shape of the fulfillment module, as actually captured."""
    return {"module_type": "ProductDetailWebDatasourceFulfillmentAndVariations",
            "module_data": {"data": {"product": {"fulfillment": {
                "shipping_options": {"availability_status": status}}}}}}


def response(*modules) -> dict:
    return {"modules": list(modules), "extensions": {}}


class FakePage:
    """Minimal stand-in for a Playwright Page: on()/remove_listener() only."""

    def __init__(self):
        self._handlers = []

    def on(self, event, handler):
        if event == "response":
            self._handlers.append(handler)

    def remove_listener(self, event, handler):
        if event == "response" and handler in self._handlers:
            self._handlers.remove(handler)

    def fire(self, resp):
        for h in list(self._handlers):
            h(resp)


class FakeResponse:
    def __init__(self, url, body):
        self.url = url
        self._body = body

    def json(self):
        return self._body


TARGET_URL = ("https://www.target.com/cdui_orchestrations/v1/pages/pdp/"
              "deferred_enrichment/modules?tcin=95267143")


# --------------------------------------------------------------------------- #
# tcin parsing
# --------------------------------------------------------------------------- #
def test_tcin_from_a_real_target_url():
    assert target_api.tcin_from_url(
        "https://www.target.com/p/pok-mon-.../-/A-95120834") == "95120834"


def test_tcin_from_a_non_target_url_is_none():
    assert target_api.tcin_from_url("https://www.walmart.com/ip/x/12345") is None


def test_is_configured_is_always_true_no_key_needed():
    """This path needs no key, no signup — it only listens to a request the
    page makes on its own. A caller that still gates on is_configured must
    not be silently skipped."""
    assert target_api.is_configured() is True
    assert target_api.is_configured(settings=None, site="target") is True


# --------------------------------------------------------------------------- #
# capture() — the response listener
# --------------------------------------------------------------------------- #
def test_capture_collects_only_matching_responses():
    page = FakePage()
    with target_api.capture(page) as captured:
        page.fire(FakeResponse("https://www.target.com/some/other/api", {"x": 1}))
        page.fire(FakeResponse(TARGET_URL, response(price_module(59.99))))
    assert len(captured) == 1
    assert captured[0]["modules"][0]["module_data"]["data"]["product"]["price"][
        "current_retail"] == 59.99


def test_capture_ignores_a_response_that_is_not_json():
    page = FakePage()

    class Unparseable:
        url = TARGET_URL
        def json(self):
            raise ValueError("not json")

    with target_api.capture(page) as captured:
        page.fire(Unparseable())
    assert captured == []


def test_capture_stops_listening_after_the_block_exits():
    page = FakePage()
    with target_api.capture(page) as captured:
        pass
    page.fire(FakeResponse(TARGET_URL, response(price_module(59.99))))
    assert captured == [], "a response after the block exited must not be added"


# --------------------------------------------------------------------------- #
# signals_from_captures — merging across multiple responses
# --------------------------------------------------------------------------- #
def test_price_and_stock_come_from_different_responses_and_still_merge():
    """Verified live: Target fires SEVERAL requests per PDP load, each
    carrying a different subset of modules — price in one, fulfillment in
    another. A single response is not guaranteed to carry both."""
    captured = [
        response(price_module(59.99)),
        response(fulfillment_module("IN_STOCK")),
    ]
    sig = target_api.signals_from_captures(captured)
    assert sig == {"price": 59.99, "stock": True}


def test_out_of_stock_status_maps_to_false():
    sig = target_api.signals_from_captures([response(fulfillment_module("OUT_OF_STOCK"))])
    assert sig["stock"] is False


def test_unknown_availability_status_is_left_out_not_guessed():
    sig = target_api.signals_from_captures([response(fulfillment_module("SOME_NEW_CODE"))])
    assert "stock" not in sig


def test_no_captures_at_all_returns_empty_dict():
    assert target_api.signals_from_captures([]) == {}
    assert target_api.signals_from_captures(None) == {}


def test_seller_and_is_official_are_never_present():
    """Checked directly against a real Target Plus listing: the seller name
    does not appear anywhere in these payloads. classify() already falls
    through to HTML-based seller detection when these keys are absent —
    this module must never invent them."""
    sig = target_api.signals_from_captures([
        response(price_module(219.99), fulfillment_module("IN_STOCK"))])
    assert "seller" not in sig
    assert "is_official" not in sig


def test_a_malformed_module_is_skipped_not_fatal():
    captured = [
        {"modules": [{"module_type": "Junk", "module_data": "not a dict"}]},
        response(price_module(59.99)),
    ]
    sig = target_api.signals_from_captures(captured)
    assert sig["price"] == 59.99


# --------------------------------------------------------------------------- #
# these signals run through the SAME gate as HTML-sourced ones
# --------------------------------------------------------------------------- #
def _verdict(signals, name=ETB):
    return C.classify("", "https://www.target.com/p/x/-/A-95267143",
                      {"name": name}, source="target-api",
                      handler=get_handler("target"), signals=signals)


def test_captured_price_at_msrp_in_stock_is_buyable():
    sig = target_api.signals_from_captures([
        response(price_module(59.99), fulfillment_module("IN_STOCK"))])
    assert _verdict(sig)["status"] == C.IN_STOCK_RETAIL


def test_captured_price_over_msrp_is_not_buyable():
    sig = target_api.signals_from_captures([
        response(price_module(219.99), fulfillment_module("IN_STOCK"))])
    v = _verdict(sig)
    assert v["status"] != C.IN_STOCK_RETAIL


def test_captured_out_of_stock_reports_out_of_stock():
    sig = target_api.signals_from_captures([
        response(price_module(59.99), fulfillment_module("OUT_OF_STOCK"))])
    assert _verdict(sig)["status"] == C.OUT_OF_STOCK


def test_a_captured_price_counts_as_anchored():
    """The whole point of this module: a price from Target's own structured
    session data must not be refused as 'unanchored page text' the way a
    bare HTML text-scan is."""
    sig = target_api.signals_from_captures([response(price_module(59.99))])
    v = _verdict(sig)
    assert v["price_source"] == "retailer-api"
    assert v["price_scoped"] is True


# --------------------------------------------------------------------------- #
# priority normalisation (unrelated, carried over from the old file)
# --------------------------------------------------------------------------- #
def test_task_priority_defaults_to_normal():
    assert M.normalize_task({"url": "x"})["priority"] == "normal"


def test_task_priority_high_is_kept():
    assert M.normalize_task({"url": "x", "priority": "high"})["priority"] == "high"


def test_bad_priority_falls_back_to_normal():
    assert M.normalize_task({"url": "x", "priority": "urgent"})["priority"] == "normal"


# --------------------------------------------------------------------------- #
# runner wiring — capture must wrap the SAME navigation, not a second one
# --------------------------------------------------------------------------- #
def test_runner_wraps_navigate_in_the_capture_context_not_a_separate_fetch():
    src = Path("raindance/tasks/runner.py").read_text()
    assert "target_api.capture(page)" in src
    assert "target_api.signals_from_captures(target_captures)" in src
    with_pos = src.index("target_api.capture(page)")
    nav_pos = src.index("handler.navigate(page, task)", with_pos)
    assert with_pos < nav_pos, "capture() must wrap the navigate() call"
