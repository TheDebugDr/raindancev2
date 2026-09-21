"""Proxy pool with sticky assignment, health checks, and a harvester sub-pool.

Sticky sessions keep one account on one exit IP; the harvester pool is a slice
of proxies reserved for CAPTCHA-harvester windows so they don't burn checkout
IPs. Health checks are best-effort (cache-only for now) with a hook to mark a
proxy bad and re-roll the sticky assignment.

Two levels of probe, both kept: ``check()`` opens a TCP connection to the proxy
endpoint and answers yes/no (``check_all()`` counts how many of the pool pass),
while ``check_http()`` sends a real HTTPS request THROUGH the proxy — CONNECT
tunnel, TLS, response body — and reports the exit IP, country and timezone the
endpoint saw, or which stage the attempt died at.
"""
from __future__ import annotations

import hashlib
import random
import re
import socket
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import quote, urlparse

SESSION_PLACEHOLDER = "{session}"


def parse_proxy(line: str) -> Optional[Dict]:
    """Parse one proxy line into Playwright's proxy dict.

    Accepted forms:
      http://host:port
      http://user:pass@host:port
      host:port
      host:port:user:pass
      socks5://host:port
    """
    raw = (line or "").strip()
    if not raw or raw.startswith("#"):
        return None

    if "://" in raw:
        u = urlparse(raw)
        if not u.hostname:
            return None
        # urlparse computes .port lazily and raises ValueError for anything
        # outside 0-65535 (e.g. ":99999"). valid() already guards this; a bad
        # port here must reject the line, not blow up the Save & Apply click.
        try:
            uport = u.port
        except ValueError:
            return None
        port = f":{uport}" if uport else ""
        out: Dict = {"server": f"{u.scheme}://{u.hostname}{port}"}
        if u.username:
            out["username"] = u.username
        if u.password:
            out["password"] = u.password
        return out

    parts = raw.split(":")
    if len(parts) == 2:
        # host:port
        return {"server": f"http://{parts[0]}:{parts[1]}"}
    if len(parts) == 4:
        # host:port:user:pass
        return {
            "server": f"http://{parts[0]}:{parts[1]}",
            "username": parts[2],
            "password": parts[3],
        }
    if len(parts) == 3:
        return {
            "server": f"http://{parts[0]}:{parts[1]}",
            "username": parts[2],
        }
    return {"server": f"http://{raw}"}


# Hostname or IP literal. Deliberately strict: without this a junk line like
# "not a proxy!!" parses into {"server": "http://not a proxy!!"}, joins the pool,
# and only fails later as an opaque browser launch error.
# Underscores are illegal in DNS but common in real proxy-gateway hostnames
# (gate_1.provider.net), and resolvers accept them - so allow them, and allow a
# trailing dot (fully-qualified form). Still strict enough to reject junk.
_HOST_RE = re.compile(r"^(?=.{1,253}\.?$)[A-Za-z0-9_]([A-Za-z0-9_-]*[A-Za-z0-9_])?"
                      r"(\.[A-Za-z0-9_]([A-Za-z0-9_-]*[A-Za-z0-9_])?)*\.?$")
_IPV6_RE = re.compile(r"^[0-9A-Fa-f:]+$")


def host_port(proxy: Dict) -> tuple:
    """(host, port) for a parsed proxy, or ("", 0) if the server is malformed."""
    try:
        u = urlparse(proxy.get("server", ""))
        return (u.hostname or ""), int(u.port or 80)
    except ValueError:
        return "", 0


def valid(proxy: Optional[Dict]) -> bool:
    """Usable only with a real host AND an explicit in-range port."""
    if not proxy:
        return False
    try:
        u = urlparse(proxy.get("server", ""))
        host, port = (u.hostname or ""), u.port   # .port itself raises on junk
    except ValueError:
        return False
    if not host or not (_HOST_RE.match(host) or _IPV6_RE.match(host)):
        return False
    return bool(port and 0 < int(port) < 65536)


