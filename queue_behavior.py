"""Human-paced waiting-room behavior (Queue-it / Pokémon Center).

Queue-it's late-2025 detection update scores HOW you wait: check
frequency under ~20s, zero mouse-movement entropy, and a missing
referrer chain are bot tells. Humans check the queue every 30–90s [2].
Pokémon Center's own guidance (and its DataDome/Cloudflare layer) also
flags reloads and multi-tabbing — so this module never reloads and never
opens tabs [1].
"""
from __future__ import annotations

import random
import time
from typing import Optional, Tuple


def prewarm(page, homepage_url: str, target_url: str,
            dwell: Tuple[float, float] = (3.0, 6.0)) -> None:
    """Browse the homepage briefly before hitting the event/drop URL [2]."""
    page.goto(homepage_url, wait_until="domcontentloaded")
    time.sleep(random.uniform(*dwell))
    page.mouse.move(random.randint(120, 900), random.randint(90, 600),
                    steps=random.randint(6, 18))
    page.goto(target_url, wait_until="domcontentloaded")


class QueuePacer:
    """Wait in a virtual queue like a human: check 30–90s, not every 5s."""

    def __init__(self, min_check: int = 30, max_check: int = 90,
                 move_prob: float = 0.3, max_wait_s: float = 3600.0):
        self.min_check, self.max_check = min_check, max_check
        self.move_prob = move_prob
        self.max_wait_s = max_wait_s

    def wait(self, page, url_marker: str = "queue-it",
             on_tick=None) -> bool:
        """Block (human-paced) until the page URL leaves the waiting room.
        Never reloads, never opens tabs."""
        deadline = time.monotonic() + self.max_wait_s
        while url_marker in (page.url or "").lower():
            if time.monotonic() > deadline:
                return False
            if random.random() < self.move_prob:
                page.mouse.move(
                    random.randint(100, 1700), random.randint(100, 900),
                    steps=random.randint(8, 20))
                page.mouse.wheel(0, random.randint(40, 240))
            if on_tick is not None:
                try:
                    on_tick(page)
                except Exception:
                    pass
            time.sleep(random.uniform(self.min_check, self.max_check))
        return True
