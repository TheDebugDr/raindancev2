"""Discovered product links, cached per (store, set, line).

Discovery is slow and rate-limited, so every link a crawl finds is written here
and read back later — the retailer step's coverage numbers come from this index
plus the listings that already exist, so a store you crawled once keeps showing
what it can cover without going back out to the network.

An entry is a CANDIDATE until it is promoted into a real listing. Matching a
product title to a (set, line) is fuzzy, so nothing here is trusted blindly:
each entry carries the score and the title it matched, and the UI confirms
before a candidate becomes something the runner will open.

Stored under settings["link_index"] — a plain list, so it round-trips through
the settings merge untouched, same as products and tasks.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

KEY = "link_index"

# status values
CANDIDATE = "candidate"   # found by discovery, not yet confirmed
CONFIRMED = "confirmed"   # user accepted it (or it came from a listing)
REJECTED = "rejected"     # user said no — never propose it again


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def entry_id(store_id: str, set_id: str, line_id: str, url: str) -> str:
    return f"{store_id}|{set_id}|{line_id}|{url}"


class LinkIndex:
    def __init__(self, settings):
        self.settings = settings
        self.settings.data.setdefault(KEY, [])

    @property
    def items(self) -> List[Dict]:
        return self.settings.data[KEY]

    def save(self) -> None:
        self.settings.save()

    # -- writing ----------------------------------------------------------- #
    def put(self, *, store_id: str, set_id: str, line_id: str, url: str,
            title: str = "", price: Optional[float] = None, score: float = 0.0,
            status: str = CANDIDATE, source: str = "") -> Dict:
        """Insert or update one discovered link. Idempotent on the same URL.

        A REJECTED entry is never silently resurrected by a later crawl — the
        user's "no" outlives the cache.
        """
        eid = entry_id(store_id, set_id, line_id, url)
        for e in self.items:
            if e.get("id") == eid:
                if e.get("status") == REJECTED:
                    return e
                e.update(title=title or e.get("title", ""),
                         price=price if price is not None else e.get("price"),
                         score=max(float(score), float(e.get("score") or 0)),
                         source=source or e.get("source", ""),
                         seen=_now())
                self.save()
                return e
        rec = {"id": eid, "store_id": store_id, "set_id": set_id,
               "line_id": line_id, "url": url, "title": title, "price": price,
               "score": round(float(score), 3), "status": status,
               "source": source, "found": _now(), "seen": _now()}
        self.items.append(rec)
        self.save()
        return rec

    def put_many(self, records: List[Dict]) -> int:
        n = 0
        for r in records:
            before = len(self.items)
            self.put(**r)
            if len(self.items) != before:
                n += 1
        return n

    def set_status(self, eid: str, status: str) -> Optional[Dict]:
        for e in self.items:
            if e.get("id") == eid:
                e["status"] = status
                e["seen"] = _now()
                self.save()
                return e
        return None

    def forget_store(self, store_id: str) -> int:
        """Drop every cached link for a store (e.g. its base URL changed)."""
        before = len(self.items)
        self.settings.data[KEY] = [e for e in self.items
                                   if e.get("store_id") != store_id]
        self.save()
        return before - len(self.settings.data[KEY])

    # -- reading ----------------------------------------------------------- #
    def for_store(self, store_id: str, *, status: Optional[str] = None) -> List[Dict]:
        out = [e for e in self.items if e.get("store_id") == store_id]
        if status:
            out = [e for e in out if e.get("status") == status]
        return out

    def candidates(self, store_id: str, set_id: str, line_id: str) -> List[Dict]:
        """Proposals for one product at one store, best score first."""
        out = [e for e in self.items
               if e.get("store_id") == store_id and e.get("set_id") == set_id
               and e.get("line_id") == line_id and e.get("status") != REJECTED]
        return sorted(out, key=lambda e: -float(e.get("score") or 0))

    def best(self, store_id: str, set_id: str, line_id: str) -> Optional[Dict]:
        got = self.candidates(store_id, set_id, line_id)
        return got[0] if got else None

    def has_link(self, store_id: str, set_id: str, line_id: str) -> bool:
        return self.best(store_id, set_id, line_id) is not None

    def crawled_stores(self) -> List[str]:
        return sorted({e.get("store_id", "") for e in self.items if e.get("store_id")})

    def stats(self, store_id: str) -> Dict[str, int]:
        rows = self.for_store(store_id)
        return {
            "total": len(rows),
            "candidate": sum(1 for e in rows if e.get("status") == CANDIDATE),
            "confirmed": sum(1 for e in rows if e.get("status") == CONFIRMED),
            "rejected": sum(1 for e in rows if e.get("status") == REJECTED),
        }

    # -- promotion --------------------------------------------------------- #
    def promote(self, eid: str, *, catalog, store) -> Optional[Dict]:
        """Turn a confirmed link into a real listing the runner can open."""
        e = next((x for x in self.items if x.get("id") == eid), None)
        if e is None:
            return None
        existing = catalog.listings(store, set_id=e["set_id"],
                                    line_id=e["line_id"], store_id=e["store_id"])
        if existing:
            e["status"] = CONFIRMED
            self.save()
            return existing[0]
        product = catalog.add_listing(
            store, set_id=e["set_id"], line_id=e["line_id"],
            store_id=e["store_id"], url=e["url"])
        e["status"] = CONFIRMED
        self.save()
        return product
