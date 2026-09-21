"""Search-results → product-page resolution.

The catalog's resolve_url() returns kind="search" when no exact listing is
saved for a set + line + store — a retailer search URL, not a product page.
A results page has no single price, seller or buy box, so the MSRP gate
correctly refuses it. This module is the bridge from that refusal to a real
product page:

    open the search results → find the ONE result that is this product →
    navigate to its dedicated product page → validate it.

The rules are fail-safe and deterministic — nothing here guesses:

* retailer-owned domains only (lookalikes never qualify);
* dedicated product-page URL patterns per retailer;
* the result must match the selected set AND the selected product line;
* zero matches refuse; a tie for best match refuses;
* after navigating, the final URL must still be a product page on the
  retailer's domain and its title must match the set + line.

Only after this returns ok=True does the runner proceed to the seller/MSRP
gate and checkout. A refusal here is a deliberate skip, not a transient
failure — it must not be retried blindly.
"""
from __future__ import annotations

import re
from html import unescape
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

# site key -> retailer-owned host suffixes
_HOSTS: Dict[str, Tuple[str, ...]] = {
    "target": ("target.com",),
    "walmart": ("walmart.com",),
    "bestbuy": ("bestbuy.com",),
    "pokemon_center": ("pokemoncenter.com",),
}

# Retailer search endpoints. A task URL matching one of these is a results
# page, not a product page.
_SEARCH_URL = (
    ("target", re.compile(r"^https?://(?:[a-z0-9-]+\.)?target\.com/s[/?]", re.I)),
    ("walmart", re.compile(r"^https?://(?:[a-z0-9-]+\.)?walmart\.com/search[/?]", re.I)),
    ("bestbuy", re.compile(r"^https?://(?:[a-z0-9-]+\.)?bestbuy\.com/site/searchpage\.jsp", re.I)),
    ("pokemon_center", re.compile(r"^https?://(?:[a-z0-9-]+\.)?pokemoncenter\.com/search", re.I)),
)

# Dedicated product-page URL shapes, per retailer.
_PDP = {
    "target": re.compile(r"/p/(?:[^/]+/)*-/A-\d+", re.I),
    "walmart": re.compile(r"/ip/(?:[^/]+/)*\d+", re.I),
    "bestbuy": re.compile(r"/site/(?!searchpage\.jsp)[^?#]+", re.I),
    "pokemon_center": re.compile(r"/products?/", re.I),
}

_A = re.compile(
    r"<a\b[^>]*\bhref\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
    re.I | re.S)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_H1 = re.compile(r"<h1\b[^>]*>(.*?)</h1>", re.I | re.S)


def _host(url: str) -> str:
    try:
        return (urlparse(url or "").hostname or "").lower()
    except Exception:
        return ""


def _site_for_host(host: str) -> str:
    for site, suffixes in _HOSTS.items():
        for sfx in suffixes:
            if host == sfx or host.endswith("." + sfx):
                return site
    return ""


def site_for_url(url: str) -> str:
    """Retailer site key for a URL, or "" when it is not a known retailer."""
    return _site_for_host(_host(url))


def is_search_url(url: str) -> bool:
    """True when the URL is a retailer search-results page."""
    url = (url or "").strip()
    if not url:
        return False
    return any(pat.search(url) for _, pat in _SEARCH_URL)


def is_pdp_url(url: str, site: str) -> bool:
    """True when the URL is a dedicated product page on the retailer's domain."""
    if not url or site not in _PDP:
        return False
    if _site_for_host(_host(url)) != site:
        return False
    return bool(_PDP[site].search(urlparse(url).path or ""))


def extract_candidates(html: str, site: str, base_url: str) -> List[Dict[str, str]]:
    """Every product-page link on a results page: [{"url", "text"}].

    Same retailer domain only, PDP-shaped URLs only, de-duplicated.
    """
    out: List[Dict[str, str]] = []
    seen = set()
    for m in _A.finditer(html or ""):
        href = unescape(m.group(1)).strip()
        if not href or href.startswith(("#", "javascript:")):
            continue
        url = urljoin(base_url or "", href).split("#")[0].strip()
        if not is_pdp_url(url, site) or url in seen:
            continue
        seen.add(url)
        text = _WS.sub(" ", _TAG.sub(" ", m.group(2))).strip()
        out.append({"url": url, "text": unescape(text)[:240]})
    return out


def _tokens(text: str) -> List[str]:
    return [t for t in re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).split()
            if len(t) > 2]


