"""Catalog: sets -> product lines -> per-store listings.

The app's product list is flat (one row = one URL). This adds the three axes the
UI needs on top of it, without changing how products are stored:

    Set          "Prismatic Evolutions"      settings["catalog"]["sets"]
    Line         "Elite Trainer Box"         settings["catalog"]["lines"]
    Store        your Shopify/Wix/custom     settings["stores"]
    Listing      one (set, line, store) URL  a normal product carrying
                                             set_id / line_id / store_id

Listings ARE products — same ProductStore, same monitoring, same run plan. The
three ids ride along as extra keys, which persist through save/load (lists are
replaced wholesale by the settings merge, so nothing strips them).

Per-store operational settings (proxy group, captcha mode, browser, selectors)
live in settings["retailer_configs"][store_id], which retailer_config.resolve()
already reads for ANY key — no whitelist. That is the existing per-site channel,
reused rather than reinvented.

Two extras ride on the same records:

    search_url   a per-store template containing {q}, so a (set, line) with no
                 discovered product URL still has somewhere to look. See
                 resolve_url() for the sharp edge: a search page is NOT a
                 listing and the runner will refuse it.
    image/symbol optional set art, filled from the public Pokémon TCG API by
                 enrich_set_from_api(). Absent means "unknown" — the UI falls
                 back to its letter/code badge. Nothing here needs a network.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus

# --------------------------------------------------------------------------- #
# Seed data. Purely a starting point — everything is editable in the UI.
# --------------------------------------------------------------------------- #

# accent is used to draw the set's placeholder tile until a real image is set.
DEFAULT_SETS: List[Dict[str, Any]] = [
    # --- Mega Evolution era (current) ---------------------------------- #
    {"id": "set_delta_reign",     "name": "Delta Reign",        "code": "DR",  "era": "Mega Evolution", "released": "2026-11-06", "accent": "#7A3B6B"},
    {"id": "set_30th",            "name": "30th Celebration",   "code": "30C", "era": "Mega Evolution", "released": "2026-09-16", "accent": "#B4894A"},
    {"id": "set_pitch_black",     "name": "Pitch Black",        "code": "PB",  "era": "Mega Evolution", "released": "2026-07-17", "accent": "#2E2A3B"},
    {"id": "set_chaos_rising",    "name": "Chaos Rising",       "code": "CR",  "era": "Mega Evolution", "released": "2026-05-22", "accent": "#8C2F39"},
    {"id": "set_perfect_order",   "name": "Perfect Order",      "code": "PO",  "era": "Mega Evolution", "released": "2026-03-27", "accent": "#1F6F6B"},
    {"id": "set_ascended_heroes", "name": "Ascended Heroes",    "code": "AH",  "era": "Mega Evolution", "released": "2026-01-30", "accent": "#B4894A"},
    {"id": "set_phantasmal",      "name": "Phantasmal Flames",  "code": "PF",  "era": "Mega Evolution", "released": "2025-11-14", "accent": "#5C3A6E"},
    {"id": "set_mega_evolution",  "name": "Mega Evolution",     "code": "ME",  "era": "Mega Evolution", "released": "2025-09-26", "accent": "#3B5BA5"},
    # --- Scarlet & Violet era ------------------------------------------ #
    {"id": "set_white_flare",     "name": "White Flare",        "code": "WHF", "era": "Scarlet & Violet", "released": "2025-07-18", "accent": "#C9C4BC"},
    {"id": "set_black_bolt",      "name": "Black Bolt",         "code": "BLB", "era": "Scarlet & Violet", "released": "2025-07-18", "accent": "#26262B"},
    {"id": "set_destined_rivals", "name": "Destined Rivals",    "code": "DRI", "era": "Scarlet & Violet", "released": "2025-05-30", "accent": "#9C4A2F"},
    {"id": "set_journey_together","name": "Journey Together",   "code": "JTG", "era": "Scarlet & Violet", "released": "2025-03-28", "accent": "#2F6FA5"},
    {"id": "set_prismatic",       "name": "Prismatic Evolutions","code":"PRE", "era": "Scarlet & Violet", "released": "2025-01-17", "accent": "#5B4B8A"},
    {"id": "set_surging_sparks",  "name": "Surging Sparks",     "code": "SSP", "era": "Scarlet & Violet", "released": "2024-11-08", "accent": "#C8A02A"},
    {"id": "set_stellar_crown",   "name": "Stellar Crown",      "code": "SCR", "era": "Scarlet & Violet", "released": "2024-09-13", "accent": "#2E7D6B"},
    {"id": "set_shrouded_fable",  "name": "Shrouded Fable",     "code": "SFA", "era": "Scarlet & Violet", "released": "2024-08-02", "accent": "#3E3357"},
    {"id": "set_twilight_masq",   "name": "Twilight Masquerade","code": "TWM", "era": "Scarlet & Violet", "released": "2024-05-24", "accent": "#6B3F7A"},
    {"id": "set_temporal_forces", "name": "Temporal Forces",    "code": "TEF", "era": "Scarlet & Violet", "released": "2024-03-22", "accent": "#2A6E8C"},
    {"id": "set_paldean_fates",   "name": "Paldean Fates",      "code": "PAF", "era": "Scarlet & Violet", "released": "2024-01-26", "accent": "#B08D57"},
    {"id": "set_paradox_rift",    "name": "Paradox Rift",       "code": "PAR", "era": "Scarlet & Violet", "released": "2023-11-03", "accent": "#7A4B8C"},
    {"id": "set_151",             "name": "151",                "code": "MEW", "era": "Scarlet & Violet", "released": "2023-09-22", "accent": "#C0392B"},
    {"id": "set_obsidian_flames", "name": "Obsidian Flames",    "code": "OBF", "era": "Scarlet & Violet", "released": "2023-08-11", "accent": "#37343A"},
    {"id": "set_paldea_evolved",  "name": "Paldea Evolved",     "code": "PAL", "era": "Scarlet & Violet", "released": "2023-06-09", "accent": "#2F8A5B"},
    {"id": "set_scarlet_violet",  "name": "Scarlet & Violet",   "code": "SVI", "era": "Scarlet & Violet", "released": "2023-03-31", "accent": "#A03050"},
]

# `msrp` seeds the pricing table so a listing on your own store can clear the
# MSRP gate; leave None to fall back to the built-in tier table.
DEFAULT_LINES: List[Dict[str, Any]] = [
    {"id": "line_etb",            "name": "Elite Trainer Box",       "short": "ETB",     "msrp": 59.99},
    {"id": "line_booster_bundle", "name": "Booster Bundle",          "short": "Bundle",  "msrp": 26.99},
    {"id": "line_spc",            "name": "Special Collection",      "short": "SPC",     "msrp": 29.99},
    {"id": "line_blister",        "name": "Blister Pack",            "short": "Blister", "msrp": 9.99},
    {"id": "line_booster_box",    "name": "Booster Box",             "short": "Box",     "msrp": 161.99},
    {"id": "line_upc",            "name": "Ultra Premium Collection", "short": "UPC",    "msrp": 119.99},
    {"id": "line_tin",            "name": "Tin",                     "short": "Tin",     "msrp": 24.99},
]

# Retailers the wizard offers out of the box. `base_url` is the public
# storefront root; it is what detect_seller() compares a listing URL against,
# so a lookalike host cannot inherit the store's trust. Edit or remove these in
# the Monitors tab like any other store.
# `search_url` is the storefront's own public search endpoint, with {q} standing
# in for the URL-encoded product query. It is a FALLBACK: it lands on a results
# page, not a product page, so it gives every product somewhere to look without
# pretending to be a buyable listing (see resolve_url).
DEFAULT_STORES: List[Dict[str, Any]] = [
    {"id": "store_pokemon_center", "name": "Pokémon Center",
     "platform": "custom", "base_url": "https://www.pokemoncenter.com", "enabled": True,
     "search_url": "https://www.pokemoncenter.com/search/{q}"},
    {"id": "store_target", "name": "Target",
     "platform": "custom", "base_url": "https://www.target.com", "enabled": True,
     "search_url": "https://www.target.com/s?searchTerm={q}"},
    {"id": "store_best_buy", "name": "Best Buy",
     "platform": "custom", "base_url": "https://www.bestbuy.com", "enabled": True,
     "search_url": "https://www.bestbuy.com/site/searchpage.jsp?st={q}"},
    {"id": "store_walmart", "name": "Walmart",
     "platform": "custom", "base_url": "https://www.walmart.com", "enabled": True,
     "search_url": "https://www.walmart.com/search?q={q}"},
]

# Seeded store id -> its default search template, for backfilling records that
# were saved before search_url existed.
DEFAULT_SEARCH_URLS: Dict[str, str] = {
    s["id"]: s["search_url"] for s in DEFAULT_STORES if s.get("search_url")
}

# Seeded store id -> the checkout handler registered for it in
# raindance/sites/__init__.py. These do NOT share a naming convention on
# purpose — "store_best_buy" vs the registry's "bestbuy" is not a typo to
# align, it is two different ids for two different concerns (the catalog
# entry's stable identity vs. the handler registry's key) that happen to
# look similar. Anything that needs a task's `site` to be a handler key
# (get_handler, retailer_config.resolve, evasion/proxy-group config — see
# execute_queue.materialize) must go through this, not use store_id raw.
# A custom, user-added store has no entry here and correctly falls through
# to GenericHandler — that is the intended behaviour, not a gap.
HANDLER_FOR_STORE: Dict[str, str] = {
    "store_pokemon_center": "pokemon_center",
    "store_target": "target",
    "store_best_buy": "bestbuy",
    "store_walmart": "walmart",
}


def handler_for_store(store_id: str) -> str:
    """The site-handler registry key for a catalog store id.

    Falls back to the store_id itself when there is no seeded mapping — a
    custom store's id will not match anything in the handler registry
    either, so `get_handler()` correctly lands on GenericHandler either way.
    """
    return HANDLER_FOR_STORE.get(store_id or "", store_id or "generic")

PLATFORMS = ("shopify", "wix", "custom")

_SLUG = re.compile(r"[^a-z0-9]+")


def slug(text: str, prefix: str = "") -> str:
    s = _SLUG.sub("_", (text or "").strip().lower()).strip("_") or "item"
    return f"{prefix}{s}"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class CatalogStore:
    """Sets, product lines and user-owned stores. Backed by Settings."""

    def __init__(self, settings):
        self.settings = settings
        cat = self.settings.data.setdefault("catalog", {})
        if not cat.get("sets"):
            cat["sets"] = [dict(s) for s in DEFAULT_SETS]
        if not cat.get("lines"):
            cat["lines"] = [dict(l) for l in DEFAULT_LINES]

        # Non-destructive top-up. Seeding only fires on an EMPTY list, so a
        # catalog saved before a default was added would never see it again —
        # an existing install stayed frozen at whatever shipped the day it was
        # first run. Add defaults the catalog has never seen, by id, and leave
        # every record already present exactly as the user has it (renamed,
        # re-priced, re-arted, or deliberately deleted-then-recreated).
        def _topup(existing, defaults):
            have = {str(r.get("id") or "") for r in existing}
            added = 0
            for d in defaults:
                if str(d.get("id") or "") not in have:
                    existing.append(dict(d))
                    added += 1
            return added

        _topup(cat["sets"], DEFAULT_SETS)
        _topup(cat["lines"], DEFAULT_LINES)
        if not self.settings.data.get("stores"):
            self.settings.data["stores"] = [
                dict(s, created=_now()) for s in DEFAULT_STORES
            ]
            rc = self.settings.data.setdefault("retailer_configs", {})
            for s in self.settings.data["stores"]:
                rc.setdefault(s["id"], {
                    "proxy_group": "default",
                    "captcha": {"mode": "manual"},
                    "browser": {"headless": False},
                    "site_params": {},
                })
        self._backfill_search_urls()

    def _backfill_search_urls(self) -> int:
        """Give pre-existing seeded stores the search template they predate.

        Non-destructive on purpose: a record that already carries the key is
        left alone, INCLUDING one the user deliberately blanked. Only a record
        missing the key entirely (saved before this field existed) is filled.
        Mutates in memory only — it persists with the next save(), matching how
        the seeding above already behaves, so merely constructing a
        CatalogStore never rewrites config.json.
        """
        n = 0
        for st in self.settings.data.get("stores") or []:
            if not isinstance(st, dict):
                continue
            default = DEFAULT_SEARCH_URLS.get(st.get("id") or "")
            if default and st.get("search_url", None) is None:
                st["search_url"] = default
                n += 1
        return n

    # -- raw access -------------------------------------------------------- #
    @property
    def _cat(self) -> Dict[str, Any]:
        return self.settings.data["catalog"]

    def sets(self) -> List[Dict]:
        return self._cat["sets"]

    def lines(self) -> List[Dict]:
        return self._cat["lines"]

    def stores(self) -> List[Dict]:
        return self.settings.data["stores"]

    def save(self) -> None:
        self.settings.save()

    # -- lookups ----------------------------------------------------------- #
    def get_set(self, sid: str) -> Optional[Dict]:
        return next((s for s in self.sets() if s["id"] == sid), None)

    def get_line(self, lid: str) -> Optional[Dict]:
        return next((l for l in self.lines() if l["id"] == lid), None)

    def get_store(self, stid: str) -> Optional[Dict]:
        return next((s for s in self.stores() if s["id"] == stid), None)

    def name_of(self, kind: str, ident: str) -> str:
        got = {"set": self.get_set, "line": self.get_line,
               "store": self.get_store}[kind](ident)
        return (got or {}).get("name", ident or "—")

    # -- mutation ---------------------------------------------------------- #
    def add_set(self, name: str, *, code: str = "", accent: str = "#5B4B8A",
                image: str = "", symbol: str = "", era: str = "",
                released: str = "") -> Dict:
        """Create a set.

        `image` is the set logo and `symbol` the small set icon; both optional,
        and seeded sets carry neither, so the UI falls back to its letter badge.
        `era` and `released` complete the seeded-set schema, so a set built from
        an API lookup keeps its series and release date instead of dropping them.
        Every one of these is optional — add_set(name) alone still works.
        """
        rec = {"id": slug(name, "set_"), "name": name.strip(),
               "code": (code or name[:3]).upper(), "accent": accent,
               "image": image, "symbol": symbol, "era": era,
               "released": released, "created": _now()}
        self.sets().append(rec)
        self.save()
        return rec

    def add_line(self, name: str, *, short: str = "", msrp: Optional[float] = None) -> Dict:
        rec = {"id": slug(name, "line_"), "name": name.strip(),
               "short": short or name[:6], "msrp": msrp}
        self.lines().append(rec)
        self.save()
        return rec

    def add_store(self, name: str, *, platform: str = "custom", base_url: str = "",
                  enabled: bool = True, search_url: str = "") -> Dict:
        """Create a store. `search_url` is a template containing {q}; leave it
        blank and the store simply has no search fallback."""
        platform = platform if platform in PLATFORMS else "custom"
        rec = {"id": slug(name, "store_"), "name": name.strip(),
               "platform": platform, "base_url": base_url.strip(),
               "enabled": bool(enabled), "search_url": (search_url or "").strip(),
               "created": _now()}
        self.stores().append(rec)
        # Give the store its own operational config block. retailer_config
        # resolves ANY key, so this is the per-store settings channel.
        rc = self.settings.data.setdefault("retailer_configs", {})
        rc.setdefault(rec["id"], {
            "proxy_group": "default",
            "captcha": {"mode": "manual"},
            "browser": {"headless": False},
            "site_params": {},
        })
        self.save()
        return rec

    def update_store(self, stid: str, **fields) -> Optional[Dict]:
        """Patch any field on a store, `search_url` included.

        Every key is passed straight through as before; search_url is merely
        trimmed, so a pasted template with stray whitespace still resolves.
        """
        st = self.get_store(stid)
        if st is None:
            return None
        if "search_url" in fields:
            fields = dict(fields)
            fields["search_url"] = str(fields.get("search_url") or "").strip()
        st.update(fields)
        self.save()
        return st

    def remove(self, kind: str, ident: str) -> None:
        coll = {"set": self.sets, "line": self.lines, "store": self.stores}[kind]()
        for i, rec in enumerate(coll):
            if rec["id"] == ident:
                coll.pop(i)
                break
        self.save()

    # -- listings (products carrying the three ids) ------------------------ #
    @staticmethod
    def is_listing(product: Dict) -> bool:
        return bool(product.get("set_id") and product.get("line_id"))

    @staticmethod
    def filter_listings(products, *, set_id: str = "", line_id: str = "",
                        store_id: str = "") -> List[Dict]:
        """The listing filter, over any product sequence.

        listings() and resolve_url() both go through this so the definition of
        "a listing for this set+line+store" cannot drift between them.
        Returns the live dicts, not copies.
        """
        out = []
        for p in products or []:
            if set_id and p.get("set_id") != set_id:
                continue
            if line_id and p.get("line_id") != line_id:
                continue
            if store_id and p.get("store_id") != store_id:
                continue
            if CatalogStore.is_listing(p):
                out.append(p)
        return out

    def _products(self, store=None) -> List[Dict]:
        """Products from a ProductStore if given, else straight from settings.

        Both are the same underlying list; the fallback is what lets
        resolve_url() and product_image() work without a ProductStore handle.
        """
        if store is not None:
            return list(getattr(store, "items", None) or [])
        return list(self.settings.data.get("products") or [])

    def listings(self, store, *, set_id: str = "", line_id: str = "",
                 store_id: str = "") -> List[Dict]:
        """Products filtered by any combination of the three axes."""
        return self.filter_listings(getattr(store, "items", None) or [],
                                    set_id=set_id, line_id=line_id,
                                    store_id=store_id)

    def lines_in_set(self, store, set_id: str) -> List[Dict]:
        """Product lines that actually have at least one listing in this set."""
        have = {p.get("line_id") for p in self.listings(store, set_id=set_id)}
        return [l for l in self.lines() if l["id"] in have]

    def counts_for_set(self, store, set_id: str) -> Dict[str, int]:
        ls = self.listings(store, set_id=set_id)
        return {"listings": len(ls),
                "lines": len({p.get("line_id") for p in ls}),
                "stores": len({p.get("store_id") for p in ls})}

    def add_listing(self, store, *, set_id: str, line_id: str, store_id: str,
                    url: str, frequency: str = "5s", priority: str = "high",
                    price_threshold: Optional[float] = None,
                    image: str = "") -> Dict:
        """Create a product tagged with the three catalog ids.

        `image` is optional per-listing art; leave it blank and product_image()
        falls back to the set's logo.
        """
        s, l, st = self.get_set(set_id), self.get_line(line_id), self.get_store(store_id)
        name = f"{(s or {}).get('name','?')} — {(l or {}).get('name','?')}"
        thr = price_threshold
        if thr is None and l is not None and l.get("msrp"):
            thr = float(l["msrp"])
        p = store.add(name=name, url=url.strip(), price_threshold=thr,
                      frequency=frequency, priority=priority,
                      image=(str(image).strip() or None),
                      site=(st or {}).get("id") or None)
        store.update(p["id"], set_id=set_id, line_id=line_id, store_id=store_id,
                     store_name=(st or {}).get("name", ""))
        return store.get(p["id"]) or p

    # -- urls -------------------------------------------------------------- #
    def product_query(self, set_id: str, line_id: str) -> str:
        """The human search string for a product, e.g.

            "Prismatic Evolutions Elite Trainer Box"

        Set name plus the line's full name. Missing halves are dropped rather
        than rendered as "?", so an orphaned id degrades to the half we know.
        """
        s = self.get_set(set_id) or {}
        l = self.get_line(line_id) or {}
        parts = [str(s.get("name") or "").strip(), str(l.get("name") or "").strip()]
        return " ".join(x for x in parts if x)

    def search_url_for(self, store_id: str, set_id: str, line_id: str) -> str:
        """This store's search template with {q} filled in, or "".

        {q} is replaced with the URL-encoded product_query (quote_plus, so a
        space becomes "+"). Returns "" when the store has no template, or when
        the set/line are unknown and there is therefore nothing to search for.
        """
        st = self.get_store(store_id) or {}
        tpl = str(st.get("search_url") or "").strip()
        if not tpl:
            return ""
        q = self.product_query(set_id, line_id)
        if not q:
            return ""
        return tpl.replace("{q}", quote_plus(q))

    def resolve_url(self, set_id: str, line_id: str, store_id: str,
                    *, store=None) -> Dict[str, Any]:
        """Where to send someone for this product at this store.

        Returns {"url": str, "kind": "listing"|"search"|"", "product_id": str|None}

            kind="listing"  a real product URL — an existing listing for this
                            exact set+line+store, matched the way listings()
                            filters. This ALWAYS wins when one exists.
            kind="search"   the store's search template, filled in.
            kind=""         neither exists; url is "" and there is nothing to open.

        READ THIS BEFORE WIRING IT TO A BUY BUTTON. A kind="search" url opens a
        SEARCH RESULTS PAGE, not a product page. The runner's MSRP/seller gate
        will refuse it, and that refusal is correct, not a bug to work around:
        a results page has no single price, no seller and no buy box, so
        nothing about it can be verified as an in-stock retail listing. The
        search fallback exists so every product has SOMEWHERE TO LOOK — a
        human-facing "go find it" link, and a starting point for discovery —
        not so every product is purchasable. Only a kind="listing" url is a
        candidate for checkout.

        `store` is an optional ProductStore; without it the products are read
        straight from settings, which is the same list.
        """
        rows = self.filter_listings(self._products(store), set_id=set_id,
                                    line_id=line_id, store_id=store_id)
        with_url = [p for p in rows if str(p.get("url") or "").strip()]
        if with_url:
            p = with_url[0]
            return {"url": str(p.get("url") or "").strip(), "kind": "listing",
                    "product_id": p.get("id")}
        search = self.search_url_for(store_id, set_id, line_id)
        if search:
            return {"url": search, "kind": "search", "product_id": None}
        return {"url": "", "kind": "", "product_id": None}

    # -- images ------------------------------------------------------------ #
    def set_image(self, set_id: str) -> str:
        """The set's logo URL, or "" when unknown (draw the letter badge)."""
        return str((self.get_set(set_id) or {}).get("image") or "").strip()

    def set_symbol(self, set_id: str) -> str:
        """The set's small symbol URL, or "" when unknown."""
        return str((self.get_set(set_id) or {}).get("symbol") or "").strip()

    def product_image(self, set_id: str, line_id: str, *, store=None) -> str:
        """Art for one product: its own image if a listing carries one, else
        the set's logo, else "" — at which point the UI draws its badge."""
        for p in self.filter_listings(self._products(store), set_id=set_id,
                                      line_id=line_id):
            own = str(p.get("image") or "").strip()
            if own:
                return own
        return self.set_image(set_id)

    # -- enrichment (optional, network, always fails soft) ------------------ #
    def enrich_set_from_api(self, set_id: str, *, api_id: Optional[str] = None) -> bool:
        """Fill a set's blanks from the public Pokémon TCG API.

        Looks the set up by its exact name, or by `api_id` when you already
        know it, and fills image / symbol / released / code ONLY where the
        record is currently empty. A value the user typed is never overwritten.
        Returns True if anything changed.

        Without an explicit api_id the name must match exactly (ignoring case
        and punctuation). A fuzzy near-match is deliberately refused: hanging
        the wrong set's artwork on a set is worse than showing no artwork, and
        the catalog seeds unreleased sets the API has never heard of.

        Fails soft — no network, a timeout or a bad response all return False.
        """
        rec = self.get_set(set_id)
        if rec is None:
            return False
        try:
            from raindance.core import tcg_api
        except Exception:                             # noqa: BLE001
            return False

        got = None
        try:
            if api_id:
                got = tcg_api.get_set(api_id)
            else:
                want = str(rec.get("name") or "").strip()
                if not want:
                    return False
                target = tcg_api.normalize_name(want)
                for cand in tcg_api.search_sets(want):
                    if tcg_api.normalize_name(cand.get("name")) == target:
                        got = cand
                        break
        except Exception:                             # noqa: BLE001
            return False                              # enrichment never raises
        if not got:
            return False

        changed = False
        for field in ("image", "symbol", "released", "code"):
            new = str(got.get(field) or "").strip()
            if new and not str(rec.get(field) or "").strip():
                rec[field] = new
                changed = True
        if changed:
            if got.get("api_id"):
                rec["api_id"] = got["api_id"]
            self.save()
        return changed

    def backfill_images(self, limit: Optional[int] = None) -> int:
        """Enrich every set that has no image yet. Returns how many were filled.

        `limit` caps how many sets are LOOKED UP (i.e. bounds the outbound
        calls), not how many succeed. Safe with no network: every lookup fails
        soft, so an unreachable API just returns 0.
        """
        try:
            cap = None if limit is None else max(int(limit), 0)
        except (TypeError, ValueError):
            cap = None
        filled = 0
        tried = 0
        for s in list(self.sets()):
            if str(s.get("image") or "").strip():
                continue
            if cap is not None and tried >= cap:
                break
            tried += 1
            try:
                if self.enrich_set_from_api(s.get("id") or ""):
                    filled += 1
            except Exception:                         # noqa: BLE001
                continue
        return filled

    # -- pricing ----------------------------------------------------------- #
    def sync_msrp_table(self) -> int:
        """Publish line MSRPs into settings["pricing"]["msrp_table"].

        msrp.py already reads that table, so a listing on your own store can
        clear the MSRP gate instead of stalling at in_stock_unverified.
        Returns the number of rows written.
        """
        rows = []
        for l in self.lines():
            if l.get("msrp"):
                rows.append({"type": l["id"], "msrp": float(l["msrp"]),
                             "pattern": re.escape(l["name"].lower())})
        pricing = self.settings.data.setdefault("pricing", {})
        pricing["msrp_table"] = rows
        self.save()
        return len(rows)