def mask(proxy: Dict) -> str:
    """Display form with the password hidden and the username truncated."""
    host, port = host_port(proxy)
    user = proxy.get("username")
    if not user:
        return f"{host}:{port}"
    shown = user[:3] + "\u2026" if len(user) > 3 else user
    return f"{host}:{port}  ({shown}:\u2022\u2022\u2022\u2022)"


# Schemes the managed inventory accepts for explicit ``scheme://`` URLs.
# Legacy bare forms (host:port, host:port:user:pass) are normalized to http
# by parse_proxy and are always allowed.
VALID_PROXY_SCHEMES = frozenset({"http", "https", "socks4", "socks5"})


def normalize_proxy_entry(entry) -> tuple:
    """Split one pool entry into its URL line and its metadata.

    Entries are either a plain proxy line (``host:port``,
    ``http://user:pass@host:port``, ...) or a managed dict::

        {"url": "http://user:pass@host:port", "label": "resi-01",
         "added_at": "2026-09-21T17:30:00Z", "notes": "...", "sticky": True}

    Returns ``(url_line, meta)``. ``meta`` always carries ``label``,
    ``added_at``, ``notes`` and ``sticky``; ``sticky`` is None when the entry
    should inherit the group setting. Legacy string lines get empty metadata.

    Metadata is stored BESIDE the pool, never inside the Playwright proxy
    dict — the launch path only ever sees server/username/password.
    """
    if isinstance(entry, dict):
        url = str(entry.get("url") or "").strip()
        sticky = entry.get("sticky")
        meta = {
            "label": str(entry.get("label") or ""),
            "added_at": str(entry.get("added_at") or ""),
            "notes": str(entry.get("notes") or ""),
            "sticky": sticky if sticky in (True, False) else None,
        }
        return url, meta
    return str(entry or "").strip(), {
        "label": "", "added_at": "", "notes": "", "sticky": None,
    }


# Default endpoint for check_http(): a plain-text key=value trace over HTTPS.
# One request exercises everything a launch needs from a proxy — CONNECT tunnel,
# TLS to the origin, a response body — and its ip= / loc= / tz= lines name the
# exit the browser would appear to come from. Override per manager
# (probe_url=...), per call (check_http(proxy, ...)), or in evasion settings.
DEFAULT_PROBE_URL = "https://www.cloudflare.com/cdn-cgi/trace"
DEFAULT_PROBE_TIMEOUT = 8.0


def _proxy_url(proxy: Dict) -> str:
    """``scheme://user:pass@host:port`` for urllib, with credentials quoted.

    Proxy passwords routinely contain ``@``, ``:`` and ``/``; unquoted, those
    split the URL in the wrong places and the request never reaches the proxy.
    """
    parsed = urlparse(proxy.get("server", ""))
    host = parsed.hostname or ""
    if not host:
        return ""
    if ":" in host:                       # IPv6 literal needs its brackets back
        host = f"[{host}]"
    try:
        port = f":{parsed.port}" if parsed.port else ""
    except ValueError:
        return ""
    auth = ""
    user = proxy.get("username")
    if user:
        auth = quote(str(user), safe="")
        password = proxy.get("password")
        if password:
            auth += f":{quote(str(password), safe='')}"
        auth += "@"
    return f"{parsed.scheme or 'http'}://{auth}{host}{port}"


def _classify_urlerror(exc: Exception) -> tuple:
    """(stage, message) for a urllib failure — where in the path it stopped."""
    reason = getattr(exc, "reason", exc)
    text = str(reason) or type(exc).__name__
    low = text.lower()
    if isinstance(reason, ssl.SSLError) or isinstance(exc, ssl.SSLError):
        return "tls", text
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return "timeout", text
    if isinstance(reason, socket.gaierror):
        return "tcp", text
    if "certificate" in low or "ssl" in low or "handshake" in low:
        return "tls", text
    # A CONNECT tunnel refused for auth surfaces as a plain error carrying the
    # tunnel's status line, so 407 has to be recognised from the text as well.
    if "407" in text or "proxy authentication" in low:
        return "auth", text
    if "timed out" in low or "timeout" in low:
        return "timeout", text
    return "tcp", text


