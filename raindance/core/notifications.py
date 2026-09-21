"""Provider-agnostic notifications.

Discord is just one provider. A provider is a self-contained unit that declares
the settings fields it needs (`config_fields`) and knows how to `send`. Adding
Slack / Telegram / email is a new file in `providers/` — the hub, the GUI
settings form, and every other provider stay untouched.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional


@dataclass
class NotificationEvent:
    kind: str                    # in_stock | checkout_success | error | info
    title: str
    message: str
    url: Optional[str] = None


class NotificationProvider:
    id: str = ""
    name: str = "Provider"
    description: str = ""

    def config_fields(self) -> list[dict]:
        """Fields the GUI should render. Each: {key,label,type,placeholder?}.
        type ∈ {"bool","text","secret"}. 'secret' renders masked."""
        return []

    def configure(self, settings: dict) -> None:
        """Apply this provider's slice of the config (e.g. {enabled, webhook})."""

    def is_ready(self) -> bool:
        """True when enabled and sufficiently configured to send."""
        return False

    def send(self, event: NotificationEvent) -> None:
        raise NotImplementedError


PROVIDER_REGISTRY: dict[str, type] = {}


def register_provider(cls):
    if not getattr(cls, "id", ""):
        raise ValueError(f"{cls.__name__} must set a unique .id")
    if cls.id in PROVIDER_REGISTRY:
        raise ValueError(f"duplicate provider id: {cls.id!r}")
    PROVIDER_REGISTRY[cls.id] = cls
    return cls


class NotificationHub:
    """Instantiates all registered providers and fans events out to the ready
    ones. Sends run on a small thread pool so a slow webhook never blocks."""

    def __init__(self, bus=None):
        self.bus = bus
        self._providers: dict[str, NotificationProvider] = {}
        self._pool = ThreadPoolExecutor(max_workers=4)

    def load(self) -> None:
        self._providers = {pid: cls() for pid, cls in PROVIDER_REGISTRY.items()}

    def configure(self, notif_settings: dict) -> None:
        for pid, prov in self._providers.items():
            try:
                prov.configure(notif_settings.get(pid, {}))
            except Exception as e:  # noqa: BLE001
                if self.bus:
                    self.bus.log(f"configure {pid} failed: {e}", "warn")

    def providers(self) -> list[NotificationProvider]:
        return list(self._providers.values())

    def notify(self, event: NotificationEvent) -> None:
        for prov in self._providers.values():
            if prov.is_ready():
                self._pool.submit(self._safe_send, prov, event)

    def _safe_send(self, prov: NotificationProvider, event: NotificationEvent) -> None:
        try:
            prov.send(event)
        except Exception as e:  # noqa: BLE001 - one provider must not break others
            if self.bus:
                self.bus.log(f"notify via {prov.id} failed: {e}", "warn")
