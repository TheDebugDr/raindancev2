"""Structured failure forensics — append-only JSONL for bot walls and denials.

Pure observation: this module never changes run behavior. It does not retry,
change gates, touch CAPTCHA handling, or make any purchase decision. Every
public function swallows its own exceptions and `log_failure` returns a bool,
so a logging failure can never break, slow, or mask the run it is observing.

One JSONL record is appended per denial/failure to data/failure_forensics.jsonl:

    {"timestamp": "2026-09-21T17:40:12.123456+00:00", "kind": "bot_wall",
     "task_id": "task_12c14398", "site": "target",
     "url": "https://www.target.com/p/...", "page_title": "Robot or Human?",
     "classification": "blocked", "detector": "robot or human",
     "attempt": 2, "action": "aborted_before_add_to_cart",
     "outcome": "denied:blocked",
     "screenshot": "data/screenshots/20260921-134012-task_12c14398-denied.png",
     "evasion": {"evasion_on": true, "proxy": "direct",
                 "fingerprint": "fp_9f31", "headless": false}}

kinds: bot_wall (BLOCKED classification) | gate_denied (any other gate
refusal: over-MSRP, unverified, search-resolution, cart-check) | failed
(queue/ATC/checkout failures) | error (uncaught exception).

Query example — every Target bot wall in the last 7 days (needs jq):

    jq -c --arg since "$(date -u -v-7d +%Y-%m-%dT%H:%M:%SZ)" \
      'select(.kind=="bot_wall" and .site=="target" and .timestamp >= $since)' \
      data/failure_forensics.jsonl
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_PATH = "data/failure_forensics.jsonl"


def utc_now_iso() -> str:
    """Current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def mask_proxy(proxy) -> str:
    """Proxy exit for the log: 'direct' when none, else credentials masked.

    Accepts the proxy dict the browser factory hands around
    ({"server": "http://user:pass@host:port", ...}), a bare server
    string, or None. Credentials NEVER reach the log.
    """
    try:
        server = ""
        if isinstance(proxy, dict):
            server = str(proxy.get("server") or "")
        elif isinstance(proxy, str):
            server = proxy
        elif proxy:
            return "direct"  # unrecognised type — never log its repr
        server = server.strip()
        if not server:
            return "direct"
        if "://" in server:
            scheme, rest = server.split("://", 1)
            if "@" in rest:
                return f"{scheme}://***@{rest.rsplit('@', 1)[1]}"
            return server  # no credentials present — nothing to hide
        if "@" in server:
            return f"***@{server.rsplit('@', 1)[1]}"
        return server
    except Exception:
        return "direct"


def evasion_snapshot(*, evasion_on, proxy, fingerprint, headless) -> dict:
    """Build the evasion half of a forensics record. The proxy is masked."""
    try:
        return {
            "evasion_on": bool(evasion_on),
            "proxy": mask_proxy(proxy),
            "fingerprint": str(fingerprint or "n/a"),
            "headless": bool(headless),
        }
    except Exception:
        return {"evasion_on": False, "proxy": "direct",
                "fingerprint": "n/a", "headless": False}


def log_failure(*, settings=None, path: str | None = None, kind: str,
                task_id: str = "", site: str = "", url: str = "",
                title: str = "", classification: str = "",
                detector: str | None = None, attempt: int = 0,
                action: str = "", outcome: str = "", screenshot: str = "",
                evasion: dict | None = None) -> bool:
    """Append ONE forensics record as a single JSONL line.

    Never raises — returns True when the line was written, False otherwise.
    No payment data, no profile PII, no proxy credentials are ever recorded.
    """
    try:
        record = {
            "timestamp": utc_now_iso(),
            "kind": kind,
            "task_id": str(task_id or ""),
            "site": str(site or ""),
            "url": str(url or ""),
            "page_title": str(title or "")[:200],
            "classification": str(classification or ""),
            "detector": str(detector or ""),
            "attempt": int(attempt or 0),
            "action": str(action or ""),
            "outcome": str(outcome or ""),
            "screenshot": str(screenshot or ""),
            "evasion": dict(evasion or {}),
        }
        dest = path
        if not dest and settings is not None:
            try:
                dest = (settings.data or {}).get("forensics_path")
            except Exception:
                dest = None
        dest = Path(dest or DEFAULT_PATH)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False
