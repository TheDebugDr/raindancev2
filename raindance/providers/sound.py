"""Audible alert: a system chime plus a terminal bell."""
from __future__ import annotations

import subprocess
import sys

from raindance.core.notifications import (
    NotificationEvent,
    NotificationProvider,
    register_provider,
)


@register_provider
class SoundProvider(NotificationProvider):
    id = "sound"
    name = "Sound chime"
    description = "Play a chime (louder for in-stock) plus a terminal bell."

    def __init__(self):
        self.enabled = True

    def config_fields(self) -> list[dict]:
        return [{"key": "enabled", "label": "Enabled", "type": "bool"}]

    def configure(self, settings: dict) -> None:
        self.enabled = bool(settings.get("enabled", True))

    def is_ready(self) -> bool:
        return self.enabled

    def send(self, event: NotificationEvent) -> None:
        print("\a", end="", flush=True)
        if sys.platform == "darwin":
            chime = "Glass.aiff" if event.kind == "in_stock" else "Ping.aiff"
            subprocess.Popen(
                ["afplay", f"/System/Library/Sounds/{chime}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
