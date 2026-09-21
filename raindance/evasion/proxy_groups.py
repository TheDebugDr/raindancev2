"""Proxy groups — named pools (residential / ISP / default) for task assignment."""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

from raindance.evasion.proxy_manager import (
    ProxyManager,
    normalize_proxy_entry,
    parse_proxy,
    valid,
)


class ProxyGroupManager:
    """Manages multiple named proxy pools.

    Config shape (settings.data['proxy_groups']):
      {
        "default": {"file": "proxies.txt", "proxies": [], "sticky": true},
        "residential": {"file": "proxies_res.txt", "proxies": [], "sticky": true},
        "isp": {"file": "proxies_isp.txt", "proxies": [], "sticky": true}
      }
    """

    def __init__(self):
        self._groups: Dict[str, ProxyManager] = {}
        self._meta: Dict[str, dict] = {}

    def load_from_settings(self, settings_data: dict) -> None:
        groups = settings_data.get("proxy_groups") or {}
        # Always ensure "default" from evasion.proxies / proxies.txt
        evasion = settings_data.get("evasion") or {}
        if "default" not in groups:
            groups = {
                "default": {
                    "file": evasion.get("proxies_file") or "proxies.txt",
                    "proxies": list(evasion.get("proxies") or []),
                    "sticky": bool(evasion.get("sticky", True)),
                },
                **groups,
            }
        self._groups.clear()
        self._meta.clear()

        def _ingest(mgr: ProxyManager, entry) -> Optional[Dict]:
            """Parse one entry (plain line or managed dict).

            Managed-dict metadata (label, added_at, notes) is stored beside
            the pool on the manager; a sticky override is registered so
            get_proxy() honors it. Plain lines behave exactly as before.
            """
            url, meta = normalize_proxy_entry(entry)
            p = parse_proxy(url)
            if not valid(p):
                return None
            mgr.proxies.append(p)
            server = p.get("server", "")
            mgr._proxy_meta[server] = meta
            if meta.get("sticky") is not None:
                mgr._sticky_overrides[server] = bool(meta["sticky"])
            return p

        for name, cfg in groups.items():
            cfg = cfg or {}
            sticky = bool(cfg.get("sticky", True))
            mgr = ProxyManager([], sticky=sticky)
            # file
            fpath = cfg.get("file") or ""
            if fpath and Path(fpath).exists():
                mgr = ProxyManager.from_file(fpath, sticky=sticky)
            # inline (plain lines and managed dict entries)
            inline = cfg.get("proxies") or []
            for line in inline:
                _ingest(mgr, line)
            # also merge global evasion list into default
            if name == "default":
                for line in (evasion.get("proxies") or []):
                    url, _ = normalize_proxy_entry(line)
                    p = parse_proxy(url)
                    if valid(p) and p not in mgr.proxies:
                        _ingest(mgr, line)
                if not mgr.proxies:
                    file_fallback = evasion.get("proxies_file") or "proxies.txt"
                    if Path(file_fallback).exists():
                        extra = ProxyManager.from_file(file_fallback, sticky=sticky)
                        mgr.proxies.extend(extra.proxies)
            self._groups[name] = mgr
            self._meta[name] = {
                "file": fpath,
                "sticky": sticky,
                "count": mgr.count,
            }

        if "default" not in self._groups:
            self._groups["default"] = ProxyManager.from_settings(evasion)
            self._meta["default"] = {"file": "proxies.txt", "sticky": True,
                                     "count": self._groups["default"].count}

    def group_names(self) -> List[str]:
        return sorted(self._groups.keys())

    def count(self, group: str = "default") -> int:
        mgr = self._groups.get(group) or self._groups.get("default")
        return mgr.count if mgr else 0

    def total(self) -> int:
        return sum(m.count for m in self._groups.values())

    def get_manager(self, group: str = "default") -> Optional[ProxyManager]:
        return self._groups.get(group) or self._groups.get("default")

    def get_proxy(self, session_key: str, group: str = "default") -> Optional[dict]:
        mgr = self.get_manager(group)
        if not mgr:
            return None
        return mgr.get_proxy(session_key)

    def check_all(self, timeout: float = 3.0) -> dict:
        """TCP-probe every group's pool. Returns {group: (reachable, total)}.

        This is the path that makes "Test reachability" mean something for TASK
        runs: the runner pulls proxies from these group managers, not from the
        standalone pool the Evasion editor writes to.
        """
        out: dict = {}
        for name, mgr in self._groups.items():
            if not mgr or not mgr.proxies:
                continue
            ok = mgr.check_all(timeout=timeout) if hasattr(mgr, "check_all") else 0
            out[name] = (ok, mgr.count)
        return out

    def summary(self) -> dict:
        return {name: {"count": m.count, **self._meta.get(name, {})}
                for name, m in self._groups.items()}
