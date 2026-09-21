"""Discord webhook provider. This is the reference provider — copy it to add
Slack, Telegram, email, etc. Uses only urllib, so no extra dependencies."""
from __future__ import annotations

import json
import urllib.request

from raindance.core.notifications import (
    NotificationEvent,
    NotificationProvider,
    register_provider,
)


@register_provider
class DiscordProvider(NotificationProvider):
    id = "discord"
    name = "Discord"
    description = "Post alerts to a Discord channel via an incoming webhook."

    def __init__(self):
        self.enabled = False
        self.webhook = ""

    def config_fields(self) -> list[dict]:
        return [
            {"key": "enabled", "label": "Enabled", "type": "bool"},
            {"key": "webhook", "label": "Webhook URL", "type": "secret",
             "placeholder": "https://discord.com/api/webhooks/..."},
        ]

    def configure(self, settings: dict) -> None:
        self.enabled = bool(settings.get("enabled", False))
        self.webhook = (settings.get("webhook") or "").strip()

    def is_ready(self) -> bool:
        return self.enabled and self.webhook.startswith("http")

    def send(self, event: NotificationEvent) -> None:
        content = f"**{event.title}**\n{event.message}"
        if event.url:
            content += f"\n{event.url}"
        data = json.dumps({"content": content}).encode("utf-8")
        req = urllib.request.Request(
            self.webhook, data=data,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        urllib.request.urlopen(req, timeout=8)
