"""Task models and status constants for the multi-task orchestrator."""
from __future__ import annotations

import secrets
from datetime import datetime
from typing import Any

# Lifecycle (maps to your Phases 1–8)
STATUS_IDLE = "idle"
STATUS_ARMED = "armed"           # loaded, waiting for start/schedule
STATUS_MONITORING = "monitoring"  # Phase 2
STATUS_TRIGGERED = "triggered"    # stock hit, about to launch
STATUS_LAUNCHING = "launching"    # Phase 3
STATUS_QUEUED = "queued"          # Phase 4 virtual queue
STATUS_ATC = "atc"                # Phase 5
STATUS_CAPTCHA = "captcha"        # Phase 5 pause
STATUS_CHECKOUT = "checkout"      # Phase 6
STATUS_SUBMITTING = "submitting"  # Phase 7
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_STOPPED = "stopped"
STATUS_RETRYING = "retrying"

# Per-task ceiling on units. The retail/MSRP gate reasons about ONE unit at one
# listed price and nothing downstream re-checks the cart total, so an unbounded
# quantity silently multiplies an approved price by a number no gate ever saw.
# Matches the max the Tasks panel has always used.
MAX_QUANTITY = 10

TERMINAL = {STATUS_SUCCESS, STATUS_FAILED, STATUS_STOPPED, STATUS_IDLE}

MODES = ("guest", "account")
SITES = ("pokemon_center", "target", "walmart", "bestbuy", "generic")
PRIORITIES = ("high", "normal", "low")

DEFAULT_TASK: dict[str, Any] = {
    "id": "",
    "name": "",
    "enabled": True,
    "url": "",
    "product_id": "",          # optional site PID
    "quantity": 1,
    "profile_id": "",
    "proxy_group": "default",
    "site": "pokemon_center",
    "mode": "guest",           # guest | account
    "monitor": True,           # Phase 2 first; False → go straight to checkout
    "priority": "normal",      # high | normal | low — high polls fastest, fires first
    "dry_run": True,           # stop before place-order
    "max_retries": 2,
    "variant": "",             # optional size/version selector hint
    # runtime (not always persisted mid-run)
    "status": STATUS_IDLE,
    "attempt": 0,
    "last_error": "",
    # True when the run ended because the retail/MSRP gate refused the page
    # (bot wall, reseller, over MSRP, unverifiable) rather than because
    # something broke. A deliberate "denied", not a failure.
    "denied": False,
    "order_id": "",
    "message": "",
    "created": "",
    "updated": "",
}


def new_task_id() -> str:
    return "task_" + secrets.token_hex(4)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def normalize_task(raw: dict | None) -> dict:
    t = dict(DEFAULT_TASK)
    if raw:
        t.update({k: v for k, v in raw.items() if k in t or k in raw})
        # ensure defaults for known keys
        for k, v in DEFAULT_TASK.items():
            if k not in t or t[k] is None:
                t[k] = v
    if not t.get("id"):
        t["id"] = new_task_id()
    if not t.get("name"):
        t["name"] = t.get("url") or t["id"]
    if not t.get("created"):
        t["created"] = now_iso()
    t["updated"] = now_iso()
    t["quantity"] = max(1, min(MAX_QUANTITY, int(t.get("quantity") or 1)))
    t["max_retries"] = max(0, int(t.get("max_retries") or 0))
    # Validate against the LIVE handler registry, not the frozen tuple: a
    # user-owned store registers its own id at startup, and this clamp used to
    # rewrite it to "generic" on every read/write of the task store.
    try:
        from raindance.sites import list_sites as _live_sites
        _valid = set(_live_sites()) | set(SITES)
    except Exception:      # import cycle or a broken handler - fail safe
        _valid = set(SITES)
    if t.get("site") not in _valid:
        t["site"] = "generic"
    if t.get("mode") not in MODES:
        t["mode"] = "guest"
    if (t.get("priority") or "normal") not in PRIORITIES:
        t["priority"] = "normal"
    return t
