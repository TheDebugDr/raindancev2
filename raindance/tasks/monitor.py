"""Phase 2 — lightweight stock monitoring with jittered multi-proxy polls."""
from __future__ import annotations

import random
import threading
import time
import urllib.request
from typing import Callable, Optional

from raindance.sites import get_handler

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Lower rank sorts first, so high-priority items are polled ahead of the rest.
_PRIORITY_RANK = {"high": 0, "normal": 1, "low": 2}


def _priority_rank(priority) -> int:
    return _PRIORITY_RANK.get((priority or "normal").lower(), 1)


def check_url_stock(
    url: str,
    *,
    site: str = "generic",
    timeout: int = 12,
    proxy: Optional[dict] = None,
    product: Optional[dict] = None,
    settings=None,
) -> dict:
    """Fetch and classify (seller + MSRP + stock). Returns
    {in_stock, note, status, reason}.

    `in_stock` is True ONLY for a confirmed retail-at-MSRP listing, so the
    monitor never auto-triggers checkout on a third-party, over-MSRP, or
    bot-walled page. A challenge body is reported honestly as `blocked`.
    """
    if not url:
        return {"in_stock": None, "note": "no url", "status": "error", "reason": "no url"}

    from raindance.core import classify as C

    # Target fast path: read stock + price from the RedSky product API instead of
    # the bot-walled HTML page, then run the SAME anti-scalper gate on those
    # signals. Fails safe — returns None → we fall through to the HTML fetch.
    if (site or "").lower() == "target" and settings is not None:
        try:
            from raindance.sites import target_api
            signals = target_api.fetch(url, settings, site="target", proxy=proxy)
        except Exception:
            signals = None
        if signals:
            handler = get_handler(site)
            verdict = C.classify("", url, product, source="target-api",
                                 handler=handler, settings=settings, signals=signals)
            if verdict["status"] == C.IN_STOCK_RETAIL:
                api_stock: Optional[bool] = True
            elif verdict["status"] == C.OUT_OF_STOCK:
                api_stock = False
            else:
                api_stock = None
            return {"in_stock": api_stock,
                    "note": verdict.get("reason") or verdict["status"],
                    "status": verdict["status"], "reason": verdict.get("reason")}
    try:
        opener = urllib.request.build_opener()
        if proxy and proxy.get("server"):
            server = proxy["server"]
            # simple HTTP proxy only for monitor polls; skip socks
            if not server.startswith("socks"):
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({"http": server, "https": server})
                )
        req = urllib.request.Request(
            url,
            headers={"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"},
        )
        with opener.open(req, timeout=timeout) as resp:
            html = resp.read(700_000).decode("utf-8", "ignore")
    except Exception as e:
        return {"in_stock": None, "note": str(e)[:100], "status": "error",
                "reason": str(e)[:100]}

    handler = get_handler(site)
    verdict = C.classify(html, url, product, source="http",
                         handler=handler, settings=settings)
    if verdict["status"] == C.IN_STOCK_RETAIL:
        in_stock: Optional[bool] = True
    elif verdict["status"] == C.OUT_OF_STOCK:
        in_stock = False
    else:
        in_stock = None
    return {"in_stock": in_stock, "note": verdict.get("reason") or verdict["status"],
            "status": verdict["status"], "reason": verdict.get("reason")}


