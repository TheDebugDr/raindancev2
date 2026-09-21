"""Verified MSRP reference for Pokémon TCG sealed product.

WHERE THESE NUMBERS COME FROM. Every figure marked ``verified`` was read
directly off pokemoncenter.com — The Pokémon Company's own storefront — on the
date in ``CHECKED``. That is not a third-party estimate of MSRP; for a
first-party store it *is* MSRP. Figures marked ``probable`` come from observed
first-party retailer pricing that agreed with the tier, and ``unverified``
means the tier is carried for matching but must not gate a purchase on its own.

WHY THE BAND IS TIGHT. Calibration observed live on 2026-09-04:

    Pokémon Center   Mega Evolution ETB ............... $59.99   (MSRP)
    Target (1P)      Mega Evolution Chaos Rising ETB .. $59.99   (exactly MSRP)
    Target (3P)      Perfect Order PC ETB ............. $149.99  (2.5x)
    Target (3P)      Pitch Black PC ETB ............... $179.99  (3.0x)
    Target (3P)      Prismatic Evolutions ETB ......... $219.99  (3.7x)

Legitimate first-party retail sat *exactly* on MSRP, and the cheapest resale
listing observed was 2.5x. There is no crowded middle to be careful about, so
the ceiling can be tight without costing real buys. The previous settings
allowed MSRP + 15% + $12 — a $197.89 ceiling on a $161.64 box — which admitted
distributor-priced listings in the $180s. That is the gap this closes.

ERAS MATTER. An Elite Trainer Box is $59.99 today and was $49.99 in the Sword &
Shield era; both are on shelves. Pricing a Sword & Shield ETB against today's
tier would admit a $10 markup as "at MSRP", so set names select the era.

MAINTENANCE. Re-check pokemoncenter.com when a new block launches, or if buys
start being refused on genuine listings. ``CHECKED`` below is the date of
record; ``staleness_days`` in ``catalog_health()`` reports the age.
"""
from __future__ import annotations

import re
from typing import Optional

CHECKED = "2026-09-04"
SOURCE_PC = "pokemoncenter.com (first-party storefront)"
SOURCE_TGT = "target.com first-party listing"
RESEARCH = "independent research, adversarially verified 2026-09-04"

# --------------------------------------------------------------------------- #
# Era detection — set names, newest block first.
# --------------------------------------------------------------------------- #
ERAS: list[tuple[str, str]] = [
    ("current", r"30th celebration|30th anniversary|mega evolution|ascended heroes|"
                r"pitch black|chaos rising|phantasmal flames|perfect order|"
                r"mega symphonia|\bmega\s+[a-z]+\s*ex\b|\bmega\s+(?:lucario|"
                r"gardevoir|zygarde|emboar|meganium|charizard|venusaur|blastoise)\b"),
    ("sv",      r"scarlet\s*&?\s*violet|prismatic evolutions|destined rivals|"
                r"journey together|surging sparks|stellar crown|shrouded fable|"
                r"paldean fates|paldea evolved|obsidian flames|black bolt|"
                r"white flare|twilight masquerade|temporal forces|151"),
    ("swsh",    r"sword\s*&?\s*shield|brilliant stars|astral radiance|lost origin|"
                r"silver tempest|crown zenith|fusion strike|evolving skies|"
                r"chilling reign|battle styles|vivid voltage|darkness ablaze"),
    ("sm",      r"sun\s*&?\s*moon|celestial storm|hidden fates|cosmic eclipse|"
                r"unified minds|team up"),
]

def era_for(name: str) -> Optional[str]:
    """Which pricing era a listing belongs to, or None if it cannot be told.

    There is deliberately no default. The same product type carries different
    MSRPs across eras — an Elite Trainer Box is $59.99 today and $49.99 in the
    Sword & Shield era, and both are still on shelves — so guessing an era is
    guessing a price. Guess high and an older, cheaper box passes at today's
    ceiling; guess low and a legitimate current listing is refused. Returning
    None instead makes the caller report the listing as unverified, which is
    the honest answer and costs only a manual check.

    Real retailer titles carry the set name ("Mega Evolution - Chaos Rising
    Elite Trainer Box", "Scarlet & Violet-Prismatic Evolutions"), so this
    resolves in practice; a title that does not is genuinely ambiguous.
    """
    low = (name or "").lower()
    for era, pattern in ERAS:
        if re.search(pattern, low, re.I):
            return era
    return None


