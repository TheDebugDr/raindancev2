"""Keyless lookups against the public Pokémon TCG API (api.pokemontcg.io/v2).

WHAT THIS IS FOR
    Enrichment, nothing more. It fills in a set's logo, symbol, release date and
    PTCGO code so the catalog can show real art instead of a letter badge. The
    app is fully functional with this module returning nothing at all — every
    entry point here fails soft and NEVER raises into the UI.

WHY NO API KEY
    /v2/sets is public and unauthenticated. Rate limits are looser with a key,
    but we deliberately do not ask the user for one: this is a nice-to-have.

WHY THE RETRIES
    The public API is intermittently unhealthy — it answers a large share of
    requests with a fast HTTP 500/502 and then serves the identical request
    fine a moment later. A single-shot client would therefore report "no
    results" for sets that plainly exist. So a retryable server error is
    retried a couple of times, bounded by the caller's `timeout` budget: the
    call still returns within roughly `timeout` seconds no matter what.
    A 4xx, a malformed body or a DNS failure is NOT retried — those do not fix
    themselves.

QUERY SYNTAX (verified against the live API, not assumed)
    name:"Prismatic Evolutions"     exact-ish phrase match
    name:"*prisma*"                 substring, case-insensitive, partial tokens OK
    name:*prismatic evolutions*     500s — an UNQUOTED wildcard containing a
                                    space breaks the server's parser, so every
                                    wildcard we send is quoted.
    The substring form is a superset of the phrase form, so the default search
    issues ONE request using it and ranks exact matches to the top, rather than
    spending two round-trips on a flaky endpoint.

STDLIB ONLY — urllib + json. No new dependency.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

# Overridable at runtime — tests point this at an unroutable host to prove the
# module fails soft. URLs are built per call, so reassigning it takes effect.
API_BASE = "https://api.pokemontcg.io/v2"

USER_AGENT = "RainDance/1.0 (+catalog set enrichment)"

# Set names are few (~170 total), so one generous page always holds the whole
# result. That keeps the cache complete and independent of any caller's limit.
PAGE_SIZE = 250

# A fast typist must not be able to fire dozens of requests. Outbound calls are
# spaced by at least this many seconds, process-wide.
MIN_REQUEST_INTERVAL = 0.4

# Total attempts per logical lookup, including the first (see WHY THE RETRIES).
MAX_ATTEMPTS = 3

# Server-side hiccups worth another go. Anything else is taken at face value.
RETRY_STATUS = frozenset({500, 502, 503, 504})

# Characters that would break out of, or confuse, the q=name:"..." filter.
# Stripping the quote is what makes the filter injection-safe; stripping the
# wildcards means the caller cannot smuggle in a syntax we have not verified.
_UNSAFE = re.compile(r'[\"\\*?:()\[\]{}^~|<>\x00-\x1f]')
_WS = re.compile(r"\s+")
_ALNUM = re.compile(r"[^a-z0-9]+")

_lock = threading.Lock()
_last_request_at = 0.0

# Successful lookups only. Keyed by mode + normalized query / api id.
_CACHE: Dict[str, List[Dict[str, Any]]] = {}
_SET_CACHE: Dict[str, Optional[Dict[str, Any]]] = {}
_CACHE_MAX = 256


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def clean_query(text: str) -> str:
    """Strip anything that could break the q=name:"..." filter, collapse space."""
    return _WS.sub(" ", _UNSAFE.sub(" ", str(text or ""))).strip()


def normalize_name(text: str) -> str:
    """Fold a set name to letters+digits for tolerant comparison.

    "Scarlet & Violet" and "scarlet violet" both become "scarletviolet".
    """
    return _ALNUM.sub("", str(text or "").lower())


def _norm_date(raw: str) -> str:
    """The API dates are YYYY/MM/DD; the catalog stores YYYY-MM-DD."""
    s = str(raw or "").strip()
    if not s:
        return ""
    m = re.match(r"^(\d{4})[/-](\d{1,2})[/-](\d{1,2})", s)
    if not m:
        return s
    y, mo, d = m.groups()
    return f"{y}-{int(mo):02d}-{int(d):02d}"


def _normalize_set(raw: Dict[str, Any]) -> Dict[str, Any]:
    """One API set object -> the flat shape the catalog and UI consume."""
    images = raw.get("images") or {}
    if not isinstance(images, dict):
        images = {}
    return {
        "api_id": str(raw.get("id") or ""),
        "name": str(raw.get("name") or ""),
        "series": str(raw.get("series") or ""),
        "code": str(raw.get("ptcgoCode") or ""),
        "released": _norm_date(raw.get("releaseDate") or ""),
        "image": str(images.get("logo") or ""),
        "symbol": str(images.get("symbol") or ""),
    }


def _throttle(budget: float) -> None:
    """Space outbound calls process-wide. Waits at most MIN_REQUEST_INTERVAL.

    The wait is also capped by the caller's remaining time `budget`, so an
    explicitly tiny timeout keeps its promise instead of being padded out to
    the spacing interval. That only erodes the guard for timeouts shorter than
    MIN_REQUEST_INTERVAL, and such a call cannot load the API anyway — it
    aborts before a response could arrive.
    """
    global _last_request_at
    with _lock:
        wait = MIN_REQUEST_INTERVAL - (time.monotonic() - _last_request_at)
        if wait > 0:
            time.sleep(min(wait, MIN_REQUEST_INTERVAL, max(budget, 0.0)))
        _last_request_at = time.monotonic()


def _cache_put(cache: Dict, key: str, value: Any) -> None:
    if len(cache) >= _CACHE_MAX:
        try:                                   # drop the oldest inserted key
            del cache[next(iter(cache))]
        except (StopIteration, KeyError):      # pragma: no cover - racy trim
            cache.clear()
    cache[key] = value


def _get_json(path: str, params: Optional[Dict[str, Any]],
              timeout: float) -> Optional[Dict[str, Any]]:
    """GET and decode one endpoint. Returns None on ANY failure.

    Bounded by `timeout` overall, not per attempt: retries stop once the budget
    is spent, so the worst case stays roughly `timeout` seconds.
    """
    url = f"{API_BASE.rstrip('/')}/{path.lstrip('/')}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    deadline = time.monotonic() + max(float(timeout), 0.0)

    for attempt in range(1, MAX_ATTEMPTS + 1):
        remaining = deadline - time.monotonic()
        if attempt > 1 and remaining <= 0:
            break
        _throttle(max(remaining, 0.0))
        remaining = deadline - time.monotonic()
        # urllib treats timeout<=0 as "no timeout"; keep a positive floor and
        # never exceed what the caller allowed.
        sock_timeout = max(min(remaining, float(timeout)), 0.001)
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=sock_timeout) as resp:
                if getattr(resp, "status", 200) != 200:
                    log.warning("tcg_api: %s returned HTTP %s",
                                url, getattr(resp, "status", "?"))
                    return None
                body = resp.read(8_000_000)
            return json.loads(body.decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            if exc.code in RETRY_STATUS and attempt < MAX_ATTEMPTS:
                log.warning("tcg_api: HTTP %s from %s (attempt %d/%d), retrying",
                            exc.code, url, attempt, MAX_ATTEMPTS)
                continue
            log.warning("tcg_api: HTTP %s from %s — giving up", exc.code, url)
            return None
        except (urllib.error.URLError, OSError) as exc:
            # DNS failure, refused connection, timeout, no network at all.
            log.warning("tcg_api: cannot reach %s (%s)", url, exc)
            return None
        except (ValueError, TypeError) as exc:
            log.warning("tcg_api: malformed response from %s (%s)", url, exc)
            return None
    log.warning("tcg_api: no usable response from %s within %.3fs", url, timeout)
    return None


def _rank(rows: List[Dict[str, Any]], query: str) -> List[Dict[str, Any]]:
    """Exact name match first, then prefix, then the rest — each A-Z stable."""
    want = normalize_name(query)

    def key(row: Dict[str, Any]):
        name = normalize_name(row.get("name"))
        if want and name == want:
            tier = 0
        elif want and name.startswith(want):
            tier = 1
        else:
            tier = 2
        return (tier, row.get("name", ""))

    return sorted(rows, key=key)


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def search_sets(query: str, *, timeout: float = 10.0, limit: int = 20,
                exact: bool = False) -> List[Dict[str, Any]]:
    """Look up sets by name. Returns [] on any failure — never raises.

    Each row is {"api_id","name","series","code","released","image","symbol"}
    where `code` is the PTCGO code, `released` is YYYY-MM-DD, `image` is the
    set logo and `symbol` the small set symbol.

    By default this is a SUBSTRING search (`name:"*query*"`), which is what a
    type-ahead wants and which also contains the exact match; results are
    ranked so an exact name match comes first. Pass exact=True for the phrase
    filter `name:"query"` only.

    Safe to call on every keystroke: successful lookups are cached in-process
    by query, and outbound calls are spaced by MIN_REQUEST_INTERVAL seconds.
    """
    cleaned = clean_query(query)
    if not cleaned:
        return []
    try:
        limit = max(int(limit), 0)
    except (TypeError, ValueError):
        limit = 20
    if limit == 0:
        return []

    key = f"{'exact' if exact else 'sub'}:{cleaned.lower()}"
    with _lock:
        hit = _CACHE.get(key)
    if hit is not None:
        return [dict(r) for r in hit[:limit]]

    filt = f'name:"{cleaned}"' if exact else f'name:"*{cleaned}*"'
    payload = _get_json("sets", {"q": filt, "pageSize": PAGE_SIZE}, timeout)
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, list):
        log.warning("tcg_api: unexpected payload for %r", filt)
        return []

    rows = _rank([_normalize_set(r) for r in data if isinstance(r, dict)], cleaned)
    with _lock:
        _cache_put(_CACHE, key, rows)
    return [dict(r) for r in rows[:limit]]


def get_set(api_id: str, *, timeout: float = 10.0) -> Optional[Dict[str, Any]]:
    """One set by its API id (e.g. "sv8pt5"). None on any failure."""
    sid = clean_query(api_id).replace(" ", "")
    if not sid:
        return None
    with _lock:
        if sid in _SET_CACHE:
            got = _SET_CACHE[sid]
            return dict(got) if got else None

    payload = _get_json(f"sets/{urllib.parse.quote(sid, safe='')}", None, timeout)
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        log.warning("tcg_api: unexpected payload for set %r", sid)
        return None

    row = _normalize_set(data)
    with _lock:
        _cache_put(_SET_CACHE, sid, row)
    return dict(row)


def clear_cache() -> None:
    """Forget every cached lookup (tests, and a UI 'refresh' affordance)."""
    with _lock:
        _CACHE.clear()
        _SET_CACHE.clear()


def cache_size() -> int:
    """How many lookups are memoized right now."""
    with _lock:
        return len(_CACHE) + len(_SET_CACHE)