def pick_result(candidates: List[Dict[str, str]], *, set_name: str = "",
                line_name: str = "") -> Tuple[Optional[Dict[str, str]], str]:
    """The unique candidate matching this set + product line.

    Returns (candidate, "") or (None, reason). Refuses on zero matches and
    on ambiguity — a tie for best match is not a decision.
    """
    line_toks = _tokens(line_name)
    set_toks = _tokens(set_name)
    if not line_toks:
        return None, "no product-line name to match the results against"
    need_set = max(1, (len(set_toks) + 1) // 2)

    scored = []
    for c in candidates:
        hay = _tokens(c.get("text") or "") + _tokens(urlparse(c["url"]).path)
        hit_line = sum(1 for t in line_toks if t in hay)
        hit_set = sum(1 for t in set_toks if t in hay)
        if hit_line < len(line_toks):
            continue
        if hit_set < need_set:
            continue
        total = len(line_toks) + len(set_toks)
        score = (hit_line + hit_set) / total if total else 0.0
        scored.append((score, c))
    if not scored:
        want = f"{set_name} {line_name}".strip()
        return None, (f"no search result matched '{want}' "
                       f"({len(candidates)} product links seen)")
    scored.sort(key=lambda s: (s[0], s[1]["url"]), reverse=True)
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        want = f"{set_name} {line_name}".strip()
        return None, (f"ambiguous: several results match '{want}' equally "
                       "— refusing to guess")
    return scored[0][1], ""


def _page_title_text(html: str, title: str = "") -> str:
    parts = [title or ""]
    m = _TITLE.search(html or "")
    if m:
        parts.append(m.group(1))
    for m in _H1.finditer(html or ""):
        parts.append(m.group(1))
    text = _WS.sub(" ", _TAG.sub(" ", " ".join(parts))).strip()
    return unescape(text)


def validate_pdp(url: str, html: str, *, site: str, set_name: str = "",
                 line_name: str = "", title: str = "") -> Tuple[bool, str]:
    """The landed page really is this product's page at this retailer."""
    if not is_pdp_url(url, site):
        return False, f"landed URL is not a {site} product page: {url}"
    line_toks = _tokens(line_name)
    set_toks = _tokens(set_name)
    if line_toks:
        hay = _tokens(_page_title_text(html, title))
        hit_line = sum(1 for t in line_toks if t in hay)
        hit_set = sum(1 for t in set_toks if t in hay)
        need_set = max(1, (len(set_toks) + 1) // 2)
        if hit_line < len(line_toks) or hit_set < need_set:
            want = f"{set_name} {line_name}".strip()
            return False, (f"product page title does not match '{want}' "
                           "— wrong product")
    return True, ""


def resolve_search_to_pdp(page, search_url: str, *, site: str, set_name: str = "",
                          line_name: str = "", wait_ms: int = 2500) -> Dict:
    """Drive a live browser page from search results to the product page.

    `page` is a Playwright-ish page (goto / content / url / title /
    wait_for_timeout). Returns {"ok", "url", "title", "reason",
    "candidates"}. On ok=True the page is left ON the validated product
    page, ready for the normal navigate → gate → checkout flow.
    """
    if not is_search_url(search_url):
        return {"ok": False, "url": "", "reason": "not a retailer search URL",
                "candidates": 0}
    if site not in _HOSTS:
        return {"ok": False, "url": "",
                "reason": f"no product-page resolver for site '{site}'",
                "candidates": 0}
    try:
        page.goto(search_url, wait_until="domcontentloaded")
    except Exception as e:
        return {"ok": False, "url": "",
                "reason": f"search page did not load: {e}", "candidates": 0}
    try:
        page.wait_for_timeout(wait_ms)
    except Exception:
        pass
    try:
        html = page.content()
        current = page.url
    except Exception as e:
        return {"ok": False, "url": "",
                "reason": f"could not read the search results: {e}",
                "candidates": 0}

    cands = extract_candidates(html, site, current or search_url)
    pick, note = pick_result(cands, set_name=set_name, line_name=line_name)
    if pick is None:
        return {"ok": False, "url": "", "reason": note,
                "candidates": len(cands)}

    try:
        page.goto(pick["url"], wait_until="domcontentloaded")
    except Exception as e:
        return {"ok": False, "url": "",
                "reason": f"product page did not load: {e}",
                "candidates": len(cands)}
    try:
        page.wait_for_timeout(2000)
    except Exception:
        pass
    try:
        final = page.url
        title = page.title()
        body = page.content()
    except Exception as e:
        return {"ok": False, "url": "",
                "reason": f"could not read the product page: {e}",
                "candidates": len(cands)}

    ok, why = validate_pdp(final, body, site=site, set_name=set_name,
                           line_name=line_name, title=title)
    if not ok:
        return {"ok": False, "url": "", "reason": why,
                "candidates": len(cands)}
    return {"ok": True, "url": final, "title": title,
            "reason": f"search resolved to the product page: {final}",
            "candidates": len(cands)}