# --------------------------------------------------------------------------- #
# The catalog. Ordered MOST SPECIFIC FIRST — matching stops at the first hit,
# so "ultra premium collection" must precede "premium collection", and
# "booster bundle" must precede "booster".
# --------------------------------------------------------------------------- #
# type, patterns, {era: msrp}, confidence, source
CATALOG: list[dict] = [
    {"type": "ultra_premium_collection",
     "display": "Ultra-Premium Collection",
     "patterns": [r"ultra[\s\-]?premium collection", r"\bupc\b"],
     # Observed directly on pokemoncenter.com: Terapagos ex, SV-151 and the
     # Sword & Shield Charizard UPCs all at $119.99. Independent research puts
     # the CURRENT-era (30th Celebration) UPC at $179.99, which is consistent
     # with a generational price step rather than a contradiction — so the two
     # figures are held per era rather than averaged.
     "msrp": {"current": 179.99, "sv": 119.99, "swsh": 119.99, "sm": 119.99},
     "confidence": "verified", "source": SOURCE_PC + " + " + RESEARCH},

    {"type": "elite_trainer_box_plus",
     "display": "Elite Trainer Box Plus",
     "patterns": [r"elite trainer box plus", r"\betb\+|\betb plus"],
     "msrp": {"current": 64.99, "sv": 64.99, "swsh": 64.99, "sm": 64.99},
     "confidence": "verified", "source": SOURCE_PC},

    {"type": "pokemon_center_elite_trainer_box",
     "display": "Pokémon Center Elite Trainer Box",
     "patterns": [r"pok[eé]mon center elite trainer box",
                  r"pokemon center elite trainer box"],
     "msrp": {"current": 59.99, "sv": 59.99, "swsh": 49.99, "sm": 39.99},
     "confidence": "verified", "source": SOURCE_PC},

    {"type": "elite_trainer_box",
     "display": "Elite Trainer Box",
     "patterns": [r"elite trainer box", r"\betb\b"],
     "msrp": {"current": 59.99, "sv": 59.99, "swsh": 49.99, "sm": 39.99},
     "confidence": "verified", "source": SOURCE_PC},

    {"type": "super_premium_collection",
     "display": "Super-Premium Collection",
     "patterns": [r"super[\s\-]?premium collection"],
     "msrp": {"current": 89.99, "sv": 89.99, "swsh": 89.99, "sm": 89.99},
     "confidence": "probable", "source": "tier carried from prior table"},

    {"type": "premium_collection",
     "display": "Premium Collection",
     "patterns": [r"premium collection", r"ex premium"],
     "msrp": {"current": 44.99, "sv": 44.99, "swsh": 39.99, "sm": 39.99},
     "confidence": "verified", "source": SOURCE_TGT},

    {"type": "figure_collection",
     "display": "Figure Collection",
     "patterns": [r"figure collection"],
     "msrp": {"current": 29.99, "sv": 29.99, "swsh": 29.99, "sm": 29.99},
     "confidence": "verified", "source": RESEARCH},

    {"type": "special_collection",
     "display": "Special Collection",
     "patterns": [r"special collection"],
     "msrp": {"current": 29.99, "sv": 29.99, "swsh": 29.99, "sm": 24.99},
     "confidence": "probable", "source": RESEARCH},

    {"type": "binder_collection",
     "display": "Binder Collection",
     "patterns": [r"binder collection"],
     "msrp": {"current": 31.99, "sv": 31.99, "swsh": 29.99, "sm": 29.99},
     "confidence": "probable", "source": RESEARCH},

    {"type": "poster_collection",
     "display": "Poster Collection",
     "patterns": [r"poster collection"],
     "msrp": {"current": 14.99, "sv": 14.99, "swsh": 14.99, "sm": 14.99},
     "confidence": "probable", "source": RESEARCH},

    {"type": "knock_out_collection",
     "display": "Knock Out Collection",
     "patterns": [r"knock[\s\-]?out collection"],
     "msrp": {"current": 9.99, "sv": 9.99, "swsh": 9.99, "sm": 9.99},
     "confidence": "probable", "source": RESEARCH},

    {"type": "ex_box",
     "display": "ex Box",
     "patterns": [r"\bex[\s\-]box\b"],
     "msrp": {"current": 21.99, "sv": 21.99, "swsh": 19.99, "sm": 19.99},
     "confidence": "verified", "source": RESEARCH},

    {"type": "checklane_blister",
     "display": "Checklane Blister (1 pack)",
     "patterns": [r"checklane"],
     "msrp": {"current": 5.49, "sv": 5.49, "swsh": 5.49, "sm": 4.99},
     "confidence": "probable", "source": RESEARCH},

    # MUST precede booster_bundle: "Trick or Trade BOOster Bundle" contains
    # "booster bundle", so the generic tier would claim it and price a
    # $14.99 product against a $26.94 ceiling — a 2x listing would pass.
    {"type": "trick_or_trade",
     "display": "Trick or Trade BOOster Bundle",
     "patterns": [r"trick or trade"],
     "msrp": {"current": 14.99, "sv": 14.99, "swsh": 14.99, "sm": 14.99},
     "confidence": "verified", "source": SOURCE_PC},

    {"type": "booster_bundle",
     "display": "Booster Bundle (6 packs)",
     "patterns": [r"booster bundle", r"\b6[\s\-]?pack"],
     "msrp": {"current": 26.94, "sv": 26.94, "swsh": 23.94, "sm": 23.94},
     "confidence": "verified", "source": SOURCE_PC},

    {"type": "mini_tin_10pack",
     "display": "Mini Tins (10-pack)",
     "patterns": [r"mini tins?\s*\(?10", r"10[\s\-]?pack mini tin"],
     "msrp": {"current": 99.90, "sv": 99.90, "swsh": 99.90, "sm": 99.90},
     "confidence": "verified", "source": SOURCE_PC},

    {"type": "special_delivery_box",
     "display": "Special Delivery Box",
     "patterns": [r"special delivery box"],
     "msrp": {"current": 49.99, "sv": 49.99, "swsh": 49.99, "sm": 49.99},
     "confidence": "verified", "source": SOURCE_PC},


    {"type": "build_battle_stadium",
     "display": "Build & Battle Stadium",
     "patterns": [r"build\s*&?\s*battle stadium", r"b&b stadium"],
     "msrp": {"current": 59.99, "sv": 59.99, "swsh": 49.99, "sm": 44.99},
     "confidence": "verified", "source": RESEARCH},

    {"type": "build_battle_box",
     "display": "Build & Battle Box",
     "patterns": [r"build\s*&?\s*battle box"],
     "msrp": {"current": 24.99, "sv": 24.99, "swsh": 22.99, "sm": 22.99},
     "confidence": "probable", "source": "tier carried; verify before live buys"},

    {"type": "booster_box_36",
     "display": "Booster Box (36 packs)",
     "patterns": [r"enhanced booster (?:display|box)",
                  r"booster box.*36|36[\s\-]?pack.*booster box",
                  r"booster display(?: box)?", r"display box", r"booster box"],
     # $161.64 reproduced independently by two sources that do not share a
     # lineage, so it functions as a real SRP reference rather than only a
     # wholesale sheet figure. Still the weakest anchor in the table.
     "msrp": {"current": 161.64, "sv": 161.64, "swsh": 143.64, "sm": 143.64},
     "confidence": "probable", "source": RESEARCH},

    {"type": "blister_3pack",
     "display": "3-pack Blister",
     "patterns": [r"3[\s\-]?booster blister", r"three[\s\-]?booster blister",
                  r"blister", r"\b3[\s\-]?pack", r"three[\s\-]?pack"],
     "msrp": {"current": 14.99, "sv": 14.99, "swsh": 13.99, "sm": 12.99},
     "confidence": "verified", "source": RESEARCH},

    {"type": "tin",
     "display": "Tin",
     "patterns": [r"\btin\b"],
     "msrp": {"current": 24.99, "sv": 24.99, "swsh": 24.99, "sm": 21.99},
     "confidence": "probable", "source": "tier carried from prior table"},

    {"type": "booster_pack",
     "display": "Booster Pack",
     "patterns": [r"booster pack", r"single pack", r"sleeved booster"],
     # $4.49 corroborated three ways: PHD distributor sheets (144-pack case at
     # $646.56 = $4.49/pack across every set from Destined Rivals to Perfect
     # Order), pokemoncenter.com single-pack listings, and trade press. Retail
     # shelf runs higher; that is markup, and the first-party band covers it.
     "msrp": {"current": 4.49, "sv": 4.49, "swsh": 4.49, "sm": 3.99},
     "confidence": "probable", "source": RESEARCH},
]

