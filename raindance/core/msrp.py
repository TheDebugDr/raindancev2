"""MSRP judgement — the decisive anti-scalper signal.

Price is ground truth here, and seller identity is corroboration. That ordering
is deliberate and was chosen from how the listings actually behave: a partner
trading as a "national distributor" is still charging $183 for a box that
retails at $161.64, and a marketplace listing carrying no visible third-party
badge is still charging $219.99 for an Elite Trainer Box that Pokémon Center
and Target both sell at $59.99. The name on the listing does not bound what it
costs. The number does.

Two things this module got wrong before, both of which let money out:

1. NO UPPER TIGHTNESS. tolerance 0.15 + $12 fixed put the ceiling on a $161.64
   booster box at $197.89, so a $183 distributor listing read as "at MSRP".
   Observed reality is that legitimate first-party retail sits *on* MSRP and
   the cheapest resale seen was 2.5x, so the band is now 6% + $5 and the
   ceiling on that same box is $176.34. $183 is refused.

2. NO LOWER BOUND. ``at_msrp`` was ``price <= threshold`` with nothing below,
   so an under-parsed price — the $9.00 protection-plan offer that the old
   unanchored parser used to grab — satisfied the gate. A price far under MSRP
   is not a bargain on sealed product in a shortage; it is a misread. Anything
   below ``floor_fraction * msrp`` now returns ``at_msrp=None``, which the
   classifier reports as unverified rather than buyable.

The tier table, era handling and provenance live in ``msrp_catalog``; settings
may override the numeric bands and add tiers under a ``pricing`` block.
"""
from __future__ import annotations

from typing import Optional

from raindance.core import msrp_catalog as _cat

# Re-exported so callers and tests have one import site.
CATALOG = _cat.CATALOG
CHECKED = _cat.CHECKED
DEFAULTS = dict(_cat.DEFAULTS)


def _cfg(settings) -> dict:
    cfg = dict(_cat.DEFAULTS)
    if settings is not None:
        override = (settings.data.get("pricing") or {})
        cfg.update({k: v for k, v in override.items() if k in cfg})
    return cfg


def _extra_tiers(settings) -> list:
    """User-supplied tiers from config.json, tried before the built-ins.

    Accepts the legacy shape ({type, msrp, pattern}) as well as the catalog
    shape ({type, patterns, msrp:{era:...}}), so an existing config keeps
    working after the table moved.
    """
    if settings is None:
        return []
    raw = (settings.data.get("pricing") or {}).get("msrp_table")
    if not raw:
        return []
    out = []
    for row in raw:
        try:
            msrp = row["msrp"]
            if not isinstance(msrp, dict):
                msrp = {e: float(msrp) for e, _ in _cat.ERAS}
                msrp["current"] = float(row["msrp"])
            patterns = row.get("patterns") or [row["pattern"]]
            out.append({"type": row["type"], "display": row.get("display", row["type"]),
                        "patterns": patterns, "msrp": msrp,
                        "confidence": row.get("confidence", "user"),
                        "source": "config.json"})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def classify_sku(name: str, settings=None) -> str:
    """Product tier from the listing title; 'unknown' when nothing matches."""
    entry = _cat.find_tier(name, _extra_tiers(settings))
    return entry["type"] if entry else "unknown"


def msrp_for(name: str, settings=None) -> Optional[float]:
    return _cat.msrp_for(name, _extra_tiers(settings))


def tier_info(name: str, settings=None) -> dict:
    """Everything known about the matched tier, for logging and the UI."""
    entry = _cat.find_tier(name, _extra_tiers(settings))
    if not entry:
        return {"type": "unknown", "display": "unknown", "confidence": "none",
                "source": "", "era": _cat.era_for(name)}
    return {"type": entry["type"], "display": entry.get("display", entry["type"]),
            "confidence": entry.get("confidence", "unverified"),
            "source": entry.get("source", ""), "era": _cat.era_for(name)}


def price_verdict(price, name: str, *, allin: bool = False, settings=None,
                  first_party: Optional[bool] = None) -> dict:
    """Judge a price against MSRP for the product's tier and era.

    Returns {sku_type, msrp, price, at_msrp, band, threshold, floor, era,
             confidence, over_pct}.

    ``at_msrp`` is None — not False — whenever the judgement cannot honestly be
    made: no price, no tier, or a price so far below MSRP that it is a parsing
    artefact rather than an offer. Callers treat None as not-buyable.
    """
    cfg = _cfg(settings)
    extra = _extra_tiers(settings)
    entry = _cat.find_tier(name, extra)
    sku_type = entry["type"] if entry else "unknown"
    era = _cat.era_for(name)
    confidence = entry.get("confidence", "unverified") if entry else "none"
    msrp = _cat.msrp_for(name, extra)

    base = {"sku_type": sku_type, "msrp": msrp, "price": price, "era": era,
            "confidence": confidence, "at_msrp": None, "band": "unknown",
            "threshold": None, "floor": None, "over_pct": None,
            "band_used": None}

    if msrp is None or not isinstance(price, (int, float)) or isinstance(price, bool):
        base["band"] = "unknown"
        return base
    if era is None:
        # Tier matched but the set did not, so which era's MSRP applies is a
        # guess. Refuse rather than guess — see msrp_catalog.era_for.
        base["band"] = "era_unknown"
        return base

    price = float(price)
    is_box = sku_type in _cat._BOX_TYPES
    # A confirmed first-party listing gets the retail band; marketplace and
    # unconfirmed sellers get the tight one. See the calibration note in
    # msrp_catalog.DEFAULTS for why one band cannot cover both.
    if first_party is True:
        tol = max(cfg["tolerance_first_party"],
                  cfg["tolerance_allin"] if allin else 0.0)
    else:
        tol = cfg["tolerance_allin"] if allin else cfg["tolerance"]
    fixed = cfg["fixed_box"] if is_box else cfg["fixed_small"]
    ceiling = msrp * (1 + tol) + fixed
    if first_party is True:
        # Percentage alone gets loose in dollars on expensive product: +25% on a
        # $161.64 box is $40 of slack. Cap the absolute premium as well.
        ceiling = min(ceiling, msrp + cfg["first_party_max_premium"])
    floor = msrp * cfg["floor_fraction"]
    scalper = msrp * cfg["scalper_multiple"]

    base.update(price=price, threshold=round(ceiling, 2), floor=round(floor, 2),
                over_pct=round((price / msrp - 1) * 100, 1),
                band_used="first_party" if first_party is True else "tight")

    if price < floor:
        # Not a deal — a misread. Sealed product does not sell at 40% under
        # MSRP during a shortage, and this is exactly the shape of the
        # protection-plan / accessory price the old parser used to pick up.
        base.update(at_msrp=None, band="implausible")
        return base
    if price <= ceiling:
        base.update(at_msrp=True, band="at_msrp")
    elif price < scalper:
        base.update(at_msrp=False, band="soft")
    else:
        base.update(at_msrp=False, band="scalper")
    return base
