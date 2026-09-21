"""Per-retailer config overlays.

`config.json["retailer_configs"]` holds optimal settings keyed by site handler
(``target`` / ``pokemon_center`` / ``generic``): which proxy group to use, whether
evasion is on, the CAPTCHA mode, headless preference, and free-form ``site_params``
the handler can read.

These are resolved in two places:
  * arm time  — `run_plan.materialize_tasks` stamps `proxy_group` + `site_params`
                onto the task, so the choice is visible and persisted.
  * run time  — `TaskRunner` reads evasion / headless / captcha so a retailer can
                turn evasion on for itself without touching the global switch.

Adding a retailer is a config entry plus a handler — no code changes here.
"""
from __future__ import annotations

from typing import Any


def resolve(settings, site: str) -> dict:
    """The config block for `site`, falling back to `generic`, then {}."""
    configs = settings.data.get("retailer_configs") or {}
    return configs.get(site) or configs.get("generic") or {}


def proxy_group_for(settings, site: str, default: str = "default") -> str:
    return resolve(settings, site).get("proxy_group") or default


def evasion_enabled_for(settings, site: str) -> bool:
    """Retailer setting wins; otherwise fall back to the global evasion switch."""
    ev = resolve(settings, site).get("evasion") or {}
    if "enabled" in ev:
        return bool(ev["enabled"])
    return bool((settings.data.get("evasion") or {}).get("enabled", False))


def sticky_for(settings, site: str, default: bool = True) -> bool:
    ev = resolve(settings, site).get("evasion") or {}
    return bool(ev.get("sticky", default))


def headless_for(settings, site: str, default: bool) -> bool:
    br = resolve(settings, site).get("browser") or {}
    if "headless" in br:
        return bool(br["headless"])
    return default


def captcha_mode_for(settings, site: str, default: str) -> str:
    cap = resolve(settings, site).get("captcha") or {}
    return cap.get("mode") or default


def site_params_for(settings, site: str) -> dict:
    """The per-store overrides only (what the user saved), never the defaults."""
    return dict(resolve(settings, site).get("site_params") or {})


def default_site_params_for(site_id: str) -> dict:
    """The handler's built-in defaults in the site_params shape (see
    raindance/sites/base.py): generic defaults ← handler defaults. This is
    what the Catalog editor shows as placeholders; an unknown id yields the
    generic defaults."""
    from raindance.sites import handler_class
    return handler_class(site_id).default_site_params()


def effective_site_params_for(settings, site: str) -> dict:
    """Defaults with the store's overrides layered on — the values a run uses."""
    from raindance.sites.base import deep_merge
    return deep_merge(default_site_params_for(site), site_params_for(settings, site))


def describe(settings, site: str) -> str:
    """A one-line summary for the log, e.g. for the launch line."""
    rc = resolve(settings, site)
    if not rc:
        return "no retailer config (defaults)"
    parts = [
        f"evasion={'on' if evasion_enabled_for(settings, site) else 'off'}",
        f"proxy_group={proxy_group_for(settings, site)}",
        f"captcha={captcha_mode_for(settings, site, 'manual')}",
    ]
    params = site_params_for(settings, site)
    if params:
        parts.append("params=" + ",".join(sorted(params.keys())))
    return ", ".join(parts)