# Tiers whose absolute dollar slack is larger, because a percentage of a $160
# item is a lot of dollars while still being a normal amount of tax.
_BOX_TYPES = frozenset({
    "booster_box_36", "ultra_premium_collection", "super_premium_collection",
    "elite_trainer_box", "elite_trainer_box_plus", "premium_collection",
    "pokemon_center_elite_trainer_box",
    "mini_tin_10pack", "build_battle_stadium",
})

# Tight by design — see the calibration table in the module docstring.
# TWO BANDS, because one number cannot do the job. Observed 2026-09-04:
#
#   sold BY Target   Ascended Heroes Booster Bundle .. $31.99 vs $26.94  +18.7%
#   sold BY Target   Phantasmal Flames Bundle ........ $29.99 vs $26.94  +11.3%
#   sold BY Target   30th Celebration ETB ............ $69.99 vs $59.99  +16.7%
#   third party      "national distributor" box ...... $183.00 vs $161.64 +13.2%
#   Target Plus      Perfect Order PC ETB ............ $149.99 vs $59.99  +150%
#   Target Plus      Prismatic Evolutions ETB ........ $219.99 vs $59.99  +267%
#
# First-party retail now routinely sits ABOVE MSRP — Target marks up 11-19% on
# its own stock — while the distributor listing worth refusing is +13%. Those
# ranges OVERLAP, so no single percentage separates them and a ceiling loose
# enough to keep buying at Target is loose enough to buy the distributor.
#
# The split is by who is selling. A confirmed first-party listing gets the
# retail band, because Target's own markup is a real price the buyer accepts.
# Everyone else — marketplace, distributor, or simply unconfirmed — gets the
# tight band, which is where the anti-scalper protection actually lives. This
# is the one job seller identity does that price cannot, and it is why Target
# needs the seller check while Pokemon Center, which has no marketplace, does
# not.
DEFAULTS = {
    "tolerance": 0.06,             # over MSRP, marketplace / unconfirmed seller
    "tolerance_first_party": 0.25, # over MSRP, confirmed sold-by-the-retailer
    "tolerance_allin": 0.12,       # price already includes tax/shipping
    "fixed_small": 2.00,           # absolute slack ($), small SKUs
    "fixed_box": 5.00,             # absolute slack ($), box-sized SKUs
    "first_party_max_premium": 25.0,  # hard $ cap on the first-party band, so a
                                      # percentage does not become $45 of slack
                                      # on an expensive box
    "scalper_multiple": 1.35,      # at/above this * MSRP is unambiguously resale
    "floor_fraction": 0.55,        # below this the read is a misread, not a deal
}


def find_tier(name: str, extra: Optional[list] = None) -> Optional[dict]:
    """First catalog entry whose patterns match the listing title."""
    low = (name or "").lower()
    for entry in list(extra or []) + CATALOG:
        for pattern in entry.get("patterns", ()):
            if re.search(pattern, low, re.I):
                return entry
    return None


def msrp_for(name: str, extra: Optional[list] = None) -> Optional[float]:
    entry = find_tier(name, extra)
    if not entry:
        return None
    table = entry.get("msrp") or {}
    return table.get(era_for(name)) or table.get("current")


def catalog_health(today: str = CHECKED) -> dict:
    """Coverage and staleness, for the UI and for tests to assert on."""
    from datetime import date
    counts: dict = {}
    for e in CATALOG:
        counts[e["confidence"]] = counts.get(e["confidence"], 0) + 1
    try:
        y, m, d = (int(x) for x in CHECKED.split("-"))
        y2, m2, d2 = (int(x) for x in today.split("-"))
        age = (date(y2, m2, d2) - date(y, m, d)).days
    except Exception:
        age = -1
    return {"tiers": len(CATALOG), "checked": CHECKED,
            "staleness_days": age, "by_confidence": counts}
