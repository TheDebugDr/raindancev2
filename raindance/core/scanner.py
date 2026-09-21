"""Product scanner — the "run a check" backend for the Execute page.

Does a real, site-agnostic HTTP fetch and heuristically reads stock state +
price from the page text. It works on server-rendered pages; JS-heavy or
bot-protected retailers (Amazon/Target often) may block or hide the data, which
is reported honestly as `unknown`/`error` rather than faked. Runs off the UI
thread; progress and results stream through the event bus.
"""
from __future__ import annotations

import re
import threading
import time
import urllib.request
from datetime import datetime

from raindance.core import classify
from raindance.core.notifications import NotificationEvent

# UI metadata for a status (label, Quasar colour). Shared by the pages.
STATUS_META: dict[str, tuple[str, str]] = {
    "pending": ("Pending", "grey"),
    "in_stock": ("In stock", "green"),
    "in_stock_retail": ("In stock · retail", "green"),
    "in_stock_reseller": ("Reseller · over MSRP", "deep-orange"),
    "in_stock_over_msrp": ("In stock · over MSRP", "deep-orange"),
    "in_stock_unverified": ("In stock · unverified", "amber"),
    "out_of_stock": ("Out of stock", "red"),
    "blocked": ("Blocked · bot wall", "amber"),
    "unknown": ("Unknown", "orange"),
    "error": ("Error", "red"),
}

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")


def parse_frequency(value, default: int = 30) -> int:
    """Turn a frequency label into seconds: '30s'→30, '5m'→300, '1h'→3600.
    A bare number is seconds; anything unrecognised → `default`."""
    m = re.match(r"\s*(\d+)\s*([smh]?)", str(value or "").lower())
    if not m:
        return default
    return max(1, int(m.group(1)) * {"s": 1, "m": 60, "h": 3600}[m.group(2) or "s"])


def scan_product(url: str, product: dict | None = None, *,
                 timeout: int = 12, settings=None) -> dict:
    """Fetch a URL and classify seller + MSRP + stock. Best-effort, never raises.

    Returns the classifier verdict (status/seller/is_official/price/msrp/at_msrp/
    reason). A bot-protected retailer usually returns a challenge body over a
    plain fetch, which is reported honestly as `blocked` — never a fabricated
    in-stock.
    """
    if not url:
        return {"status": classify.ERROR, "price": None, "note": "no url",
                "reason": "no url"}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA,
                                                   "Accept-Language": "en-US,en;q=0.9"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            html = resp.read(700_000).decode("utf-8", "ignore")
    except Exception as e:  # noqa: BLE001
        return {"status": classify.ERROR, "price": None, "note": str(e)[:80],
                "reason": str(e)[:80]}

    # Route to the retailer's handler for seller/stock parsing (falls back to
    # generic). Imported lazily to avoid any import-order coupling.
    from raindance.core import run_plan
    from raindance.core.sites import detect_site
    from raindance.sites import get_handler
    handler = get_handler(run_plan.handler_for(detect_site(url)))
    verdict = classify.classify(html, url, product, source="http",
                                handler=handler, settings=settings)
    verdict["note"] = "http"
    return verdict


