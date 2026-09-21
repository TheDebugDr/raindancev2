"""The Run Plan — what the Arm wizard produces and the orchestrator consumes.

A plan answers four questions: which retailers, which items from each, at what
time, and does it spend money. Arming the plan materializes one task per
selected product.

Products (`ProductStore`) are things you *watch*. Tasks (`TaskStore`) are things
that *check out*. This module is the only bridge between them — it is where a
retailer display name becomes a site handler, and where a plan's `live` flag
becomes a task's `dry_run`.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from raindance.tasks.models import SITES, normalize_task

# Retailer display name → the checkout handler that can drive it.
# raindance/sites/ ships a handler per key here; every other retailer is generic.
_HANDLERS: dict[str, str] = {
    "Pokémon Center": "pokemon_center",
    "Target": "target",
    "Walmart": "walmart",
    "Best Buy": "bestbuy",
}

# Tasks created by arming a plan carry this marker, so re-arming replaces only
# them and never clobbers a task the user hand-wrote on the Tasks page.
ORIGIN = "run_plan"

DEFAULT_PLAN: dict[str, Any] = {
    "retailers": [],       # display names, e.g. ["Target", "Best Buy"]
    "product_ids": [],     # ProductStore ids
    "start_at": "",        # "" → start now; else "HH:MM[:SS]" or ISO
    "live": False,         # False → dry run (stop before place-order)
    "workers": 8,          # max parallel checkout sessions
    "updated": "",
}


def handler_for(site: str) -> str:
    """Map a retailer display name to a task `site` handler."""
    h = _HANDLERS.get(site, "generic")
    return h if h in SITES else "generic"


def load(settings) -> dict:
    """Read the saved plan, filling any missing keys with defaults."""
    raw = settings.data.get("run_plan") or {}
    plan = dict(DEFAULT_PLAN)
    plan.update({k: v for k, v in raw.items() if k in DEFAULT_PLAN})
    plan["retailers"] = list(plan["retailers"] or [])
    plan["product_ids"] = list(plan["product_ids"] or [])
    plan["workers"] = max(1, min(64, int(plan["workers"] or 8)))
    plan["live"] = bool(plan["live"])
    return plan


def save(settings, plan: dict) -> dict:
    plan = dict(plan)
    plan["updated"] = datetime.now().isoformat(timespec="seconds")
    settings.data["run_plan"] = plan
    settings.save()
    return plan


def is_empty(plan: dict) -> bool:
    return not plan.get("product_ids")


def prune(plan: dict, store) -> dict:
    """Drop product ids that no longer exist, and retailers with nothing left."""
    live_ids = {p["id"] for p in store.items}
    plan["product_ids"] = [i for i in plan["product_ids"] if i in live_ids]
    kept = {p["site"] for p in store.items if p["id"] in plan["product_ids"]}
    plan["retailers"] = [r for r in plan["retailers"] if r in kept]
    return plan


def products(plan: dict, store) -> list[dict]:
    """The selected products, in ProductStore order."""
    chosen = set(plan.get("product_ids") or [])
    return [p for p in store.items if p["id"] in chosen]


def summary(plan: dict, store) -> str:
    """One human line: '4 items across Target + Best Buy at 09:59:30'."""
    items = products(plan, store)
    if not items:
        return "No items selected"
    retailers = sorted({p["site"] for p in items})
    when = plan.get("start_at") or "now"
    noun = "item" if len(items) == 1 else "items"
    return f"{len(items)} {noun} across {' + '.join(retailers)} at {when}"


def live_blockers(profile_store) -> list[str]:
    """Reasons a LIVE run cannot be armed. Empty list → live is allowed.

    A live checkout needs an identity to ship and bill to; a dry run does not.
    """
    reasons: list[str] = []
    summaries = profile_store.list() if profile_store else []
    if not summaries:
        reasons.append("No profile exists — a live order needs a shipping "
                       "address and a payment method.")
        return reasons

    # profile_store.list() returns only {id, name, email}; the address and
    # payment live in the full record, so each one has to be fetched. The old
    # code tested list() for keys it never contained ("address"/"payment.number"
    # instead of "shipping"/"payment.card_number"), so LIVE was permanently
    # blocked no matter how complete a profile was.
    missing: list[str] = []
    usable = 0
    for summary in summaries:
        pid = summary.get("id") or ""
        full = None
        try:
            full = profile_store.get(pid)
        except Exception:
            full = None
        if not full:
            missing.append(f"{summary.get('name') or pid}: could not be read")
            continue

        ship = full.get("shipping") or {}
        pay = full.get("payment") or {}
        gaps: list[str] = []
        if not (ship.get("address1") or "").strip():
            gaps.append("shipping address")
        if not (ship.get("zip") or "").strip():
            gaps.append("postcode")
        if not (pay.get("card_number") or "").strip():
            gaps.append("card number")
        if not (pay.get("expiry") or "").strip():
            gaps.append("card expiry")
        if gaps:
            missing.append(f"{full.get('name') or pid}: missing {', '.join(gaps)}")
        else:
            usable += 1

    if not usable:
        reasons.append("No profile is complete enough for a live order.")
        reasons.extend(missing[:4])
    return reasons


def usable_profile_ids(profile_store) -> list[str]:
    """Ids of profiles complete enough to place a real order."""
    out: list[str] = []
    for summary in (profile_store.list() if profile_store else []):
        try:
            full = profile_store.get(summary.get("id") or "")
        except Exception:
            continue
        if not full:
            continue
        ship = full.get("shipping") or {}
        pay = full.get("payment") or {}
        if all([(ship.get("address1") or "").strip(),
                (ship.get("zip") or "").strip(),
                (pay.get("card_number") or "").strip(),
                (pay.get("expiry") or "").strip()]):
            out.append(full.get("id") or summary.get("id") or "")
    return [i for i in out if i]


def default_profile_id(profile_store) -> str:
    profiles = profile_store.list() if profile_store else []
    return profiles[0].get("id", "") if profiles else ""


def materialize_tasks(plan: dict, *, store, task_store, profile_store,
                      proxy_group: str = "default") -> list[dict]:
    """Turn the plan's selected products into executable tasks.

    Replaces any tasks previously created from a plan; leaves hand-written tasks
    alone. Returns the tasks that now exist for this plan.
    """
    from raindance.core import retailer_config as rc

    settings = task_store.settings          # TaskStore holds the Settings instance
    for t in list(task_store.items):
        if t.get("origin") == ORIGIN:
            task_store.remove(t["id"])

    profile_id = default_profile_id(profile_store)
    dry_run = not plan.get("live")
    created: list[dict] = []

    for p in products(plan, store):
        site = handler_for(p.get("site", ""))
        # The retailer's optimal proxy group + site params, applied automatically.
        group = rc.proxy_group_for(settings, site, default=proxy_group)
        created.append(task_store.add(**normalize_task({
            "name": p.get("name") or p["url"],
            "url": p["url"],
            "site": site,
            "profile_id": profile_id,
            "proxy_group": group,
            "site_params": rc.site_params_for(settings, site),
            "quantity": 1,
            "monitor": True,
            "priority": p.get("priority") or "normal",
            "dry_run": dry_run,
            "enabled": True,
            "origin": ORIGIN,
        })))
    return created


def apply_to_settings(plan: dict, settings) -> None:
    """Push plan-level knobs into the places the backend already reads them."""
    settings.data.setdefault("schedule", {})["start_at"] = plan.get("start_at") or ""
    settings.data.setdefault("orchestrator", {})["workers"] = int(plan.get("workers") or 8)
    settings.data.setdefault("orchestrator", {})["max_workers"] = int(plan.get("workers") or 8)
    # A dry-run plan asserts the global guard; a live plan must not silently clear
    # a guard the user set elsewhere, so we only ever raise it here.
    if not plan.get("live"):
        settings.data.setdefault("checkout", {})["force_dry_run"] = True
    settings.save()
