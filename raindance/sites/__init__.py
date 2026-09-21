"""Retailer-specific handlers for navigation, ATC, queue, and checkout.

See base.py for the override layer every handler shares, README.md in this
package for the per-store `site_params` contract, and _template.py for a
copy-and-rename skeleton when adding a retailer.
"""
from __future__ import annotations

from raindance.sites.base import SiteHandler
from raindance.sites.bestbuy import BestBuyHandler
from raindance.sites.generic import GenericHandler
from raindance.sites.pokemon_center import PokemonCenterHandler
from raindance.sites.target import TargetHandler
from raindance.sites.walmart import WalmartHandler

_REGISTRY = {
    "pokemon_center": PokemonCenterHandler,
    "target": TargetHandler,
    "walmart": WalmartHandler,
    "bestbuy": BestBuyHandler,
    "generic": GenericHandler,
}


# Ids seeded above are the built-ins; user-owned stores register at startup.
BUILTIN_SITES = tuple(_REGISTRY.keys())


def register_handler(site_id: str, cls) -> None:
    """Register a handler under a new site id (e.g. one of the user's stores).

    Makes the id valid everywhere: get_handler resolves it, list_sites lists it,
    and normalize_task stops rewriting it to "generic".
    """
    if site_id:
        _REGISTRY[site_id] = cls


def unregister_handler(site_id: str) -> None:
    if site_id not in BUILTIN_SITES:
        _REGISTRY.pop(site_id, None)


def handler_class(site: str):
    """The handler class for `site` (GenericHandler for an unknown id)."""
    return _REGISTRY.get(site) or GenericHandler


def get_handler(site: str, *, bus=None, captcha=None) -> SiteHandler:
    return handler_class(site)(bus=bus, captcha=captcha)


def list_sites() -> list[str]:
    return sorted(_REGISTRY.keys())


def handler_capabilities(site_id: str) -> dict:
    """What the handler behind `site_id` implements — for the Catalog editor.

    {
      "site_id": str, "handler": str (class name), "registered": bool,
      "custom_queue": bool,     # overrides the generic handle_queue
      "custom_atc": bool,       # overrides the generic add_to_cart
      "custom_checkout": bool,  # overrides the generic checkout
      "custom_navigate": bool,
      "frame_aware": bool,      # lookups search child frames (all handlers)
      "seller_detection": bool, # detect_seller is site-specific
      "honors": [...]           # site_params keys the handler reads
    }
    """
    cls = handler_class(site_id)

    def overrides(method: str) -> bool:
        return getattr(cls, method) is not getattr(GenericHandler, method)

    return {
        "site_id": site_id,
        "handler": cls.__name__,
        "registered": site_id in _REGISTRY,
        "custom_queue": overrides("handle_queue"),
        "custom_atc": overrides("add_to_cart"),
        "custom_checkout": overrides("checkout"),
        "custom_navigate": overrides("navigate"),
        "frame_aware": True,
        "seller_detection": cls.detect_seller is not SiteHandler.detect_seller,
        "honors": list(getattr(cls, "HONORS", ()) or ()),
    }
