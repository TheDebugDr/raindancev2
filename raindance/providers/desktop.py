"""Native desktop notification banner (macOS / Linux)."""
from __future__ import annotations

import subprocess
import sys

from raindance.core.notifications import (
    NotificationEvent,
    NotificationProvider,
    register_provider,
)


@register_provider
class DesktopProvider(NotificationProvider):
    id = "desktop"
    name = "Desktop banner"
    description = "Show a native OS notification banner when something happens."

    def __init__(self):
        self.enabled = True

    def config_fields(self) -> list[dict]:
        return [{"key": "enabled", "label": "Enabled", "type": "bool"}]

    def configure(self, settings: dict) -> None:
        self.enabled = bool(settings.get("enabled", True))

    def is_ready(self) -> bool:
        return self.enabled

    def send(self, event: NotificationEvent) -> None:
        title = event.title.replace('"', "'")
        msg = event.message.replace('"', "'")
        if sys.platform == "darwin":
            subprocess.run(
                ["osascript", "-e",
                 f'display notification "{msg}" with title "{title}"'],
                check=False, capture_output=True, timeout=5,
            )
        elif sys.platform.startswith("linux"):
            subprocess.run(["notify-send", title, msg],
                           check=False, capture_output=True, timeout=5)
