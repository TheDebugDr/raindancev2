"""Handler for a store the user owns (Shopify / Wix / custom).

A thin consumer of the shared override layer (see base.py): selectors, fields,
frames, waits and confirmation all come from the store's
retailer_configs[<store id>].site_params via `task["site_params"]`, exactly as
they do for every built-in handler. The only thing this class adds is seller
trust: detect_seller answers honestly for a store the user owns — matched on
the registered base_url host, so a lookalike domain does not inherit it.

Everything else (navigation, queue, add-to-cart, checkout, frame-aware lookup,
human pacing) is inherited unchanged. Nothing here touches the evasion layer.
"""
from __future__ import annotations

from typing import Dict, Optional
from urllib.parse import urlparse

from raindance.sites.generic import GenericHandler


def _host(url: str) -> str:
    """Hostname, minus a leading www.

    lstrip() takes a CHARACTER SET, not a prefix: "walmart.com".lstrip("www.")
    is "almart.com", so a lookalike host collapsed onto the real one and
    inherited its official-seller trust. Strip the prefix, not the letters.
    """
    try:
        h = (urlparse(url or "").hostname or "").lower()
        return h[4:] if h.startswith("www.") else h
    except Exception:
        return ""


class ConfigurableStoreHandler(GenericHandler):
    """One class, many stores. Bound to a store record at registration time."""

    name = "user_store"

    # Set by the factory below.
    store_id: str = ""
    store_name: str = ""
    platform: str = "custom"
    base_url: str = ""

    def _params(self, task: Dict) -> Dict:
        """The store's raw site_params (kept for callers that used it)."""
        return (task or {}).get("site_params") or {}

    # -- seller trust ------------------------------------------------------ #
    def detect_seller(self, html: str, url: str) -> Dict:
        """A store the user registered is, by definition, first-party.

        Only when the URL's host matches the registered base_url host — a
        lookalike domain must not inherit that trust.
        """
        mine = _host(self.base_url)
        theirs = _host(url)
        if mine and theirs and theirs == mine:
            return {"is_official": True, "seller": self.store_name or self.store_id}
        return {"is_official": None, "seller": None}


def handler_for_store(store: Dict):
    """Build a handler class bound to one store record."""
    return type(
        f"Store_{store.get('id', 'x')}",
        (ConfigurableStoreHandler,),
        {
            "name": store.get("id", "user_store"),
            "store_id": store.get("id", ""),
            "store_name": store.get("name", ""),
            "platform": store.get("platform", "custom"),
            "base_url": store.get("base_url", ""),
        },
    )


def register_user_stores(settings, *, bus=None) -> int:
    """Register a handler for every store in settings["stores"]. Returns count."""
    from raindance.sites import register_handler

    n = 0
    for store in (settings.data.get("stores") or []):
        sid = store.get("id")
        if not sid:
            continue
        register_handler(sid, handler_for_store(store))
        n += 1
    if bus is not None and n:
        bus.log(f"[stores] {n} user store handler(s) registered", "info")
    return n
