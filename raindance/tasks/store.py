"""Persist tasks in config.json under settings.data['tasks']."""
from __future__ import annotations

from typing import Optional

from raindance.tasks.models import normalize_task, now_iso, new_task_id


class TaskStore:
    def __init__(self, settings):
        self.settings = settings
        self.settings.data.setdefault("tasks", [])

    @property
    def items(self) -> list[dict]:
        return self.settings.data["tasks"]

    def list(self, *, enabled_only: bool = False) -> list[dict]:
        items = [normalize_task(t) for t in self.items]
        if enabled_only:
            return [t for t in items if t.get("enabled", True)]
        return items

    def get(self, tid: str) -> Optional[dict]:
        for t in self.items:
            if t.get("id") == tid:
                return normalize_task(t)
        return None

    def add(self, **fields) -> dict:
        t = normalize_task(fields)
        if not t.get("id"):
            t["id"] = new_task_id()
        t["created"] = now_iso()
        t["updated"] = now_iso()
        self.items.append(t)
        self.save()
        return t

    def update(self, tid: str, **fields) -> Optional[dict]:
        for i, t in enumerate(self.items):
            if t.get("id") == tid:
                merged = dict(t)
                merged.update(fields)
                merged["id"] = tid
                merged["updated"] = now_iso()
                self.items[i] = normalize_task(merged)
                self.save()
                return self.items[i]
        return None

    def set_runtime(self, tid: str, **fields) -> None:
        """Update runtime fields without full normalize churn."""
        for t in self.items:
            if t.get("id") == tid:
                t.update(fields)
                t["updated"] = now_iso()
                self.save()
                return

    def remove(self, tid: str) -> None:
        self.settings.data["tasks"] = [t for t in self.items if t.get("id") != tid]
        self.save()

    def save(self) -> None:
        self.settings.save()
