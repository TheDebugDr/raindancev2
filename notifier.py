"""
notifier — best-effort alerts for the auto-checkout monitor.

All channels are optional and fail quietly (a broken webhook should never
crash your watch loop). No third-party dependencies: Discord uses urllib,
desktop/sound use the OS's own tools.
"""
from __future__ import annotations

import json
import subprocess
import sys
import urllib.request
from datetime import datetime


class Notifier:
    """Fan a single event out to console + Discord + desktop + sound."""

    def __init__(self, cfg: dict | None = None):
        cfg = cfg or {}
        self.discord_webhook = (cfg.get("discord_webhook") or "").strip()
        self.desktop = cfg.get("desktop", True)
        self.sound = cfg.get("sound", True)
        # Which events to actually push. Empty/absent = all.
        self.events = set(cfg.get("on", [])) or None

    def notify(self, event: str, title: str, message: str, url: str | None = None) -> None:
        if self.events is not None and event not in self.events:
            return
        line = f"{title} — {message}" + (f"  {url}" if url else "")
        stamp = datetime.now().strftime("%H:%M:%S")
        print(f"[{stamp}] 🔔 {line}", flush=True)
        self._discord(event, title, message, url)
        if self.desktop:
            self._desktop(title, message)
        if self.sound:
            self._sound(event)

    # -- channels ---------------------------------------------------------- #
    def _discord(self, event: str, title: str, message: str, url: str | None) -> None:
        if not self.discord_webhook:
            return
        content = f"**{title}**\n{message}" + (f"\n{url}" if url else "")
        payload = json.dumps({"content": content}).encode("utf-8")
        req = urllib.request.Request(
            self.discord_webhook,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=8)
        except Exception as e:
            print(f"    (discord notify failed: {e})", flush=True)

    def _desktop(self, title: str, message: str) -> None:
        try:
            if sys.platform == "darwin":
                safe_msg = message.replace('"', "'")
                safe_title = title.replace('"', "'")
                subprocess.run(
                    ["osascript", "-e",
                     f'display notification "{safe_msg}" with title "{safe_title}"'],
                    check=False, capture_output=True, timeout=5,
                )
            elif sys.platform.startswith("linux"):
                subprocess.run(["notify-send", title, message],
                               check=False, capture_output=True, timeout=5)
            # Windows: fall back to the console line + bell (below).
        except Exception:
            pass  # desktop notifications are a nicety, never fatal

    def _sound(self, event: str) -> None:
        try:
            print("\a", end="", flush=True)  # terminal bell, works everywhere
            if sys.platform == "darwin":
                # A distinct, attention-grabbing sound for the moment it matters.
                sound = "Glass.aiff" if event == "in_stock" else "Ping.aiff"
                subprocess.Popen(
                    ["afplay", f"/System/Library/Sounds/{sound}"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
        except Exception:
            pass
