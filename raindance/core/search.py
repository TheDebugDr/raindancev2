"""Internet product search for the discovery side panel.

Real API integration is provider-based and key-driven:

  * Set the env var  SERPAPI_KEY  to use SerpApi's Google Shopping engine.
    (Get a key at https://serpapi.com/ — this is where the key goes.)
  * With no key set, a well-structured MOCK of popular Pokémon products is
    returned so the UI is fully functional offline. Each mock result has a real
    retailer URL, so site-detection / tab-routing behaves exactly as it would
    with live data.

Swap in Brave Search, Google, etc. by writing another `search_*` function and
pointing SearchService.search at it — the UI never changes.
"""
from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from raindance.core.sites import detect_site, match_retailer

_SERPAPI_DEFAULT_BASE = "https://serpapi.com/search.json"


@dataclass
class SearchResult:
    name: str
    price: float | None
    url: str
    site: str
    image: str
    source: str


def placeholder_image(text: str) -> str:
    """A self-contained Poké-ball-ish SVG data URI, so cards always show art
    even offline / when a result has no thumbnail."""
    initials = "".join(w[0] for w in text.split()[:2]).upper() or "?"
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="120" height="120">'
        '<rect width="120" height="120" rx="12" fill="#1f2937"/>'
        '<circle cx="60" cy="50" r="30" fill="#ef4444"/>'
        '<path d="M30 50h60" stroke="#111827" stroke-width="5"/>'
        '<circle cx="60" cy="50" r="9" fill="#fff" stroke="#111827" stroke-width="4"/>'
        f'<text x="60" y="102" font-size="15" fill="#e5e7eb" text-anchor="middle"'
        f' font-family="sans-serif">{initials}</text></svg>'
    )
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


# --------------------------------------------------------------------------- #
# Mock provider — popular Pokémon products across several retailers
# --------------------------------------------------------------------------- #
# Real Pokémon products with verified, hotlinkable product-photo thumbnails
# (Target's scene7 CDN). Retailer attribution here is illustrative — spread
# across sites so the dynamic per-retailer tabs are exercised — while the name
# and image are the genuine product. Live results (SerpApi) carry the true
# source and thumbnail. Tuple: (name, price, url, image).
_S7 = "https://target.scene7.com/is/image/Target/"
_SIZE = "?wid=240&hei=240&fmt=webp"
_MOCK = [
    ("Prismatic Evolutions Elite Trainer Box", 49.99,
     "https://www.target.com/p/pokemon-prismatic-evolutions-etb/-/A-1008746912",
     _S7 + "GUEST_d3dadfe5-ae38-4db2-ae2d-d381a7b0cb2d" + _SIZE),
    ("Surging Sparks Elite Trainer Box", 49.99,
     "https://www.bestbuy.com/site/pokemon-surging-sparks-etb/6588100.p",
     _S7 + "GUEST_3450b705-5011-4ff0-a341-395c8cbb076f" + _SIZE),
    ("Surging Sparks Booster Bundle", 26.94,
     "https://www.amazon.com/dp/B0DHVML5G5",
     _S7 + "GUEST_11c59c4d-2fc5-437c-9981-11064b201c4f" + _SIZE),
    ("Scarlet & Violet Booster Box + 2 ETB Bundle", 119.99,
     "https://www.walmart.com/ip/pokemon-sv-booster-box-bundle/5039324837",
     _S7 + "GUEST_2da0d1d2-2c25-4962-8eb9-45ffd3d43646" + _SIZE),
    ("Mega Evolution Ascended Heroes Elite Trainer Box", 59.99,
     "https://www.pokemoncenter.com/product/mega-ascended-heroes-etb",
     _S7 + "GUEST_aedc5346-6d34-43cf-bb8b-3dc20d8556ad" + _SIZE),
    ("Mega Evolution Perfect Order Elite Trainer Box", 59.99,
     "https://www.gamestop.com/toys-collectibles/trading-cards/products/mega-perfect-order-etb",
     _S7 + "GUEST_13562999-69e3-4c1a-9273-e06a89a04a7a" + _SIZE),
    ("Surging Sparks Three-Booster Blister (Zapdos)", 14.99,
     "https://www.ebay.com/itm/226044091572",
     _S7 + "GUEST_75f46e2d-586a-435c-8e1a-b0c6c0ee08e3" + _SIZE),
]


