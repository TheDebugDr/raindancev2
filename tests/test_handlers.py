"""Handler contract tests — every registered site handler against a stub page.

No browser, no network: the stub `page` records which selectors are queried,
clicked and filled and returns stub elements. Run:
    python3 -m pytest tests/test_handlers.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from raindance.core import retailer_config as rc            # noqa: E402
from raindance.sites import (                                # noqa: E402
    BUILTIN_SITES, get_handler, handler_capabilities, list_sites,
    register_handler, unregister_handler,
)
from raindance.sites import frames as frames_mod             # noqa: E402
from raindance.sites.user_store import handler_for_store     # noqa: E402


# --------------------------------------------------------------------------- #
# stub page
# --------------------------------------------------------------------------- #
class StubLocator:
    def __init__(self, frame, selector):
        self.frame, self.selector = frame, selector
        self.page = frame.page

    @property
    def first(self):
        return self

    def _present(self):
        return self.selector in self.frame.elements

    def count(self):
        return 1 if self._present() else 0

    def is_visible(self):
        if not self._present():
            return False
        rule = self.page.visible_until.get(self.selector)
        return True if rule is None else self.page.tick < rule

    def is_enabled(self):
        return self._present()

    def click(self, timeout=None):
        self.page.clicks.append((self.frame.name, self.selector))

    def fill(self, value, timeout=None):
        self.page.fills.append((self.frame.name, self.selector, value))

    def select_option(self, *a, **k):
        self.page.fills.append((self.frame.name, self.selector, a or k))

    def evaluate(self, js):
        return "SELECT" if self.selector.startswith("select") else "INPUT"


class StubFrame:
    def __init__(self, page, name, url, elements):
        self.page, self.name, self.url = page, name, url
        self.elements = set(elements or ())

    def locator(self, selector):
        self.page.queries.append((self.name, selector))
        return StubLocator(self, selector)


class StubPage(StubFrame):
    """Main frame + optional child frames. `visible_until` maps a selector to
    the tick after which it stops being visible; `wait_for_timeout` ticks."""

    def __init__(self, elements=(), *, child_frames=(), html="", visible_until=None):
        super().__init__(self, "", "https://example.test/p", elements)
        self.children = [StubFrame(self, n, u, els) for n, u, els in child_frames]
        self.html = html
        self.visible_until = dict(visible_until or {})
        self.tick = 0
        self.queries, self.clicks, self.fills, self.gotos = [], [], [], []

    @property
    def frames(self):
        return [self] + self.children

    def goto(self, url, **kw):
        self.gotos.append(url)

    def wait_for_timeout(self, ms):
        self.tick += 1

    def content(self):
        return self.html

    def screenshot(self, **kw):
        raise RuntimeError("no screenshots in tests")


PROFILE = {
    "email": "buyer@example.test", "phone": "5551234",
    "shipping": {"first_name": "Ash", "last_name": "K", "address1": "1 Pallet",
                 "address2": "", "city": "Pallet", "state": "KS", "zip": "66000"},
    "billing": {"same_as_shipping": True},
    "payment": {"card_name": "Ash K", "card_number": "4111111111111111",
                "expiry": "12/30", "cvc": "123"},
}

CONFIRMED_HTML = "<h1>Thank you!</h1><p>Order number: 123456789012</p>"


def _task(site, **over):
    t = {"id": "t1", "site": site, "url": "https://example.test/p", "quantity": 1}
    t.update(over)
    return t


def _defaults(site):
    return rc.default_site_params_for(site)


def _dedupe(items):
    """Collapse consecutive repeats (a miss is probed once per frame, then the
    fallback locator is built on the main document)."""
    out = []
    for x in items:
        if not out or out[-1] != x:
            out.append(x)
    return out


@pytest.fixture(scope="module")
def user_store_site():
    """One user-owned store registered for the duration of the module."""
    sid = "store_test"
    register_handler(sid, handler_for_store(
        {"id": sid, "name": "Test Store", "platform": "custom",
         "base_url": "https://www.teststore.test"}))
    yield sid
    unregister_handler(sid)


def _all_sites(user_store_site):
    return list_sites()


# --------------------------------------------------------------------------- #
# helpers / capability surface
# --------------------------------------------------------------------------- #
def test_registry_and_helpers_present(user_store_site):
    assert set(BUILTIN_SITES) <= set(list_sites())
    assert user_store_site in list_sites()
    for site in list_sites():
        caps = handler_capabilities(site)
        for key in ("custom_queue", "custom_atc", "custom_checkout", "frame_aware", "honors"):
            assert key in caps, (site, key)
        assert isinstance(caps["honors"], list) and "selectors.atc" in caps["honors"]
        d = _defaults(site)
        for key in ("selectors", "fields", "fields_only", "frames", "waits", "confirmation"):
            assert key in d, (site, key)
        for key in ("atc", "checkout", "place_order", "queue", "confirmation"):
            assert isinstance(d["selectors"][key], list), (site, key)
        assert d["selectors"]["atc"] and d["selectors"]["place_order"], site
        assert set(d["waits"]) >= {"atc_enable_s", "queue_max_s", "confirm_s"}
        assert "order_id_regex" in d["confirmation"]
    assert handler_capabilities("pokemon_center")["custom_queue"] is True
    assert handler_capabilities("generic")["custom_queue"] is False
    assert "selectors.guest" in handler_capabilities("pokemon_center")["honors"]


def test_defaults_are_copies_and_effective_merges():
    a = _defaults("target")
    a["selectors"]["atc"].append("#mutated")
    assert "#mutated" not in _defaults("target")["selectors"]["atc"]
    h = get_handler("target")
    merged = h.effective_params(_task("target", site_params={
        "waits": {"confirm_s": 3}, "selectors": {"atc": "#only"}}))
    assert merged["waits"]["confirm_s"] == 3
    assert merged["waits"]["queue_max_s"] == _defaults("target")["waits"]["queue_max_s"]
    assert h.selectors(_task("target", site_params={"selectors": {"atc": "#only"}}), "atc") == ["#only"]
    assert h.selectors(_task("target"), "checkout") == _defaults("target")["selectors"]["checkout"]


# --------------------------------------------------------------------------- #
# add to cart
# --------------------------------------------------------------------------- #
def test_add_to_cart_queries_defaults_in_order_and_clicks(user_store_site):
    for site in list_sites():
        atc = _defaults(site)["selectors"]["atc"]
        target = atc[1] if len(atc) > 1 else atc[0]
        page = StubPage([target])
        h = get_handler(site)
        assert h.add_to_cart(page, _task(site)) is True, site
        queried = _dedupe(s for _, s in page.queries)
        assert queried[:atc.index(target) + 1] == atc[:atc.index(target) + 1], site
        assert page.clicks == [("", target)], site


def test_add_to_cart_missing_button_fails_cleanly(user_store_site):
    for site in list_sites():
        page = StubPage([])
        h = get_handler(site)
        task = _task(site, site_params={"waits": {"atc_enable_s": 0}})
        assert h.add_to_cart(page, task) is False, site
        assert page.clicks == [], site


def test_atc_override_changes_what_is_queried(user_store_site):
    for site in list_sites():
        page = StubPage(["#my-atc"])
        h = get_handler(site)
        task = _task(site, site_params={"selectors": {"atc": ["#my-atc"]}})
        assert h.add_to_cart(page, task) is True, site
        assert page.clicks == [("", "#my-atc")], site
        defaults = set(_defaults(site)["selectors"]["atc"])
        assert not defaults & {s for _, s in page.queries}, site


# --------------------------------------------------------------------------- #
# checkout
# --------------------------------------------------------------------------- #
def test_dry_run_never_clicks_place_order(user_store_site):
    for site in list_sites():
        d = _defaults(site)["selectors"]
        page = StubPage([d["checkout"][0], d["place_order"][0], "#email"])
        h = get_handler(site)
        task = _task(site, site_params={"fields": {"#email": "email"}})
        out = h.checkout(page, task, PROFILE, dry_run=True)
        assert out["ok"] is True and out.get("dry_run") is True, site
        assert out["order_id"] == "", site
        clicked = {s for _, s in page.clicks}
        assert d["checkout"][0] in clicked, site
        assert not clicked & set(d["place_order"]), site
        assert ("", "#email", PROFILE["email"]) in page.fills, site


def test_live_without_place_order_button_fails(user_store_site):
    for site in list_sites():
        d = _defaults(site)["selectors"]
        page = StubPage([d["checkout"][0]], html=CONFIRMED_HTML)
        h = get_handler(site)
        out = h.checkout(page, _task(site), PROFILE, dry_run=False)
        assert out["ok"] is False, site
        assert "place-order" in out["error"], site
        assert out["order_id"] == "", site
        queried = [s for _, s in page.queries]
        assert all(s in queried for s in d["place_order"]), site


def test_live_reads_order_id_from_configurable_confirmation(user_store_site):
    for site in list_sites():
        d = _defaults(site)["selectors"]
        page = StubPage([d["checkout"][0], d["place_order"][0]], html=CONFIRMED_HTML)
        h = get_handler(site)
        out = h.checkout(page, _task(site), PROFILE, dry_run=False)
        assert out["ok"] is True and out["order_id"] == "123456789012", (site, out)
        assert ("", d["place_order"][0]) in page.clicks, site

    # A store's own regex / success text win over the handler default.
    page = StubPage(["a:has-text('Checkout')", "button:has-text('Place Order')"],
                    html="<p>Your reference is REF-77.</p><p>All set!</p>")
    task = _task("generic", site_params={
        "confirmation": {"order_id_regex": r"reference is ([A-Z]+-\d+)",
                         "success_text": ["all set"]},
        "waits": {"confirm_s": 0}})
    out = get_handler("generic").checkout(page, task, PROFILE, dry_run=False)
    assert out["ok"] is True and out["order_id"] == "REF-77"

    # Submitted but unconfirmed: never a fabricated id.
    page = StubPage(["a:has-text('Checkout')", "button:has-text('Place Order')"], html="<p></p>")
    out = get_handler("generic").checkout(
        page, _task("generic", site_params={"waits": {"confirm_s": 0}}), PROFILE, dry_run=False)
    assert out["ok"] is True and out["order_id"] == "" and "note" in out


def test_fields_only_replaces_handler_defaults():
    page = StubPage(["#e", "input[type='email'], input[name*='email' i], #email"])
    task = _task("generic", site_params={"fields": {"#e": "email"}, "fields_only": True,
                                         "selectors": {"checkout": ["#none"]}})
    get_handler("generic").checkout(page, task, PROFILE, dry_run=True)
    assert [f[1] for f in page.fills] == ["#e"]


# --------------------------------------------------------------------------- #
# frames
# --------------------------------------------------------------------------- #
def test_frame_hints_route_through_frames_find_first(monkeypatch):
    seen = []
    real = frames_mod.find_first

    def spy(page, selector, *, hints=None, require_visible=False):
        seen.append((selector, list(hints or [])))
        return real(page, selector, hints=hints, require_visible=require_visible)

    monkeypatch.setattr(frames_mod, "find_first", spy)
    page = StubPage(["a:has-text('Checkout')"],
                    child_frames=[("payment-frame", "https://pay.test/x", {"#card"})])
    task = _task("generic", site_params={"frames": ["payment"],
                                         "fields": {"#card": "payment.card_number"},
                                         "fields_only": True})
    out = get_handler("generic").checkout(page, task, PROFILE, dry_run=True)
    assert out["ok"] is True
    assert seen and all(h == ["payment"] for _, h in seen)
    assert ("payment-frame", "#card", PROFILE["payment"]["card_number"]) in page.fills
    assert ("payment-frame", "#card") in page.queries


def test_all_handlers_find_elements_inside_frames(user_store_site):
    for site in list_sites():
        page = StubPage([], child_frames=[("f1", "https://f1.test", {"#in-frame"})])
        h = get_handler(site)
        task = _task(site, site_params={"selectors": {"atc": ["#in-frame"]}})
        assert h.add_to_cart(page, task) is True, site
        assert page.clicks == [("f1", "#in-frame")], site


# --------------------------------------------------------------------------- #
# queue
# --------------------------------------------------------------------------- #
def test_queue_waits_until_gone_then_proceeds():
    q = "text=You are in line"
    page = StubPage([q], visible_until={q: 3})
    statuses = []
    ok = get_handler("pokemon_center").handle_queue(
        page, _task("pokemon_center"), on_status=statuses.append)
    assert ok is True and page.tick >= 3 and "queued" in statuses


def test_queue_respects_queue_max_s_and_override(user_store_site):
    page = StubPage(["#custom-queue"])
    task = _task("target", site_params={"selectors": {"queue": ["#custom-queue"]},
                                        "waits": {"queue_max_s": 0}})
    assert get_handler("target").handle_queue(page, task) is False
    assert ("", "#custom-queue") in page.queries
    for site in list_sites():
        assert get_handler(site).handle_queue(StubPage([]), _task(site)) is True, site


def test_user_store_seller_trust_exact_host(user_store_site):
    h = get_handler(user_store_site)
    assert h.detect_seller("", "https://teststore.test/p")["is_official"] is True
    assert h.detect_seller("", "https://www.teststore.test/p")["is_official"] is True
    assert h.detect_seller("", "https://eststore.test/p")["is_official"] is None
    assert h.detect_seller("", "https://teststore.test.evil/p")["is_official"] is None
