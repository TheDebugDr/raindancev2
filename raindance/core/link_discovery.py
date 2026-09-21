"""Find product URLs on a storefront by reading its public product feed.

Scope, deliberately: this enumerates a storefront the way the storefront itself
offers to be enumerated —

    shopify   /products.json        (documented, paginated, JSON)
    any store /sitemap.xml          (and sitemap_index.xml -> child sitemaps)

Both are published by the store for exactly this purpose. There is no attempt
to get around bot protection, no headless browser, and no evasion layer
involvement — if a site does not want to be read this way it simply returns
nothing and discovery reports that honestly.

Matching a product title to a (set, line) is fuzzy, so nothing here writes a
listing. It returns SCORED PROPOSALS; the caller decides what to keep.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

USER_AGENT = "RainDance/1.0 (+catalog link discovery)"
TIMEOUT = 12
MAX_PRODUCTS = 2000

# Words that carry no signal when matching a title.
_STOP = {"the", "a", "of", "and", "pokemon", "pokémon", "tcg", "trading",
         "card", "game", "cards", "new", "sealed", "preorder", "pre", "order"}

_WORD = re.compile(r"[a-z0-9]+")

# Short codes that are ordinary words cannot stand alone as a line match:
# "Box" appears in "Elite Trainer Box" too, so treating it as a perfect hit
# matched every ETB title to Booster Box. Distinctive codes (ETB, SPC, UPC,
# Bundle, Blister) keep the shortcut.
_GENERIC_SHORT = {"box", "tin", "pack", "set", "case", "collection", "deck"}


def _tokens(text: str) -> set:
    return {w for w in _WORD.findall((text or "").lower()) if w not in _STOP}


def _norm_base(base_url: str) -> str:
    b = (base_url or "").strip().rstrip("/")
    if b and not b.startswith(("http://", "https://")):
        b = "https://" + b
    return b


def _fetch(url: str, *, timeout: int = TIMEOUT) -> Optional[bytes]:
    """GET a URL. Returns None on any failure — discovery is best-effort."""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json, application/xml, text/xml, */*",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if getattr(resp, "status", 200) >= 400:
                return None
            return resp.read(6_000_000)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# feed readers -> [{"url","title","price"}]
# --------------------------------------------------------------------------- #
def read_shopify(base: str, *, fetch: Callable = _fetch) -> List[Dict]:
    """Shopify's documented products.json, paginated."""
    out: List[Dict] = []
    seen_handles: set = set()
    page = 1
    while page <= 10 and len(out) < MAX_PRODUCTS:
        raw = fetch(f"{base}/products.json?limit=250&page={page}")
        if not raw:
            break
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except (ValueError, AttributeError):
            break
        products = data.get("products") or []
        if not products:
            break
        # A static file server (or a feed that ignores ?page) hands back the
        # same list forever. Stop as soon as a page adds nothing new.
        handles = {p.get("handle") for p in products}
        if handles and handles <= seen_handles:
            break
        seen_handles |= handles
        for p in products:
            handle = p.get("handle") or ""
            title = p.get("title") or ""
            price = None
            for v in (p.get("variants") or []):
                try:
                    price = float(v.get("price"))
                    break
                except (TypeError, ValueError):
                    continue
            if handle:
                out.append({"url": f"{base}/products/{handle}",
                            "title": title, "price": price})
        page += 1
    return out


_LOC = re.compile(rb"<loc>\s*([^<\s]+)\s*</loc>", re.I)


def read_sitemap(base: str, *, fetch: Callable = _fetch,
                 max_children: int = 8) -> List[Dict]:
    """Walk sitemap.xml / sitemap_index.xml and keep product-looking URLs."""
    seen: set = set()
    out: List[Dict] = []

    def urls_in(xml: bytes) -> List[str]:
        return [u.decode("utf-8", "replace") for u in _LOC.findall(xml or b"")]

    roots = [f"{base}/sitemap.xml", f"{base}/sitemap_index.xml"]
    pending: List[str] = []
    for root in roots:
        raw = fetch(root)
        if not raw:
            continue
        found = urls_in(raw)
        # A sitemap index points at more sitemaps rather than pages.
        children = [u for u in found if u.lower().endswith((".xml", ".xml.gz"))]
        if children:
            pending.extend(children[:max_children])
        else:
            pending.append(root)
            for u in found:
                if u not in seen:
                    seen.add(u)
                    out.append({"url": u, "title": "", "price": None})
        break

    for child in pending[:max_children]:
        if child in roots:
            continue
        raw = fetch(child)
        if not raw:
            continue
        for u in urls_in(raw):
            if u.lower().endswith((".xml", ".xml.gz")):
                continue
            if u not in seen:
                seen.add(u)
                out.append({"url": u, "title": "", "price": None})
        if len(out) >= MAX_PRODUCTS:
            break

    # Keep the ones that look like product pages, and derive a title from the
    # slug when the sitemap gave us no title.
    kept: List[Dict] = []
    for rec in out:
        path = urllib.parse.urlparse(rec["url"]).path.lower()
        if any(seg in path for seg in ("/product", "/products/", "/item/",
                                       "/p/", "/shop/", "/store/")):
            if not rec["title"]:
                slug = path.rstrip("/").rsplit("/", 1)[-1]
                rec["title"] = slug.replace("-", " ").replace("_", " ")
            kept.append(rec)
    return kept[:MAX_PRODUCTS]


