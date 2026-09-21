"""The monitored-products store.

A flat list of product dicts persisted in config.json. The UI groups them by
`site` into tabs, so tabs are derived (dynamic) rather than declared — add a
product on a new retailer and its tab appears on the next render.

Product dict shape:
    id, name, url, site, price_threshold, frequency,
    status, price, image, last_checked, created
"""
from __future__ import annotations

from datetime import datetime

from raindance.core.sites import detect_site


class ProductStore:
    def __init__(self, settings):
        self.settings = settings
        self.settings.data.setdefault("products", [])

    @property
    def items(self) -> list[dict]:
        return self.settings.data["products"]

    def add(self, *, name: str, url: str, price_threshold=None, frequency: str = "30s",
            site: str | None = None, image: str | None = None, price=None,
            status: str = "pending", priority: str = "normal") -> dict:
        product = {
            "id": "p" + datetime.now().strftime("%Y%m%d%H%M%S%f"),
            "name": name or url,
            "url": url,
            "site": site or detect_site(url),
            "price_threshold": price_threshold,
            "frequency": frequency,
            # high | normal | low — the wave's must-get items poll fastest and
            # commit to checkout first.
            "priority": priority if priority in ("high", "normal", "low") else "normal",
            "status": status,
            "price": price,
            "image": image,
            "last_checked": None,
            "created": datetime.now().isoformat(timespec="seconds"),
        }
        self.items.append(product)
        self.save()
        return product

    def remove(self, pid: str) -> None:
        self.settings.data["products"] = [p for p in self.items if p["id"] != pid]
        self.save()

    def update(self, pid: str, **fields) -> None:
        for p in self.items:
            if p["id"] == pid:
                p.update(fields)
                break
        self.save()

    def get(self, pid: str) -> dict | None:
        return next((p for p in self.items if p["id"] == pid), None)

    def sites(self) -> list[str]:
        return sorted({p["site"] for p in self.items})

    def by_site(self) -> dict[str, list[dict]]:
        grouped: dict[str, list[dict]] = {}
        for p in self.items:
            grouped.setdefault(p["site"], []).append(p)
        return grouped

    def for_site(self, site: str) -> list[dict]:
        return [p for p in self.items if p["site"] == site]

    def save(self) -> None:
        self.settings.save()
