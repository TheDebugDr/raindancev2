"""TaskRunner driven end to end, with no browser and no network.

The original audit's sharpest finding about tests was that ``TaskRunner`` — the
only component that can spend money — had zero coverage. Everything protecting
the money path was asserted by reading source text, which cannot tell the
difference between code that is present and code that runs.

These tests stub Playwright, the browser factory and the site handler, then run
the real ``TaskRunner.run`` and assert on what it actually *did*: which handler
methods were called, in which order, and whether a purchase happened.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from raindance.core import classify as C
from raindance.tasks import models as M
from raindance.tasks import runner as runner_mod


# --------------------------------------------------------------------------- #
# stubs
# --------------------------------------------------------------------------- #
class Bus:
    def __init__(self):
        self.lines = []

    def log(self, msg, level="info"):
        self.lines.append((level, str(msg)))

    def text(self):
        return "\n".join(m for _, m in self.lines)


class Page:
    def __init__(self, body=""):
        self._body = body
        self.url = "https://www.walmart.com/ip/x/1"

    def content(self):
        return self._body

    def screenshot(self, **kw):
        return b""

    def goto(self, *a, **kw):
        return None

    def wait_for_timeout(self, *a, **kw):
        return None

    def close(self):
        return None


class Ctx:
    def close(self):
        return None


class Browser:
    def close(self):
        return None


class Factory:
    """Stands in for BrowserFactory; hands back the stub page."""

    def __init__(self, page, **kw):
        self.page = page
        self.last_session = None

    def create_context(self, p, **kw):
        return Browser(), Ctx(), self.page


class Handler:
    """Records every call so a test can assert on the sequence."""

    def __init__(self, *, stock=True, cart_total=None, clearable=True):
        self.calls = []
        self._stock = stock
        self._cart_total = cart_total
        self._clearable = clearable

    # -- things the runner drives --
    def bind_task(self, task):
        pass

    def navigate(self, page, task, **kw):
        self.calls.append("navigate")
        return True

    def handle_queue(self, page, task, **kw):
        self.calls.append("handle_queue")
        return True

    def clear_cart(self, page):
        if not self._clearable:
            raise NotImplementedError("stub cannot clear")
        self.calls.append("clear_cart")
        return 0

    def read_cart_total(self, page):
        self.calls.append("read_cart_total")
        return self._cart_total

    def add_to_cart(self, page, task, **kw):
        self.calls.append("add_to_cart")
        return True

    def checkout(self, page, task, profile, *, dry_run=True, **kw):
        self.calls.append("checkout")
        if dry_run:
            return {"ok": True, "order_id": "", "error": "", "dry_run": True}
        self.calls.append("place_order")
        return {"ok": True, "order_id": "ORDER-1", "error": ""}

    def detect_stock_signals(self, html):
        return self._stock

    def detect_seller(self, html, url):
        return {"is_official": True, "seller": "Walmart.com"}


class TaskStore:
    def __init__(self, task):
        self.task = dict(task)
        self.runtime = []

    def get(self, tid):
        return dict(self.task)

    def set_runtime(self, tid, **kw):
        self.runtime.append(kw)
        self.task.update(kw)


class ProfileStore:
    def __init__(self, pid="prof_test"):
        self.p = {"id": pid, "name": "test", "user_data_dir": "",
                  "shipping": {}, "billing": {}, "payment": {}}

    def get(self, pid):
        return dict(self.p)

    def ensure_default(self):
        return dict(self.p)


class Settings:
    def __init__(self, **over):
        self.data = {
            "browser": {"headless": True},
            "checkout": {},
            "evasion": {"enabled": False},
            "orchestrator": {},
        }
        self.data.update(over)


class Captcha:
    mode = "manual"


class Groups:
    def get_proxy(self, key, group):
        return None

    def get_manager(self, group):
        return None

    def manager_for(self, *a, **kw):
        return None


# --------------------------------------------------------------------------- #
# harness
# --------------------------------------------------------------------------- #
WALMART_ETB_AT_MSRP = (
    '<script id="__NEXT_DATA__" type="application/json">'
    '{"props":{"pageProps":{"initialData":{"data":{"product":'
    '{"name":"ETB","priceInfo":{"currentPrice":{"price":59.99}}}}}}}}'
    '</script><div>Sold and shipped by Walmart.com</div> Add to cart'
)

TITLE = "Pokemon TCG: Scarlet & Violet-Surging Sparks Elite Trainer Box"


def build(monkeypatch, *, dry_run=True, checkout_cfg=None, handler=None,
          body=WALMART_ETB_AT_MSRP, quantity=1):
    page = Page(body)
    handler = handler or Handler()
    bus = Bus()

    monkeypatch.setattr(runner_mod, "BrowserFactory",
                        lambda **kw: Factory(page, **kw))
    monkeypatch.setattr(runner_mod, "get_handler",
                        lambda site, **kw: handler, raising=False)

    class _PW:
        def __enter__(self):
            return object()

        def __exit__(self, *a):
            return False

    import playwright.sync_api as pw
    monkeypatch.setattr(pw, "sync_playwright", lambda: _PW())

    task = M.normalize_task({
        "id": "t1", "name": TITLE, "url": "https://www.walmart.com/ip/x/1",
        "site": "walmart", "profile_id": "prof_test", "dry_run": dry_run,
        "quantity": quantity,
    })
    cfg = {"profile_lease_wait_seconds": 0.2}
    cfg.update(checkout_cfg or {})
    settings = Settings(checkout=cfg)
    r = runner_mod.TaskRunner(
        bus=bus, hub=None, task_store=TaskStore(task),
        profile_store=ProfileStore(), proxy_groups=Groups(),
        captcha=Captcha(), settings=settings, should_stop=lambda: False,
    )
    return r, task, handler, bus


# --------------------------------------------------------------------------- #
# the money path
# --------------------------------------------------------------------------- #
def test_dry_run_never_touches_the_cart(monkeypatch):
    """The default rehearsal must not add to a persistent cart."""
    r, task, handler, bus = build(monkeypatch, dry_run=True)
    out = r.run(task)
    assert out.get("dry_run") is True
    assert "add_to_cart" not in handler.calls, handler.calls
    assert "checkout" not in handler.calls, handler.calls
    assert out.get("dry_run_depth") == "gate"


def test_dry_run_depth_cart_opts_into_touching_the_cart(monkeypatch):
    r, task, handler, bus = build(
        monkeypatch, dry_run=True, checkout_cfg={"dry_run_depth": "cart"})
    r.run(task)
    assert "add_to_cart" in handler.calls
    assert handler.calls.index("clear_cart") < handler.calls.index("add_to_cart")


def test_a_refused_gate_never_reaches_the_cart(monkeypatch):
    """A scalper-priced listing must abort before anything is added."""
    body = WALMART_ETB_AT_MSRP.replace('"price":59.99', '"price":219.99')
    r, task, handler, bus = build(
        monkeypatch, dry_run=False, body=body,
        checkout_cfg={"dry_run_depth": "checkout"})
    out = r.run(task)
    assert out.get("aborted_gate") is True
    assert "add_to_cart" not in handler.calls
    assert "place_order" not in handler.calls


def test_live_run_clears_the_cart_then_checks_the_total_then_buys(monkeypatch):
    handler = Handler(cart_total=64.00)
    r, task, h, bus = build(monkeypatch, dry_run=False, handler=handler)
    out = r.run(task)
    assert out.get("ok") is True and out.get("order_id") == "ORDER-1"
    order = handler.calls
    assert order.index("clear_cart") < order.index("add_to_cart")
    assert order.index("read_cart_total") < order.index("place_order")


def test_a_cart_holding_more_than_the_gate_approved_aborts(monkeypatch):
    """The gate priced one $59.99 unit; the basket says $259.99."""
    handler = Handler(cart_total=259.99)
    r, task, h, bus = build(monkeypatch, dry_run=False, handler=handler)
    out = r.run(task)
    assert out.get("aborted_gate") is True
    assert "place_order" not in handler.calls
    assert "cart total" in bus.text().lower()


def test_an_unreadable_cart_total_aborts_a_live_run_by_default(monkeypatch):
    handler = Handler(cart_total=None)
    r, task, h, bus = build(monkeypatch, dry_run=False, handler=handler)
    out = r.run(task)
    assert out.get("aborted_gate") is True
    assert "place_order" not in handler.calls


def test_require_cart_check_can_be_turned_off_deliberately(monkeypatch):
    handler = Handler(cart_total=None)
    r, task, h, bus = build(monkeypatch, dry_run=False, handler=handler,
                            checkout_cfg={"require_cart_check": False})
    out = r.run(task)
    assert out.get("ok") is True


def test_force_dry_run_overrides_a_live_task(monkeypatch):
    r, task, handler, bus = build(
        monkeypatch, dry_run=False, checkout_cfg={"force_dry_run": True})
    out = r.run(task)
    assert out.get("dry_run") is True
    assert "place_order" not in handler.calls


def test_a_busy_profile_refuses_rather_than_sharing_a_cart(monkeypatch):
    from raindance.core.leases import LEASES
    assert LEASES.acquire("prof_test", "someone_else") is True
    try:
        r, task, handler, bus = build(
            monkeypatch, dry_run=False,
            checkout_cfg={"profile_lease_wait_seconds": 0.1})
        out = r.run(task)
        assert out.get("profile_busy") is True
        assert handler.calls == [], handler.calls
    finally:
        LEASES.release("prof_test", "someone_else")


def test_the_lease_is_released_so_the_next_task_can_run(monkeypatch):
    from raindance.core.leases import LEASES
    r, task, handler, bus = build(monkeypatch, dry_run=True)
    r.run(task)
    assert LEASES.holder("prof_test") is None
    assert LEASES.acquire("prof_test", "next_task") is True
    LEASES.release("prof_test", "next_task")


def test_a_handler_that_cannot_clear_the_cart_warns_but_proceeds(monkeypatch):
    handler = Handler(cart_total=64.00, clearable=False)
    r, task, h, bus = build(monkeypatch, dry_run=False, handler=handler)
    out = r.run(task)
    assert "cannot clear the cart" in bus.text()
    assert out.get("ok") is True


def test_the_gate_verdict_and_its_evidence_are_logged(monkeypatch):
    r, task, handler, bus = build(monkeypatch, dry_run=True)
    r.run(task)
    log = bus.text()
    assert "retail/MSRP gate" in log
    assert "gate evidence" in log
    assert "anchored=" in log
