"""The Execute queue: listings loaded from Monitors, with per-item run config.

run_plan cannot carry per-item settings (its load() whitelists 6 keys), and
run_plan.materialize_tasks() deletes and recreates every task it owns. So the
queue is its own top-level settings key — free-form, additive, and safe across
save/load because the settings merge replaces lists wholesale.

One entry = one product id plus how to run it. Tasks are materialised from
entries with origin="execute_queue", so re-loading never wipes anything the user
built by hand in the Tasks page.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from raindance.core import catalog as CAT
from raindance.core import retailer_config as RC
from raindance.core.leases import assign_profiles

KEY = "execute_queue"
ORIGIN = "execute_queue"

DEFAULT_ENTRY: Dict[str, Any] = {
    "product_id": "",
    "enabled": True,
    "profile_id": "",         # "" -> orchestrator's default profile
    "proxy_group": "default",
    "captcha_mode": "manual",  # manual | api
    "evasion": True,           # per-item toggle; the layer itself is untouched
    "poll_seconds": 5.0,       # per-listing cadence on the checkout-capable loop
    "quantity": 1,
    "dry_run": True,
    "added": "",
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class ExecuteQueue:
    def __init__(self, settings):
        self.settings = settings
        self.settings.data.setdefault(KEY, [])

    @property
    def items(self) -> List[Dict]:
        return self.settings.data[KEY]

    def save(self) -> None:
        self.settings.save()

    # -- membership -------------------------------------------------------- #
    def get(self, product_id: str) -> Optional[Dict]:
        return next((e for e in self.items if e["product_id"] == product_id), None)

    def has(self, product_id: str) -> bool:
        return self.get(product_id) is not None

    def add(self, product_id: str, **overrides) -> Dict:
        """Idempotent: loading the same listing twice updates, never duplicates."""
        existing = self.get(product_id)
        if existing is not None:
            existing.update(overrides)
            self.save()
            return existing
        entry = dict(DEFAULT_ENTRY)
        entry.update(overrides)
        entry["product_id"] = product_id
        entry["added"] = _now()
        self.items.append(entry)
        self.save()
        return entry

    def add_many(self, product_ids, **overrides) -> int:
        n = 0
        for pid in product_ids:
            if not self.has(pid):
                n += 1
            self.add(pid, **overrides)
        return n

    def update(self, product_id: str, **fields) -> Optional[Dict]:
        e = self.get(product_id)
        if e is None:
            return None
        e.update(fields)
        self.save()
        return e

    def remove(self, product_id: str) -> None:
        self.settings.data[KEY] = [e for e in self.items
                                   if e["product_id"] != product_id]
        self.save()

    def clear(self) -> None:
        self.settings.data[KEY] = []
        self.save()

    # -- joining ----------------------------------------------------------- #
    def rows(self, store) -> List[Dict]:
        """[{entry, product}] for entries whose product still exists."""
        out = []
        for e in self.items:
            p = store.get(e["product_id"])
            if p is not None:
                out.append({"entry": e, "product": p})
        return out

    def prune(self, store) -> int:
        """Drop entries whose product was deleted. Returns how many went."""
        before = len(self.items)
        self.settings.data[KEY] = [e for e in self.items
                                   if store.get(e["product_id"]) is not None]
        if len(self.items) != before:
            self.save()
        return before - len(self.items)

    # -- materialise into runnable tasks ------------------------------------ #
    def materialize(self, *, store, task_store, profile_store) -> List[Dict]:
        """Turn enabled entries into tasks, replacing only our own origin.

        Tasks created by the Tasks page (no origin) and by run_plan are left
        alone — only origin == "execute_queue" rows are refreshed.
        """
        for t in list(task_store.list()):
            if t.get("origin") == ORIGIN:
                task_store.remove(t["id"])

        # Spread rows across every profile that exists instead of dealing
        # profiles[0] to all of them. Rows sharing one profile share one cart
        # and one card, so they serialize behind that profile's lease at run
        # time — which is safe, but it also means a queue of eight listings
        # would have run one at a time. Distinct profiles run in parallel.
        try:
            available = [p["id"] for p in profile_store.list()]
        except Exception:
            available = []
        enabled_rows = [r for r in self.rows(store)
                        if r["entry"].get("enabled", True)]
        chosen = assign_profiles(
            enabled_rows, available, get=lambda r: r["entry"].get("profile_id"))
        if available and len(enabled_rows) > len(available):
            log = getattr(self, "bus", None)
            if log is not None:
                log.log(
                    f"[queue] {len(enabled_rows)} listings but only "
                    f"{len(available)} profile(s) — rows sharing a profile will "
                    f"run one at a time; add profiles for full parallelism",
                    "warn")

        made: List[Dict] = []
        for row, row_profile in zip(enabled_rows, chosen):
            e, p = row["entry"], row["product"]
            # site_params.evasion is emitted only when the row turns evasion
            # OFF; at the default (True) the key is omitted so the task inherits
            # the master switch + retailer config. A row can never turn it on.
            # Start from the store's own retailer_configs[site].site_params
            # (selectors / frames / fields the Catalog page owns), exactly as
            # run_plan.materialize_tasks does; without this the wizard's tasks
            # carried only the two row keys and every per-store parameter the
            # user_store handler reads from task["site_params"] was dropped.
            #
            # store_id ("store_target") is the catalog's own id, not a
            # handler registry key ("target") — those are two different
            # naming schemes that happen to look related. Passing store_id
            # straight through as `site` silently landed every seeded-store
            # task on GenericHandler: no Target-specific seller/queue
            # handling, no retailer_configs[site] match (falls to "generic"),
            # confirmed live via an actual app run. catalog.handler_for_store
            # is the SAME translation run_plan.py already does correctly via
            # its own handler_for(); a custom store's id has no entry there
            # and correctly falls through to GenericHandler either way.
            store_id = p.get("store_id") or p.get("site") or "generic"
            site = CAT.handler_for_store(store_id)
            sp: Dict[str, Any] = RC.site_params_for(self.settings, site)
            sp["captcha_mode"] = e.get("captcha_mode", "manual")
            if not bool(e.get("evasion", True)):
                sp["evasion"] = False
            made.append(task_store.add(
                name=p.get("name") or p.get("url", ""),
                url=p.get("url", ""),
                product_id=p.get("id", ""),
                site=site,
                profile_id=row_profile,
                proxy_group=e.get("proxy_group") or "default",
                quantity=int(e.get("quantity") or 1),
                monitor=True,
                priority="high",
                dry_run=bool(e.get("dry_run", True)),
                enabled=True,
                origin=ORIGIN,
                poll_seconds=float(e.get("poll_seconds") or 5.0),
                set_id=p.get("set_id", ""),
                line_id=p.get("line_id", ""),
                store_id=p.get("store_id", ""),
                site_params=sp,
            ))
        return made