def search_mock(query: str) -> list[SearchResult]:
    q = query.lower().strip()
    results = []
    for name, price, url, image in _MOCK:
        if q and not all(tok in name.lower() for tok in q.split()):
            continue
        results.append(SearchResult(
            name=name, price=price, url=url, site=detect_site(url),
            image=image or placeholder_image(name), source=detect_site(url),
        ))
    return results


# --------------------------------------------------------------------------- #
# Real provider — SerpApi Google Shopping (used when a key is configured)
# --------------------------------------------------------------------------- #
def _price_of(item: dict) -> float | None:
    """SerpApi gives `extracted_price` (number) and `price` ("$92.00"). Prefer
    the number; parse the string if that's missing."""
    p = item.get("extracted_price")
    if isinstance(p, (int, float)):
        return float(p)
    s = item.get("price")
    if isinstance(s, str):
        m = re.search(r"([0-9][0-9,]*(?:\.[0-9]+)?)", s)
        if m:
            try:
                return float(m.group(1).replace(",", ""))
            except ValueError:
                pass
    return None


def search_serpapi(query: str, key: str, base: str | None = None,
                   timeout: int = 15) -> list[SearchResult]:
    base = base or os.environ.get("SERPAPI_BASE") or _SERPAPI_DEFAULT_BASE
    params = urllib.parse.urlencode({
        "engine": "google_shopping",
        "q": query or "pokemon tcg",
        "api_key": key,
    })
    url = f"{base}?{params}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:                 # 401 bad key, 429 quota, …
        msg = ""
        try:
            msg = json.loads(e.read().decode("utf-8", "ignore")).get("error", "")
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code}: {msg or e.reason}")

    if isinstance(data, dict) and data.get("error"):     # SerpApi error payload
        raise RuntimeError(str(data["error"]))

    # Products can appear under shopping_results and/or inline_shopping_results.
    items = (data.get("shopping_results") or []) + (data.get("inline_shopping_results") or [])
    out: list[SearchResult] = []
    for item in items[:24]:
        title = item.get("title", "Untitled")
        source = (item.get("source") or "").strip()
        # product_link is a Google Shopping URL, so route the tab by `source`.
        plink = item.get("product_link") or item.get("link") or ""
        site = match_retailer(source) or source or match_retailer(plink) or detect_site(plink) or "Unknown"
        out.append(SearchResult(
            name=title,
            price=_price_of(item),
            url=plink,
            site=site,
            image=item.get("thumbnail") or placeholder_image(title),
            source=source or site,
        ))
    return out


class SearchService:
    """Picks a provider at call time: SerpApi if a key is configured (env
    SERPAPI_KEY or settings.search.serpapi_key), else the offline mock."""

    def __init__(self, settings=None, bus=None):
        self.settings = settings
        self.bus = bus

    def _cfg(self) -> dict:
        return (self.settings.data.get("search", {}) if self.settings else {})

    def api_key(self) -> str:
        return (os.environ.get("SERPAPI_KEY") or self._cfg().get("serpapi_key") or "").strip()

    def base_url(self) -> str:
        return (os.environ.get("SERPAPI_BASE") or self._cfg().get("serpapi_base")
                or _SERPAPI_DEFAULT_BASE).strip()

    @property
    def provider_name(self) -> str:
        return "SerpApi (live)" if self.api_key() else "Mock (offline)"

    def search(self, query: str) -> list[SearchResult]:
        key = self.api_key()
        if key:
            try:
                results = search_serpapi(query, key, base=self.base_url())
                if self.bus:
                    self.bus.log(f"search '{query}' via SerpApi → {len(results)} result(s)", "info")
                return results
            except Exception as e:  # noqa: BLE001 - surface the reason, fall back to mock
                if self.bus:
                    self.bus.log(f"SerpApi error ({e}); falling back to mock", "warn")
        results = search_mock(query)
        if self.bus:
            self.bus.log(f"search '{query}' via mock → {len(results)} result(s)", "info")
        return results
