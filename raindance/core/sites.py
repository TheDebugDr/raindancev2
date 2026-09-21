"""Map a product URL to a retailer name. Drives the dynamic per-site tabs:
a product's tab is just its detected retailer, so a never-before-seen retailer
automatically gets its own tab."""
from __future__ import annotations

from urllib.parse import urlparse

# substring in hostname → display name. Order doesn't matter (first hit wins).
_RETAILERS: list[tuple[str, str]] = [
    ("amazon", "Amazon"),
    ("ebay", "eBay"),
    ("target", "Target"),
    ("bestbuy", "Best Buy"),
    ("walmart", "Walmart"),
    ("pokemoncenter", "Pokémon Center"),
    ("pokemon", "Pokémon Center"),
    ("gamestop", "GameStop"),
    ("costco", "Costco"),
    ("samsclub", "Sam's Club"),
    ("newegg", "Newegg"),
    ("tcgplayer", "TCGplayer"),
    ("barnesandnoble", "Barnes & Noble"),
    ("walgreens", "Walgreens"),
    ("apple", "Apple"),
]


# TLDs where the registrable domain is one level deeper (foo.co.uk → "foo").
_SECOND_LEVEL_TLDS = {"co.uk", "org.uk", "com.au", "co.nz", "co.jp", "com.br",
                      "co.za", "com.mx", "co.in", "com.sg"}


def known_retailers() -> list[str]:
    """Every retailer we can detect from a URL, de-duplicated, in listing order.

    Drives the Set up accordion: a retailer appears there even with nothing
    watched yet, so you can see it is supported before you paste a URL.
    """
    seen: list[str] = []
    for _, name in _RETAILERS:
        if name not in seen:
            seen.append(name)
    return seen


def match_retailer(text: str) -> str | None:
    """Return a known retailer name if any needle appears in `text` (a hostname
    or a source label like 'Amazon.com'), else None."""
    t = (text or "").lower()
    for needle, name in _RETAILERS:
        if needle in t:
            return name
    return None


def detect_site(url: str) -> str:
    """Return the retailer name for a URL. Falls back to the domain's second
    level (e.g. 'https://shop.foo.com/x' → 'Foo') so unknown sites still tab."""
    if not url:
        return "Unknown"
    host = urlparse(url if "//" in url else "https://" + url).hostname or ""
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    name = match_retailer(host)
    if name:
        return name
    parts = [p for p in host.split(".") if p]
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        return host                                   # IPv4 → the address, not "0"
    if len(parts) >= 3 and ".".join(parts[-2:]) in _SECOND_LEVEL_TLDS:
        return parts[-3].capitalize()                 # foo.co.uk → "Foo"
    if len(parts) >= 2:
        return parts[-2].capitalize()
    return host or "Unknown"
