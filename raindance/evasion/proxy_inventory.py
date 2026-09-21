"""Managed proxy inventory — add/remove/list proxies through config.json.

The pool is empty right now (placeholder); this module is the clean plumbing
for adding proxies later. Managed entries live as dicts in the group's inline
``proxies`` list inside ``settings.data["proxy_groups"]``; group-level shared
settings (``file``, ``sticky``, ...) stay in the group config where they are.

Entry shape in config.json::

    "proxy_groups": {
      "residential": {
        "file": "proxies_residential.txt",
        "sticky": true,
        "proxies": [
          {"url": "http://user:pass@1.2.3.4:8080", "label": "resi-01",
           "added_at": "2026-09-21T17:30:00Z", "notes": "provider X",
           "sticky": null}
        ]
      }
    }

Legacy plain-string lines keep working — they are treated as ``{"url": line}``
with empty metadata. The legacy ``proxies.txt`` / Evasion-page textarea path
is untouched: it edits the standalone file pool, while managed entries are
consumed by the task path (``ProxyGroupManager``). ``sync()`` refreshes both
live objects from settings after a change.

Persistence goes through the app's settings object: entries are mutated in
``settings.data`` in place, then ``save()`` calls ``settings.save()``. This
module never hand-edits config.json.

Credentials are NEVER logged or printed raw: every read API returns the
masked form from ``proxy_manager.mask()``, and validation errors echo only
the masked form of the caller's own input.
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional
from urllib.parse import urlparse

from raindance.evasion.proxy_manager import (
    VALID_PROXY_SCHEMES,
    ProxyManager,
    host_port,
    mask,
    normalize_proxy_entry,
    parse_proxy,
    valid,
)


def _utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _dedupe_key(proxy: Dict) -> tuple:
    """Canonical identity for dedupe: (server.lower(), username or "")."""
    return ((proxy.get("server") or "").lower(), proxy.get("username") or "")


class ProxyInventory:
    """Add/remove/list managed proxies on top of the settings object.

    ``settings`` is the app Settings (or anything exposing ``.data`` and
    ``.save()``). All writes go to ``settings.data["proxy_groups"]`` in
    place; call ``save()`` to persist via ``settings.save()``.
    """

    def __init__(self, settings):
        self.settings = settings

    # -- groups -------------------------------------------------------- #
    def _groups(self) -> dict:
        data = self.settings.data
        groups = data.get("proxy_groups")
        if not isinstance(groups, dict):
            groups = {}
            data["proxy_groups"] = groups
        return groups

    def groups(self) -> List[str]:
        """Names of all configured groups."""
        return sorted(self._groups())

    def group_settings(self, group: str = "default") -> dict:
        """The group's SHARED settings (file, sticky, ...), not per-proxy data.

        These stay in proxy_groups[group]; per-proxy metadata lives on the
        individual entries.
        """
        cfg = self._groups().get(group) or {}
        return {k: v for k, v in cfg.items() if k != "proxies"}

    def _ensure_group(self, group: str) -> dict:
        groups = self._groups()
        cfg = groups.get(group)
        if not isinstance(cfg, dict):
            cfg = {"file": "", "proxies": [], "sticky": True}
            groups[group] = cfg
        if not isinstance(cfg.get("proxies"), list):
            cfg["proxies"] = []
        return cfg

    def _entries(self, group: str) -> List[dict]:
        return self._ensure_group(group).get("proxies") or []

    # -- read ---------------------------------------------------------- #
    def _masked(self, group: str, entry, parsed: Dict, note: str = "") -> dict:
        """One entry in display form. Credentials masked — always."""
        _, meta = normalize_proxy_entry(entry)
        host, port = host_port(parsed)
        out = {
            "group": group,
            "label": meta["label"],
            "url": mask(parsed),      # masked — never raw credentials
            "host": host,
            "port": port,
            "added_at": meta["added_at"],
            "notes": meta["notes"],
            "sticky": meta["sticky"],   # None → inherit group setting
        }
        if note:
            out["note"] = note
        return out

    def list_proxies(self, group: Optional[str] = None) -> List[dict]:
        """Masked entries for one group, or every group when group is None."""
        names = [group] if group else self.groups()
        out: List[dict] = []
        for name in names:
            for entry in self._entries(name):
                url, _ = normalize_proxy_entry(entry)
                parsed = parse_proxy(url)
                if not valid(parsed):
                    continue
                out.append(self._masked(name, entry, parsed))
        return out

    def _locate(self, label_or_url: str,
                group: Optional[str] = None) -> tuple:
        """-> (group_name, index, entry, parsed). Raises KeyError if missing.

        Matches, in order: exact label, exact URL (server + credentials), then
        the bare endpoint (server only, credentials ignored).
        """
        want = (label_or_url or "").strip()
        want_url, _ = normalize_proxy_entry(want)
        want_p = parse_proxy(want_url)
        want_key = _dedupe_key(want_p) if valid(want_p) else None
        want_server = ((want_p.get("server") or "").lower()
                       if valid(want_p) else "")
        names = [group] if group else self.groups()
        fallback = None
        for name in names:
            for i, entry in enumerate(self._entries(name)):
                url, meta = normalize_proxy_entry(entry)
                if meta["label"] and meta["label"] == want:
                    return name, i, entry, parse_proxy(url)
                parsed = parse_proxy(url)
                if not valid(parsed):
                    continue
                if want_key and _dedupe_key(parsed) == want_key:
                    return name, i, entry, parsed
                if (want_server
                        and (parsed.get("server") or "").lower() == want_server
                        and fallback is None):
                    fallback = (name, i, entry, parsed)
        if fallback is not None:
            return fallback
        scope = f" in group {group!r}" if group else ""
        raise KeyError(f"no proxy matching {want!r}{scope}")

    # -- write --------------------------------------------------------- #
    def add_proxy(self, url: str, group: str = "default",
                  label: Optional[str] = None,
                  sticky: Optional[bool] = None,
                  notes: str = "") -> dict:
        """Validate and add one proxy. Returns the stored entry, masked.

        Format: ``scheme://[user:pass@]host:port`` (schemes http, https,
        socks4, socks5) or the legacy bare forms ``host:port`` /
        ``host:port:user:pass``. ``sticky`` True/False overrides the group's
        sticky setting for this proxy; None inherits it.

        Raises ValueError on a bad format, a taken label, or a duplicate URL
        in the same group.
        """
        group = (group or "default").strip() or "default"
        cfg = self._ensure_group(group)
        url = (url or "").strip()
        parsed = parse_proxy(url)
        if not valid(parsed):
            shown = mask(parsed) if parsed else "unparseable"
            raise ValueError(
                f"not a usable proxy ({shown}): expected "
                "scheme://[user:pass@]host:port with a numeric port 1-65535")
        if "://" in url:
            scheme = urlparse(url).scheme.lower()
            if scheme not in VALID_PROXY_SCHEMES:
                raise ValueError(
                    f"unsupported proxy scheme {scheme!r}: "
                    f"use one of {sorted(VALID_PROXY_SCHEMES)}")
        key = _dedupe_key(parsed)
        for entry in cfg.get("proxies") or []:
            e_url, e_meta = normalize_proxy_entry(entry)
            e_p = parse_proxy(e_url)
            if valid(e_p) and _dedupe_key(e_p) == key:
                raise ValueError(
                    f"duplicate of '{e_meta['label'] or mask(e_p)}' "
                    f"already in group {group!r}")
        label = (label or "").strip()
        if label:
            for entry in cfg.get("proxies") or []:
                _, e_meta = normalize_proxy_entry(entry)
                if e_meta["label"] == label:
                    raise ValueError(
                        f"label {label!r} is already used in group {group!r}")
        if sticky not in (True, False, None):
            raise ValueError("sticky must be True, False or None (inherit)")
        entry = {
            "url": url,
            "label": label,
            "added_at": _utcnow(),
            "notes": (notes or "").strip(),
            "sticky": sticky,
        }
        cfg["proxies"].append(entry)
        cross = [g for g in self.groups() if g != group
                 and self._group_has_key(g, key)]
        return self._masked(
            group, entry, parsed,
            note=("also present in group(s): " + ", ".join(cross))
            if cross else "")

    def _group_has_key(self, group: str, key: tuple) -> bool:
        for entry in self._entries(group):
            url, _ = normalize_proxy_entry(entry)
            parsed = parse_proxy(url)
            if valid(parsed) and _dedupe_key(parsed) == key:
                return True
        return False

    def remove_proxy(self, label_or_url: str,
                     group: Optional[str] = None) -> dict:
        """Remove one proxy by label or URL. Returns the removed entry, masked.

        Raises KeyError when nothing matches. With no proxies at all this
        simply raises — the empty pool keeps working (get_proxy → None →
        the launch exits direct).
        """
        name, i, entry, parsed = self._locate(label_or_url, group)
        del self._entries(name)[i]
        return self._masked(name, entry, parsed)

    # -- reachability (hooks into the existing probing) ---------------- #
    def probe(self, label_or_url: str, group: Optional[str] = None,
              timeout: float = 3.0) -> dict:
        """TCP reachability of the proxy endpoint (needs no auth).

        Uses the existing ProxyManager.check() hook on a throwaway manager so
        the live pools are untouched. Masked; no credentials leave this call.
        """
        name, _, _, parsed = self._locate(label_or_url, group)
        ok = ProxyManager([]).check(parsed, timeout=timeout)
        return {
            "group": name,
            "proxy": mask(parsed),
            "ok": ok,
            "stage": "tcp",
            "summary": ("reachable"
                          if ok else "no connection to the proxy endpoint"),
        }

    def probe_http(self, label_or_url: str, group: Optional[str] = None,
                   timeout: Optional[float] = None) -> dict:
        """Full-path probe (CONNECT tunnel, TLS, body) via check_http().

        The returned summary/error are scrubbed of credentials first — a
        urllib failure can carry the whole proxy URL, password included.
        """
        name, _, _, parsed = self._locate(label_or_url, group)
        res = ProxyManager([]).check_http(dict(parsed), timeout=timeout)
        return {
            "group": name,
            "proxy": mask(parsed),
            "ok": bool(res.get("ok")),
            "stage": res.get("stage"),
            "status": res.get("status"),
            "latency_ms": res.get("latency_ms"),
            "exit_ip": res.get("exit_ip"),
            "geo": res.get("geo"),
            "timezone": res.get("timezone"),
            "error": self._scrub(str(res.get("error") or ""), parsed),
            "summary": self._scrub(str(res.get("summary") or ""), parsed),
        }

    @staticmethod
    def _scrub(text: str, proxy: Dict) -> str:
        """Strip the proxy's own credentials from free-form text."""
        out = str(text or "")
        for secret in (proxy.get("password"), proxy.get("username")):
            if secret and len(str(secret)) > 1:
                out = out.replace(str(secret), "***")
        return out

    # -- persistence --------------------------------------------------- #
    def save(self) -> None:
        """Persist through the app's settings object (settings.save())."""
        self.settings.save()

    def sync(self, proxy_manager=None, proxy_groups=None) -> None:
        """Rebuild live managers from the saved settings, in place.

        Pass the app's ``ctx.proxy_manager`` (standalone file pool) and
        ``ctx.proxy_groups`` / the orchestrator's group manager so an
        add/remove takes effect without a restart — the same hot-reload path
        the Evasion page uses.
        """
        evasion = self.settings.data.get("evasion") or {}
        if proxy_manager is not None:
            proxy_manager.reload_from_settings(evasion)
        if proxy_groups is not None:
            proxy_groups.load_from_settings(self.settings.data)
