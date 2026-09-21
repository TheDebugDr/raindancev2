"""Proxy pool with a REAL operational health check.

Old check(): TCP connect to host:port only. This version exercises the
full path and classifies failures. Default probe is the Cloudflare trace
endpoint: 200 OK, tiny body, and it reports the exit IP, country (loc=)
and IANA timezone (tz=) — feeding fingerprint alignment with *observed*
geo instead of assumptions.

Detection-driven requirements baked in:
* sticky per-account exits (Queue-it invalidates tokens on mid-session
  IP change; rotating proxies scored 0% in 2025 tests) [2]
* datacenter ranges get challenged/hard-blocked by Queue-it — use
  residential exits [3][4]
"""
from __future__ import annotations

import random
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

_DEFAULT_PROBE_URL = "https://www.cloudflare.com/cdn-cgi/trace"


class ProxyManager:
    def __init__(self, proxies: Optional[List[Dict]] = None,
                 probe_url: str = _DEFAULT_PROBE_URL,
                 probe_timeout: float = 15.0):
        self._pool: List[Dict] = list(proxies or [])
        self._sticky: Dict[str, Dict] = {}
        self.probe_url = probe_url
        self.probe_timeout = probe_timeout

    def add(self, proxy: Dict) -> None:
        self._pool.append(proxy)

    def get_proxy(self, account_id: str, for_harvester: bool = False) -> Optional[Dict]:
        """Account-sticky exit selection."""
        if not self._pool:
            return None
        if account_id not in self._sticky:
            self._sticky[account_id] = random.choice(self._pool)
        return self._sticky[account_id]

    def release(self, account_id: str) -> None:
        self._sticky.pop(account_id, None)

    # ------------------------------------------------------------------ #
    def check(self, proxy: Dict) -> Dict[str, Any]:
        """Full request/response test THROUGH the proxy. Enriches `proxy`
        in place with exit_ip/geo/timezone on success."""
        res: Dict[str, Any] = {"ok": False, "stage": None, "status": None,
                                "latency_ms": None, "error": None,
                                "exit_ip": None, "geo": None, "timezone": None}
        server = proxy.get("server") or ""
        scheme, _, hostport = server.partition("://")
        host, _, port = hostport.partition(":")
        try:
            port_n = int(port or (443 if scheme == "https" else 80))
        except ValueError:
            res.update(stage="config", error=f"bad proxy server: {server!r}")
            res["summary"] = f"proxy check FAILED: {res['error']}"
            return res

        # Stage 1: TCP reachability (legacy semantics, kept as a preflight).
        t0 = time.monotonic()
        try:
            with socket.create_connection((host, port_n), timeout=self.probe_timeout):
                pass
        except OSError as exc:
            res.update(stage="tcp", error=f"connect failed: {exc}")
            res["summary"] = f"proxy check FAILED: {res['error']}"
            return res

        # Stage 2: real HTTPS request through the proxy.
        proxy_url = server
        if proxy.get("username"):
            auth = f"{urllib.parse.quote(str(proxy['username']), safe='')}:" \
                   f"{urllib.parse.quote(str(proxy.get('password', '')), safe='')}"
            proxy_url = f"{scheme}://{auth}@{hostport}"
        handler = urllib.request.ProxyHandler(
            {"http": proxy_url, "https": proxy_url})
        opener = urllib.request.build_opener(handler)
        try:
            with opener.open(self.probe_url, timeout=self.probe_timeout) as resp:
                body = resp.read(4096).decode("utf-8", "replace")
                res["status"] = resp.status
        except urllib.error.HTTPError as exc:
            res.update(stage="http", status=exc.code, error=f"HTTP {exc.code}")
            if exc.code == 407:
                res["stage"] = "auth"  # authentication failure, per spec
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, (socket.timeout, TimeoutError)):
                res.update(stage="timeout", error="proxy request timed out")
            elif isinstance(reason, ssl.SSLError):
                res.update(stage="tls", error=f"TLS through proxy failed: {reason}")
            elif isinstance(reason, ConnectionRefusedError):
                res.update(stage="tcp", error="proxy refused CONNECT")
            else:
                res.update(stage="http", error=f"request failed: {reason}")
        except Exception as exc:  # defensive
            res.update(stage="http", error=f"{type(exc).__name__}: {exc}")
        finally:
            res["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)

        if res["status"] and 200 <= res["status"] < 300:
            for line in body.splitlines():
                if line.startswith("ip="):
                    res["exit_ip"] = line[3:]
                elif line.startswith("loc="):
                    res["geo"] = line[4:]
                elif line.startswith("tz="):
                    res["timezone"] = line[3:]
            proxy.update({k: res[k] for k in
                         ("exit_ip", "geo", "timezone") if res[k]})
            res["ok"] = True
            res["stage"] = "http"
            res["summary"] = (f"proxy ok: {res['status']} in {res['latency_ms']}ms "
                             f"via {res['exit_ip']} ({res['geo']}/{res['timezone']})")
        elif not res["error"]:
            res["error"] = f"unexpected status {res['status']}"
        if not res["ok"] and "summary" not in res:
            res["summary"] = (f"proxy check FAILED at {res['stage']}: "
                              f"{res['error']} ({res['latency_ms']}ms)")
        return res
