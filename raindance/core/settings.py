"""App settings: load/save config.json and translate it into the config shape
the existing CheckoutBot backend expects.

Note on secrets: we deliberately store **no login credentials**. Staying logged
in is handled by a persistent browser profile (the browser keeps the session
cookie), so the app never sees your password. The only sensitive value stored
is an optional Discord webhook, which the GUI renders masked and which lives in
config.json (gitignored).
"""
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

APP_CONFIG = Path("config.json")

# Every writer of config.json serializes on this. The monitor saves twice per
# task per poll, up to 8 runner threads save per phase transition, and the
# scanner saves per product — all rewriting the same 24 KB file.
_CONFIG_LOCK = threading.RLock()

DEFAULTS: dict = {
    "site": {
        "name": "My Store",
        "login_url": "",
        # Checkout is a list of steps shared by this one site. Edited in the UI.
        "checkout_steps": [],
        # Default stock-detection heuristics for browser watch (Orders tool).
        "in_stock": {
            "text_absent": "Sold Out",
            "text_present": "Add to Cart",
            "timeout_ms": 8000,
        },
    },
    "targets": [
        # {"id","name","url","in_stock":{selector,text_present,text_absent},"enabled"}
    ],
    "active_target": None,
    # Monitored products for the dashboard (managed by ProductStore).
    "products": [],
    # Discovery search. Set serpapi_key to use live SerpApi Google Shopping;
    # empty → offline mock. serpapi_base is for testing/self-hosting (blank = default).
    "search": {"serpapi_key": "", "serpapi_base": ""},
    "poll": {"interval_seconds": 10, "jitter_seconds": 5, "max_attempts": 0},
    "schedule": {"start_at": ""},
    "browser": {
        "headless": False,
        "user_data_dir": "profile",
        "slow_mo_ms": 0,
        "viewport": {"width": 1280, "height": 900},
    },
    # Optional browser evasion layer (stealth + proxies). Applied only when
    # CheckoutEngine / Login Session open a real Playwright browser.
    "evasion": {
        "enabled": False,
        "sticky": True,
        "account_id": "default",
        "proxies_file": "proxies.txt",
        "proxies": [],
    },
    "checkout": {"dry_run": True, "force_dry_run": False},
    # Multi-task bot (Phases 0–8)
    "tasks": [],
    "proxy_groups": {
        "default": {"file": "proxies.txt", "proxies": [], "sticky": True},
        "residential": {"file": "proxies_residential.txt", "proxies": [], "sticky": True},
        "isp": {"file": "proxies_isp.txt", "proxies": [], "sticky": True},
    },
    "captcha": {
        "mode": "manual",           # manual | api
        "timeout_seconds": 300,
        "poll_seconds": 1.5,
        "capsolver_key": "",
        "twocaptcha_key": "",
    },
    "orchestrator": {
        "max_workers": 8,
    },
    "notifications": {
        "discord": {"enabled": False, "webhook": ""},
        "desktop": {"enabled": True},
        "sound": {"enabled": True},
    },
    "screenshots_dir": "data/screenshots",
}


def _deep_merge(base: dict, over: dict) -> dict:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


class Settings:
    def __init__(self, path: Path = APP_CONFIG):
        self.path = Path(path)
        self.data: dict = copy.deepcopy(DEFAULTS)
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        with _CONFIG_LOCK:
            try:
                loaded = json.loads(self.path.read_text())
            except Exception as exc:
                # An unreadable config used to fall through to DEFAULTS in
                # silence, and the next save() — seconds later, from the
                # monitor — overwrote the only copy. config.json is gitignored
                # and nothing else holds products, tasks, catalog entries or
                # proxy groups, so move it aside before carrying on.
                self._quarantine(exc)
                return
            if not isinstance(loaded, dict):
                self._quarantine(TypeError(f"top level is {type(loaded).__name__}, not object"))
                return
            self.data = _deep_merge(copy.deepcopy(DEFAULTS), loaded)

    def _quarantine(self, error: Exception) -> None:
        """Rename an unreadable config aside so save() cannot overwrite it."""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        aside = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
        # Second resolution collides when a bad config is loaded twice in the
        # same second; never clobber an earlier quarantine.
        bump = 1
        while aside.exists():
            aside = self.path.with_name(f"{self.path.name}.corrupt-{stamp}-{bump}")
            bump += 1
        try:
            os.replace(self.path, aside)
        except OSError:
            aside = None
        sys.stderr.write(
            f"[settings] {self.path} is unreadable ({error}); "
            + (f"kept a copy at {aside.name}; " if aside else "could not move it aside; ")
            + "starting from defaults\n"
        )

    def save(self) -> None:
        """Replace config.json atomically.

        A truncate-then-write leaves an unparseable file if the app dies mid
        write, and concurrent writers can interleave into one. Write a sibling
        temp file, fsync it, then rename: a reader sees either the whole old
        file or the whole new one, never a partial.
        """
        with _CONFIG_LOCK:
            payload = json.dumps(self.data, indent=2)
            directory = self.path.parent if str(self.path.parent) != "" else Path(".")
            directory.mkdir(parents=True, exist_ok=True)
            tmp = None
            try:
                fd, tmp = tempfile.mkstemp(
                    dir=str(directory), prefix=f".{self.path.name}.", suffix=".tmp")
                with os.fdopen(fd, "w") as fh:
                    fh.write(payload)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self.path)
                tmp = None
            finally:
                if tmp is not None and os.path.exists(tmp):
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass

    # -- target helpers ---------------------------------------------------- #
    def targets(self) -> list[dict]:
        return self.data.setdefault("targets", [])

    def active_target(self) -> dict | None:
        tid = self.data.get("active_target")
        for t in self.targets():
            if t.get("id") == tid:
                return t
        enabled = [t for t in self.targets() if t.get("enabled", True)]
        return enabled[0] if enabled else (self.targets()[0] if self.targets() else None)

    # -- translation to the CheckoutBot backend config --------------------- #
    def bot_config(self, target: dict) -> dict:
        d = self.data
        browser = dict(d.get("browser") or {})
        # Propagate sticky account id so BrowserFactory can pin a proxy.
        evasion = d.get("evasion") or {}
        if evasion.get("account_id"):
            browser["account_id"] = evasion["account_id"]
        return {
            "product": {
                "name": target.get("name") or target.get("url", ""),
                "url": target.get("url", ""),
                "in_stock": target.get("in_stock", {}),
            },
            "checkout_steps": d["site"].get("checkout_steps", []),
            # Persistent profile handles staying signed in; no scripted login.
            "login": {"enabled": False},
            "poll": d["poll"],
            "schedule": d["schedule"],
            "browser": browser,
            "evasion": dict(evasion),
            "notifications": {},         # routed through the hub instead
            "screenshots_dir": d.get("screenshots_dir", "screenshots"),
        }