class MonitorService:
    """Background monitor: polls enabled monitor-tasks until stock → callback."""

    def __init__(
        self,
        *,
        bus,
        hub,
        task_store,
        proxy_groups,
        settings,
        on_stock: Callable[[dict], None],
    ):
        self.bus = bus
        self.hub = hub
        self.task_store = task_store
        self.proxy_groups = proxy_groups
        self.settings = settings
        self.on_stock = on_stock
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._alerted: set[str] = set()

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        if self.is_running:
            return False
        self._stop.clear()
        self._alerted.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="raindance-monitor"
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        poll = self.settings.data.get("poll") or {}
        base = float(poll.get("interval_seconds") or 10)
        jitter = float(poll.get("jitter_seconds") or 5)
        # High-priority items poll on a tighter cadence so a contested drop is
        # seen within a second or two, not on the next 10s tick.
        fast = float(poll.get("fast_interval_seconds") or max(1.0, base / 8))
        self.bus.log(
            f"[monitor] Phase 2 started — interval≈{base}s ±{jitter}s "
            f"(high-priority≈{fast:.1f}s)",
            "ok",
        )
        due: dict[str, float] = {}
        # Statuses the monitor must not re-poll: in-flight pipeline states, plus
        # the terminal ones. "failed" and "stopped" were missing, so a task that
        # failed checkout — or that the user deliberately stopped — was picked up
        # again on the very next tick and re-fired, indefinitely. Re-arming is an
        # explicit action, not something a poll loop decides.
        busy = {
            "launching", "queued", "atc", "captcha", "checkout",
            "submitting", "triggered", "success", "retrying",
            "failed", "stopped",
        }
        while not self._stop.is_set():
            # Enabled monitor tasks that are not already in a checkout pipeline,
            # highest priority first so the item you most want is polled first.
            tasks = []
            for t in self.task_store.list(enabled_only=True):
                if not t.get("monitor", True):
                    continue
                st = t.get("status") or "idle"
                if st in busy:
                    continue
                tasks.append(t)
            tasks.sort(key=lambda t: _priority_rank(t.get("priority")))

            now = time.monotonic()
            for t in tasks:
                if self._stop.is_set():
                    break
                tid = t.get("id") or ""
                if due.get(tid, 0) > now:
                    continue
                try:
                    interval = self._poll_task(t, base=base, fast=fast, jitter=jitter)
                except Exception as e:
                    # One bad poll must not kill the loop. Without this a single
                    # disk or network error ended restock detection for EVERY
                    # task while the UI kept reporting "watching" — the same
                    # guard Scanner._check_one has carried all along.
                    self.bus.log(
                        f"[monitor] poll failed for {t.get('name') or tid}: {e}", "err"
                    )
                    try:
                        self.task_store.set_runtime(
                            tid, status="monitoring",
                            message=f"poll error: {e}"[:100],
                        )
                    except Exception:
                        pass
                    interval = max(base, 5.0)  # back off, keep watching
                due[tid] = time.monotonic() + interval
            self._stop.wait(0.5)
        self.bus.log("[monitor] Phase 2 stopped", "warn")

    def _poll_task(self, t: dict, *, base: float, fast: float, jitter: float) -> float:
        """Poll one task, act on the verdict, return seconds until its next poll.

        Raises on an unexpected failure; the caller logs it, backs that one task
        off and keeps every other task being watched.
        """
        tid = t["id"]
        self.task_store.set_runtime(tid, status="monitoring", message="polling")
        group = t.get("proxy_group") or "default"
        proxy = self.proxy_groups.get_proxy(f"mon:{tid}", group)
        res = check_url_stock(
            t.get("url") or "",
            site=t.get("site") or "generic",
            proxy=proxy,
            product={"name": t.get("name"), "url": t.get("url")},
            settings=self.settings,
        )
        prio = (t.get("priority") or "normal").lower()
        # A per-task poll_seconds wins over the global tiers, so the UI can give
        # one listing a 5s cadence on the loop that actually fires checkout (the
        # Scanner's per-product loop cannot).
        own = t.get("poll_seconds")
        if own:
            try:
                span = max(0.5, float(own))
            except (TypeError, ValueError):
                span = fast if prio == "high" else base
            interval = span + random.uniform(0, min(jitter, span * 0.25))
        else:
            span = fast if prio == "high" else base
            interval = span + random.uniform(
                0, jitter if prio != "high" else min(jitter, span))

        if res["in_stock"] is True:
            # Only fires for a confirmed retail-at-MSRP listing.
            self.bus.log(
                f"[monitor] ★ RETAIL STOCK — {t.get('name') or tid} "
                f"({res.get('reason') or res['note']})",
                "hit",
            )
            self.task_store.set_runtime(
                tid, status="triggered",
                message=res.get("reason") or "retail stock at MSRP",
            )
            if tid not in self._alerted:
                self._alerted.add(tid)
                self._notify_stock(t)
            try:
                self.on_stock(t)
            except Exception as e:
                self.bus.log(f"[monitor] trigger error: {e}", "err")
        elif res["status"] == "error":
            self.bus.log(f"[monitor] {t.get('name') or tid}: {res['note']}", "warn")
            self.task_store.set_runtime(
                tid, status="monitoring", message=res["note"][:80]
            )
        else:
            # blocked / unverified / reseller / over-MSRP / unknown — the honest
            # non-buyable states; keep watching, surface the reason.
            self.task_store.set_runtime(
                tid, status="monitoring",
                message=(res.get("reason") or res.get("status") or "watching")[:100],
            )
        return interval

    def _notify_stock(self, task: dict) -> None:
        if not self.hub:
            return
        from raindance.core.notifications import NotificationEvent
        self.hub.notify(NotificationEvent(
            kind="in_stock",
            title="🟢 Stock detected — activating tasks",
            message=f"{task.get('name') or task.get('id')} ({task.get('site')})",
            url=task.get("url"),
        ))