class Scanner:
    """Runs a batch of product checks on a worker thread and writes results
    back into the store; the UI reflects them via bindings/timers."""

    def __init__(self, store, bus, hub=None):
        self.store = store
        self.bus = bus
        self.hub = hub                      # provider-agnostic notifications (may be None)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._watch_thread: threading.Thread | None = None
        self._watch_stop = threading.Event()
        self.state = {"running": False, "watching": False, "done": 0, "total": 0}

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def is_watching(self) -> bool:
        return self._watch_thread is not None and self._watch_thread.is_alive()

    def _check_one(self, p: dict) -> None:
        """Check a single product, write the verdict back to the store, and alert
        only on a genuine retail-at-MSRP restock."""
        prev_status = p.get("status")
        self.store.update(p["id"], status="pending")
        res = scan_product(p["url"], p, settings=self.store.settings)
        status = res["status"]
        price = res.get("price")
        self.store.update(
            p["id"], status=status, price=price,
            seller=res.get("seller"), is_official=res.get("is_official"),
            at_msrp=res.get("at_msrp"), msrp=res.get("msrp"),
            verdict_reason=res.get("reason"), source=res.get("source"),
            last_checked=datetime.now().strftime("%H:%M:%S"))
        label = STATUS_META.get(status, (status, ""))[0]
        extra = f" — {res['reason']}" if res.get("reason") else (
            f" — ${price:.2f}" if isinstance(price, (int, float)) else "")
        self.bus.log(f"  {p['name']}: {label}{extra}",
                     "hit" if status == classify.IN_STOCK_RETAIL else "info")
        self._maybe_notify(p, prev_status, status, price, res)

    def _maybe_notify(self, p, prev_status, new_status, price, res) -> None:
        """Alert only on a transition INTO in_stock_retail — a genuine retail
        listing at/near MSRP — so no third-party, over-MSRP, or blocked page can
        ever produce an alert. Fires once per restock, not every scan."""
        if not self.hub:
            return
        if new_status != classify.IN_STOCK_RETAIL or prev_status == classify.IN_STOCK_RETAIL:
            return
        price_s = f" — ${price:.2f}" if isinstance(price, (int, float)) else ""
        self.hub.notify(NotificationEvent(
            kind="in_stock",
            title="🟢 In stock at retail (≈ MSRP)",
            message=f"{p['name']}{price_s} ({p.get('site', '')}) — {res.get('reason', '')}",
            url=p.get("url"),
        ))

    def scan(self, products: list[dict]) -> bool:
        """One-shot scan of the given products."""
        if self.is_running:
            self.bus.log("a scan is already running", "warn")
            return False
        items = list(products)
        if not items:
            self.bus.log("nothing to scan", "warn")
            return False
        self._stop.clear()
        self.state.update(running=True, done=0, total=len(items))

        def worker():
            self.bus.log(f"scan started — {len(items)} product(s)", "info")
            for p in items:
                if self._stop.is_set():
                    self.bus.log("scan stopped", "warn")
                    break
                self._check_one(p)
                self.state["done"] += 1
            self.state.update(running=False)
            self.bus.log("scan complete", "ok")

        self._thread = threading.Thread(target=worker, daemon=True, name="raindance-scan")
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()

    # -- continuous auto-monitor ------------------------------------------- #
    def start_watch(self, get_products) -> bool:
        """Continuously re-check products, each on its own `frequency` interval,
        until stopped. `get_products()` returns the current list to watch, so
        scope/add/remove changes are picked up live."""
        if self.is_watching:
            self.bus.log("auto-monitor already running", "warn")
            return False
        self._watch_stop.clear()
        self.state["watching"] = True

        def worker():
            self.bus.log("auto-monitor started — re-checking on each product's frequency", "ok")
            due: dict[str, float] = {}
            while not self._watch_stop.is_set():
                for p in list(get_products()):
                    if self._watch_stop.is_set():
                        break
                    pid = p["id"]
                    if due.get(pid, 0.0) <= time.monotonic():
                        try:
                            self._check_one(p)
                        except Exception as e:  # one bad check must not kill the loop
                            self.bus.log(f"check failed for {p.get('name', '?')}: {e}", "warn")
                        due[pid] = time.monotonic() + parse_frequency(p.get("frequency", "30s"))
                self._watch_stop.wait(1.0)  # tick between passes; interruptible
            self.state["watching"] = False
            self.bus.log("auto-monitor stopped", "warn")

        self._watch_thread = threading.Thread(target=worker, daemon=True, name="raindance-watch")
        self._watch_thread.start()
        return True

    def stop_watch(self) -> None:
        self._watch_stop.set()
