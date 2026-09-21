"""A tiny thread-safe event/log bus.

The backend runs on a worker thread; the GUI runs on the UI thread. The bus is
the seam between them: the worker `log()`s onto it, the UI drains it on a timer.
Nothing here touches the UI, so it stays import-safe and unit-testable.
"""
from __future__ import annotations

import queue
from dataclasses import dataclass
from datetime import datetime


@dataclass
class LogLine:
    text: str
    level: str = "info"
    ts: str = ""


class EventBus:
    def __init__(self, maxlen: int = 1000):
        self._q: "queue.Queue[LogLine]" = queue.Queue()
        self.history: list[LogLine] = []
        self.maxlen = maxlen
        self.state: dict = {}

    def log(self, text: str, level: str = "info") -> None:
        line = LogLine(text=text, level=level, ts=datetime.now().strftime("%H:%M:%S"))
        self.history.append(line)
        if len(self.history) > self.maxlen:
            del self.history[: len(self.history) - self.maxlen]
        self._q.put(line)

    def drain(self) -> list[LogLine]:
        """Return and clear everything queued since the last call (UI thread)."""
        out: list[LogLine] = []
        try:
            while True:
                out.append(self._q.get_nowait())
        except queue.Empty:
            pass
        return out

    def set_state(self, key: str, value) -> None:
        self.state[key] = value