READERS = {
    "shopify": [read_shopify, read_sitemap],
    "wix": [read_sitemap],
    "custom": [read_sitemap, read_shopify],
}


def read_catalog(store: Dict, *, fetch: Callable = _fetch) -> Tuple[List[Dict], str]:
    """Every product the storefront publishes. Returns (records, how)."""
    base = _norm_base(store.get("base_url", ""))
    if not base:
        return [], "no base URL set for this store"
    for reader in READERS.get(store.get("platform") or "custom", [read_sitemap]):
        try:
            got = reader(base, fetch=fetch)
        except Exception:
            got = []
        if got:
            return got, reader.__name__
    return [], "no public product feed found (tried products.json and sitemap.xml)"


# --------------------------------------------------------------------------- #
# matching
# --------------------------------------------------------------------------- #
def score_match(title: str, set_name: str, line_name: str,
                line_short: str = "") -> float:
    """0..1 confidence that `title` is this set's product line.

    Both halves must show up: a title matching the set but not the line (or the
    reverse) scores 0, which is what stops "Prismatic Evolutions Booster Box"
    being proposed for the ETB.
    """
    t = _tokens(title)
    if not t:
        return 0.0
    s_tok, l_tok = _tokens(set_name), _tokens(line_name)
    short = (line_short or "").lower().strip()

    set_hit = (len(s_tok & t) / len(s_tok)) if s_tok else 0.0
    line_hit = (len(l_tok & t) / len(l_tok)) if l_tok else 0.0
    if short and short in t and short not in _GENERIC_SHORT:
        line_hit = max(line_hit, 1.0)

    # Both halves must be STRONG. "Prismatic Evolutions Booster Box" shares
    # only the generic word "box" with "Elite Trainer Box" (line_hit 0.33) and
    # used to clear the threshold on the set half alone; requiring most of the
    # line's own words is what keeps the lines apart.
    if set_hit <= 0 or line_hit < 0.55:
        return 0.0
    return round(0.55 * set_hit + 0.45 * line_hit, 3)


def propose(store: Dict, wanted: Iterable[Tuple[Dict, Dict]], *,
            fetch: Callable = _fetch, threshold: float = 0.6,
            per_product: int = 3) -> Dict[str, Any]:
    """Find candidate URLs for each (set, line) pair on one storefront.

    `wanted` is an iterable of (set_dict, line_dict).
    Returns {"how", "scanned", "records":[...], "misses":[(set,line)], "error"}.
    Records are ready to hand to LinkIndex.put_many().
    """
    catalog, how = read_catalog(store, fetch=fetch)
    if not catalog:
        return {"how": how, "scanned": 0, "records": [], "misses": list(wanted),
                "error": how}

    # One entry per URL — a feed that repeats itself must not become duplicate
    # proposals.
    unique: Dict[str, Dict] = {}
    for prod in catalog:
        unique.setdefault(prod.get("url", ""), prod)
    catalog = [p for u, p in unique.items() if u]

    wanted = list(wanted)
    records: List[Dict] = []
    misses: List[Tuple[Dict, Dict]] = []
    for s, l in wanted:
        scored = []
        for prod in catalog:
            title = prod.get("title", "")
            sc = score_match(title, s.get("name", ""),
                             l.get("name", ""), l.get("short", ""))
            if sc < threshold:
                continue
            # Winner takes all: if another requested line fits this title
            # better, it belongs to that line, not this one.
            better = max(
                (score_match(title, s2.get("name", ""), l2.get("name", ""),
                             l2.get("short", ""))
                 for s2, l2 in wanted if l2.get("id") != l.get("id")),
                default=0.0)
            if better > sc:
                continue
            # An exact tie means the title fits both equally by score; prefer
            # the line whose own words are more fully present.
            own = _tokens(l.get("name", ""))
            mine = (len(own & _tokens(title)) / len(own)) if own else 0.0
            beaten = False
            for s2, l2 in wanted:
                if l2.get("id") == l.get("id"):
                    continue
                if abs(score_match(title, s2.get("name", ""), l2.get("name", ""),
                                   l2.get("short", "")) - sc) > 1e-9:
                    continue
                other = _tokens(l2.get("name", ""))
                if other and (len(other & _tokens(title)) / len(other)) > mine:
                    beaten = True
                    break
            if beaten:
                continue
            scored.append((sc, prod))
        if not scored:
            misses.append((s, l))
            continue
        scored.sort(key=lambda x: -x[0])
        for sc, prod in scored[:per_product]:
            records.append({
                "store_id": store.get("id", ""), "set_id": s.get("id", ""),
                "line_id": l.get("id", ""), "url": prod["url"],
                "title": prod.get("title", ""), "price": prod.get("price"),
                "score": sc, "source": how,
            })
    return {"how": how, "scanned": len(catalog), "records": records,
            "misses": misses, "error": ""}