def _parse_trace(body: str) -> Dict[str, str]:
    """``key=value`` lines from the probe endpoint's trace output."""
    out: Dict[str, str] = {}
    for line in (body or "").splitlines():
        key, sep, value = line.partition("=")
        key = key.strip()
        if sep and key and key not in out:
            out[key] = value.strip()
    return out


def _probe_summary(result: Dict) -> str:
    """One human-readable line for the Evasion page and the log."""
    took = f"{result.get('latency_ms', 0)}ms"
    if result.get("ok"):
        return (f"ok · {took} · exit {result.get('exit_ip') or 'unknown'}"
                f" · {result.get('geo') or '??'} · {result.get('timezone') or '??'}")
    stage = result.get("stage") or "error"
    err = result.get("error") or "failed"
    if stage == "auth":
        return f"auth rejected · HTTP 407 · {took} · check the proxy credentials"
    if stage == "http":
        return (f"proxy forwarded but the request returned "
                f"HTTP {result.get('status')} · {took}")
    if stage == "timeout":
        return f"timed out · no response in {took}"
    if stage == "tls":
        return f"TLS handshake failed · {took} · {err}"
    if stage == "tcp":
        return f"no connection · {took} · {err}"
    return f"{stage} · {err}"


class ProxyManager:
    """Handles proxies with optional sticky sessions per account_id."""

    def __init__(self, proxies: List[Dict] | None = None, sticky: bool = True,
                 health_check: bool = True, sticky_session: bool = True,
                 probe_url: str = DEFAULT_PROBE_URL,
                 probe_timeout: float = DEFAULT_PROBE_TIMEOUT):
        self.proxies: List[Dict] = list(proxies or [])
        # A slice reserved for harvester windows (rebuilt whenever the pool loads).
        self.harvester_proxies: List[Dict] = self._harvester_slice(self.proxies)
        self.sticky = sticky
        # Enables per-account {session} token substitution for residential pools.
        self.sticky_session = sticky_session
        self.health_check = health_check
        self._sticky_map: Dict[str, Dict] = {}
        self._index = 0
        self._health_cache: Dict[str, float] = {}
        # Servers a reachability probe could not connect to.
        self._bad: set = set()
        # Per-proxy sticky overrides keyed by proxy["server"]: True forces the
        # proxy into sticky binding, False keeps it out (round-robin only).
        # Absent/None inherits the group sticky flag. Populated when entries
        # carry "sticky" metadata; never by the legacy textarea edit path.
        self._sticky_overrides: Dict[str, bool] = {}
        # Per-proxy metadata (label, added_at, notes) keyed by server string.
        # Informational only — never placed into a Playwright proxy dict.
        self._proxy_meta: Dict[str, dict] = {}
        # Per-account "generation" counter; bumped by mark_bad to roll the token
        # (and thus the residential exit IP) after a burn.
        self._generation: Dict[str, int] = {}
        # What check_http() requests, and how long it waits. Plain attributes:
        # retarget the probe at your own endpoint at runtime without touching
        # this file (mgr.probe_url = "https://.../trace").
        self.probe_url: str = probe_url or DEFAULT_PROBE_URL
        self.probe_timeout: float = float(probe_timeout or DEFAULT_PROBE_TIMEOUT)

    @staticmethod
    def _harvester_slice(proxies: List[Dict]) -> List[Dict]:
        """Reserve up to a third of the pool for harvester traffic."""
        return list(proxies[: max(0, len(proxies) // 3)])

    @classmethod
    def from_lines(cls, lines: List, sticky: bool = True) -> "ProxyManager":
        """Build from proxy lines OR managed dict entries.

        See normalize_proxy_entry(): each entry is a plain line (host:port,
        http://user:pass@host:port, ...) or a dict with a "url" key plus
        metadata (label, added_at, notes, sticky). Metadata is stored beside
        the pool and sticky overrides are honored by get_proxy().
        """
        mgr = cls([], sticky=sticky)
        for entry in lines:
            url, meta = normalize_proxy_entry(entry)
            p = parse_proxy(url)
            if not valid(p):
                continue
            mgr.proxies.append(p)
            server = p.get("server", "")
            mgr._proxy_meta[server] = meta
            if meta.get("sticky") is not None:
                mgr._sticky_overrides[server] = bool(meta["sticky"])
        mgr.harvester_proxies = cls._harvester_slice(mgr.proxies)
        return mgr

    @classmethod
    def from_file(cls, path: str | Path, sticky: bool = True) -> "ProxyManager":
        p = Path(path)
        if not p.exists():
            return cls([], sticky=sticky)
        lines = p.read_text().splitlines()
        return cls.from_lines(lines, sticky=sticky)

    @classmethod
    def from_settings(cls, evasion: dict, *, proxies_file: str = "proxies.txt") -> "ProxyManager":
        """Build from config: inline list and/or proxies file."""
        sticky = bool(evasion.get("sticky", True))
        sticky_session = bool(evasion.get("sticky_session", True))
        file_path = evasion.get("proxies_file") or proxies_file
        mgr = cls.from_file(file_path, sticky=sticky)
        mgr.sticky_session = sticky_session
        inline = evasion.get("proxies") or []
        if inline:
            extra = cls.from_lines(
                list(inline) if isinstance(inline, list) else [],
                sticky=sticky,
            )
            mgr.proxies.extend(extra.proxies)
            # from_lines normalizes managed dict entries; carry their metadata
            # and sticky overrides onto this manager too.
            for server, meta in extra._proxy_meta.items():
                mgr._proxy_meta.setdefault(server, meta)
            for server, override in extra._sticky_overrides.items():
                mgr._sticky_overrides.setdefault(server, override)
        mgr.harvester_proxies = cls._harvester_slice(mgr.proxies)
        # Optional probe overrides; missing keys leave the defaults in place, so
        # an existing config behaves exactly as before.
        probe_url = evasion.get("probe_url")
        if probe_url:
            mgr.probe_url = str(probe_url)
        probe_timeout = evasion.get("probe_timeout")
        if probe_timeout:
            try:
                mgr.probe_timeout = float(probe_timeout)
            except (TypeError, ValueError):
                pass
        return mgr

    def reload_from_settings(self, evasion: dict, *, proxies_file: str = "proxies.txt") -> None:
        """Hot-reload proxy list (keeps sticky map for known accounts when possible)."""
        fresh = ProxyManager.from_settings(evasion, proxies_file=proxies_file)
        self.proxies = fresh.proxies
        self._sticky_overrides = dict(fresh._sticky_overrides)
        self._proxy_meta = dict(fresh._proxy_meta)
        self.harvester_proxies = self._harvester_slice(self.proxies)
        self.sticky = fresh.sticky
        self.sticky_session = fresh.sticky_session
        self.probe_url = fresh.probe_url
        self.probe_timeout = fresh.probe_timeout
        # Rebuild sticky map by server string so known accounts keep their exit IP
        # when the proxy still exists after reload.
        servers = {p.get("server"): p for p in self.proxies}
        new_map: Dict[str, Dict] = {}
        for aid, old in self._sticky_map.items():
            match = servers.get(old.get("server"))
            if match is not None:
                new_map[aid] = match
        self._sticky_map = new_map
        self._index = 0
        # Marks belong to the old pool; carrying them over poisons fresh proxies.
        self._bad.clear()
        self._health_cache.clear()

    @property
    def count(self) -> int:
        return len(self.proxies)

    def session_token(self, account_id: str) -> str:
        """Deterministic short token pinning a residential exit IP per account.

        Same ``account_id`` yields the same token across launches (stable exit
        IP, matching the sticky philosophy) until ``mark_bad`` bumps the
        account's generation counter, after which a fresh token is produced.
        """
        gen = self._generation.get(account_id, 0)
        digest = hashlib.sha256(f"{account_id}:{gen}".encode()).hexdigest()
        return digest[:8]

    def _session_key(self, account_id: Optional[str], for_harvester: bool) -> str:
        """Namespace so harvester tokens never collide with checkout tokens."""
        key = account_id or "_anon"
        return f"harvester:{key}" if for_harvester else key

    def _apply_session(self, proxy: Optional[Dict], account_id: Optional[str],
                       for_harvester: bool) -> Optional[Dict]:
        """Return a COPY with ``{session}`` filled in; never mutate the pool entry."""
        if proxy is None or not self.sticky_session:
            return proxy
        username = proxy.get("username")
        if not username or SESSION_PLACEHOLDER not in username:
            return proxy
        token = self.session_token(self._session_key(account_id, for_harvester))
        out = dict(proxy)
        out["username"] = username.replace(SESSION_PLACEHOLDER, token)
        return out

    def get_proxy(self, account_id: Optional[str] = None, *,
                  for_harvester: bool = False) -> Optional[Dict]:
        pool = self.harvester_proxies if for_harvester else self.proxies
        if not pool:
            # Harvester falls back to the main pool rather than going direct.
            pool = self.proxies if for_harvester else pool
        if not pool:
            return None

        chosen: Optional[Dict]
        # A per-proxy sticky=True forces sticky binding for its pool even when
        # the group flag is off; sticky=False keeps that proxy out of the
        # sticky map (round-robin only). With no overrides this is identical
        # to the old `self.sticky` branch.
        sticky_on = ((self.sticky or self._sticky_forced())
                     and account_id and not for_harvester)
        if sticky_on:
            current = self._sticky_map.get(account_id)
            if current is None or not self._is_healthy(current):
                healthy = [p for p in pool
                           if self._sticky_eligible(p) and self._is_healthy(p)]
                # With nothing healthy left, KEEP the existing binding rather than
                # re-rolling on every call - sticky mode exists to hold one exit
                # IP, and thrashing it is worse than holding a suspect one.
                if healthy:
                    self._sticky_map[account_id] = random.choice(healthy)
                elif current is None:
                    self._sticky_map[account_id] = random.choice(pool)
            chosen = self._sticky_map[account_id]
        else:
            # Round-robin, skipping unhealthy entries.
            chosen = pool[0]
            for _ in range(len(pool)):
                proxy = pool[self._index % len(pool)]
                self._index = (self._index + 1) % len(pool)
                if self._is_healthy(proxy):
                    chosen = proxy
                    break

        return self._apply_session(chosen, account_id, for_harvester)

    def _is_healthy(self, proxy: Dict) -> bool:
        """Best-effort health gate. Cache-only for now; real probing can hook here."""
        if not self.health_check:
            return True
        server = proxy.get("server")
        if server in self._bad:
            return False
        last = self._health_cache.get(server)
        if last is not None and time.time() - last < 300:
            return True
        self._health_cache[server] = time.time()
        return True

    def _sticky_eligible(self, proxy: Dict) -> bool:
        """False only for entries whose metadata sets sticky=False."""
        return self._sticky_overrides.get(proxy.get("server", ""), True)

    def _sticky_forced(self) -> bool:
        """True when any entry carries sticky=True metadata."""
        return any(self._sticky_overrides.values())

    def meta_for(self, proxy_or_server) -> dict:
        """Metadata for a pool entry (label/added_at/notes/sticky), or {}."""
        if isinstance(proxy_or_server, dict):
            server = proxy_or_server.get("server", "")
        else:
            server = str(proxy_or_server or "")
        return dict(self._proxy_meta.get(server, {}))

    def mark_bad(self, proxy: Dict, account_id: Optional[str] = None) -> None:
        """Burn a proxy for an account: drop its sticky binding AND roll the
        session token so the next get_proxy pins a fresh residential exit IP.

        On a single-gateway residential pool the server never changes, so the
        generation bump (not the sticky re-pick) is what actually rotates the IP.
        """
        self._bad.add(proxy.get("server", ""))
        if account_id:
            self._sticky_map.pop(account_id, None)
            key = self._session_key(account_id, for_harvester=False)
            self._generation[key] = self._generation.get(key, 0) + 1

    # -- editing + probing (used by the Evasion page) ---------------------- #
    def replace_lines(self, text: str) -> tuple:
        """Swap the pool for the proxies in `text`. Returns (kept, rejected)."""
        kept: List[Dict] = []
        rejected: List[str] = []
        for line in (text or "").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            parsed = parse_proxy(stripped)
            if not valid(parsed):
                rejected.append(stripped)
                continue
            kept.append(parsed)
        servers = {p.get("server"): p for p in kept}
        self.proxies = kept
        self.harvester_proxies = self._harvester_slice(kept)
        self._bad.clear()
        self._health_cache.clear()
        # Text-only edit: there is no metadata to carry, so drop any.
        self._sticky_overrides = {}
        self._proxy_meta = {}
        # Keep an account on its exit IP when that proxy survived the edit.
        self._sticky_map = {aid: servers[old.get("server")]
                            for aid, old in self._sticky_map.items()
                            if old.get("server") in servers}
        self._index = 0
        return len(kept), rejected

    def as_lines(self) -> str:
        """The pool back as editable text, credentials intact."""
        out: List[str] = []
        for p in self.proxies:
            u = urlparse(p["server"])
            auth = ""
            if p.get("username"):
                auth = p["username"]
                if p.get("password"):
                    auth += f":{p['password']}"
                auth += "@"
            port = f":{u.port}" if u.port else ""
            # u.hostname lowercases; netloc preserves the case parse_proxy stored,
            # so a round-trip still matches the in-memory server string.
            host = u.netloc.rsplit("@", 1)[-1]
            if port and host.endswith(port):
                host = host[: -len(port)]
            out.append(f"{u.scheme}://{auth}{host}{port}")
        return "\n".join(out)

    def save_to(self, path: str | Path) -> None:
        """Write the pool out. Refuses to blank an existing file: an empty pool
        is nearly always a paste that failed validation, and overwriting would
        destroy the user's credentials."""
        target = Path(path)
        if not self.proxies and target.exists() and target.read_text().strip():
            raise ValueError(
                f"refusing to blank {target} - the pool is empty, so nothing "
                "would be saved. Fix or remove the rejected lines first.")
        target.write_text((self.as_lines() + "\n") if self.proxies else "")

    def check(self, proxy: Dict, timeout: float = 3.0) -> bool:
        """Best-effort TCP reachability of the proxy endpoint (needs no auth)."""
        host, port = host_port(proxy)
        if not host:
            return False
        try:
            with socket.create_connection((host, port), timeout=timeout):
                self._bad.discard(proxy.get("server", ""))
                return True
        except OSError:
            self._bad.add(proxy.get("server", ""))
            return False

    def check_all(self, timeout: float = 3.0) -> int:
        return sum(1 for p in self.proxies if self.check(p, timeout=timeout))

    def check_http(self, proxy: Dict, timeout: Optional[float] = None) -> Dict:
        """Send a real HTTPS request THROUGH ``proxy`` and report what happened.

        ``check()`` above proves only that something is listening on the port.
        This drives the whole path a launch depends on — CONNECT tunnel, TLS to
        the origin, a response body — and says where it stopped when it stops:

        * ``config``  — the proxy dict itself is unusable (no host/port).
        * ``tcp``     — nothing answered, or the name did not resolve.
        * ``tls``     — the connection came up but the handshake failed.
        * ``auth``    — the proxy rejected the credentials (HTTP 407).
        * ``timeout`` — no response inside the budget.
        * ``http``    — the proxy forwarded, the endpoint answered non-2xx.
        * ``ok``      — 2xx came back through the proxy.

        Returns ``{ok, stage, status, latency_ms, error, exit_ip, geo, timezone,
        summary}``. ``latency_ms`` is the whole round-trip, so it is also the
        cheapest read on how much this proxy will cost every page load.

        On success the exit details are ALSO written back onto the ``proxy``
        dict passed in (``exit_ip``/``geo``/``timezone``) — that is what lets a
        fingerprint be aligned to the country the exit is really in rather than
        the one the pool file claims.

        Nothing else is mutated: a failed probe does NOT mark the proxy bad, so
        one flaky check cannot quietly empty the pool. Call ``mark_bad()``
        yourself if that is the policy you want.

        Blocking, and a full network round-trip — it belongs on a health screen
        or a pre-launch check, not in a tight loop.
        """
        budget = float(timeout if timeout is not None else self.probe_timeout)
        out: Dict = {"ok": False, "stage": "config", "status": 0,
                     "latency_ms": 0, "error": "", "exit_ip": "", "geo": "",
                     "timezone": "", "summary": ""}
        if not valid(proxy):
            out["error"] = "unusable proxy: host or port missing/malformed"
            out["summary"] = _probe_summary(out)
            return out
        url = _proxy_url(proxy)
        if not url:
            out["error"] = "could not build a proxy URL from the server string"
            out["summary"] = _probe_summary(out)
            return out

        # Bound BEFORE the try: the non-2xx and error paths read it below, and an
        # exception raised before the first assignment would otherwise surface as
        # an UnboundLocalError instead of the failure that actually happened.
        body = ""
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": url, "https": url}))
        request = urllib.request.Request(self.probe_url)
        started = time.monotonic()
        try:
            with opener.open(request, timeout=budget) as response:
                out["status"] = int(getattr(response, "status", 0)
                                    or response.getcode() or 0)
                body = response.read(4096).decode("utf-8", "replace")
                out["ok"] = 200 <= out["status"] < 300
                out["stage"] = "ok" if out["ok"] else "http"
                if not out["ok"]:
                    out["error"] = f"HTTP {out['status']}"
        except urllib.error.HTTPError as exc:      # subclass of URLError: first
            out["status"] = int(getattr(exc, "code", 0) or 0)
            out["stage"] = "auth" if out["status"] == 407 else "http"
            out["error"] = f"HTTP {out['status']} {getattr(exc, 'reason', '')}".strip()
            try:
                body = exc.read(4096).decode("utf-8", "replace")
            except Exception:                      # noqa: BLE001
                body = ""
        except urllib.error.URLError as exc:
            out["stage"], out["error"] = _classify_urlerror(exc)
            if out["stage"] == "auth" and not out["status"]:
                # A CONNECT tunnel refused for auth never reaches HTTPError, so
                # the status the caller reads would otherwise stay 0 while the
                # summary said 407. Same failure, one number.
                out["status"] = 407
        except (socket.timeout, TimeoutError) as exc:
            out["stage"], out["error"] = "timeout", str(exc) or "timed out"
        except ssl.SSLError as exc:
            out["stage"], out["error"] = "tls", str(exc)
        except OSError as exc:
            out["stage"], out["error"] = "tcp", str(exc)
        except Exception as exc:                   # noqa: BLE001
            out["stage"], out["error"] = "http", f"{type(exc).__name__}: {exc}"
        finally:
            out["latency_ms"] = int((time.monotonic() - started) * 1000)

        trace = _parse_trace(body)
        out["exit_ip"] = trace.get("ip", "")
        out["geo"] = (trace.get("loc") or "").upper()
        out["timezone"] = trace.get("tz", "")
        if out["ok"]:
            for key in ("exit_ip", "geo", "timezone"):
                if out[key]:
                    proxy[key] = out[key]
        out["summary"] = _probe_summary(out)
        return out

    def is_bad(self, proxy: Dict) -> bool:
        return proxy.get("server", "") in self._bad

    @property
    def healthy_count(self) -> int:
        return sum(1 for p in self.proxies if not self.is_bad(p))

    def sticky_for(self, account_id: str) -> Optional[Dict]:
        return self._sticky_map.get(account_id)

    def clear_sticky(self, account_id: Optional[str] = None) -> None:
        if account_id is None:
            self._sticky_map.clear()
        else:
            self._sticky_map.pop(account_id, None)
