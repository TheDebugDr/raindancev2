"""Frame-aware element lookup.

A checkout field is often not in the main document: card number, expiry and CVC
usually live inside one or more <iframe>s, and a locator built on `page` cannot
see them. This module searches the main frame and every child frame, so a
handler works whether the storefront inlines its fields or nests them.

Deliberately open-ended — nothing here assumes a particular provider or a
finished storefront:

  * With no configuration it simply tries every frame on the page.
  * `hints` bias the order so known frames are searched first (a speed and
    disambiguation aid, never a requirement).
  * Playwright exposes cross-origin iframes in `page.frames`, so a payment
    iframe is reachable without any special handling.

Nothing in here touches the evasion layer.
"""
from __future__ import annotations

from typing import Any, Iterable, List, Optional, Sequence, Tuple


def frame_label(frame) -> str:
    """Short human label for logs: the frame's name, else a trimmed URL."""
    try:
        name = (frame.name or "").strip()
        if name:
            return f"frame[{name}]"
        url = (frame.url or "").strip()
        if not url or url == "about:blank":
            return "frame[anonymous]"
        return f"frame[{url[:60]}]"
    except Exception:
        return "frame[?]"


def _matches_hint(frame, hint: str) -> bool:
    """A hint matches on frame name or any substring of its URL."""
    hint = (hint or "").strip().lower()
    if not hint:
        return False
    try:
        if hint in (frame.name or "").lower():
            return True
        if hint in (frame.url or "").lower():
            return True
    except Exception:
        pass
    return False


def ordered_frames(page, hints: Optional[Sequence[str]] = None) -> List[Any]:
    """Every frame on the page, main document first.

    `page.frames` already includes nested and cross-origin frames, so no manual
    recursion is needed. When `hints` are given, frames matching a hint are
    moved to the front (after the main frame) so the common case is found on
    the first try.
    """
    try:
        frames = list(page.frames)
    except Exception:
        return [page]
    if not frames:
        return [page]

    main, rest = frames[0], frames[1:]
    if hints:
        hinted = [f for f in rest if any(_matches_hint(f, h) for h in hints)]
        others = [f for f in rest if f not in hinted]
        rest = hinted + others
    return [main] + rest


def find_first(
    page,
    selector: str,
    *,
    hints: Optional[Sequence[str]] = None,
    require_visible: bool = False,
) -> Tuple[Optional[Any], str]:
    """First matching locator across all frames.

    Returns `(locator, where)`. `locator` is None when nothing matched anywhere;
    `where` is a label for logging. `require_visible` additionally demands the
    element be visible, which matters on checkouts that keep a hidden duplicate
    of a field in the main document.
    """
    if not selector:
        return None, ""
    for frame in ordered_frames(page, hints):
        try:
            loc = frame.locator(selector).first
            if not loc.count():
                continue
            if require_visible and not loc.is_visible():
                continue
            return loc, frame_label(frame)
        except Exception:
            # A frame can detach mid-search; that is normal on a live checkout.
            continue
    return None, ""


def find_any(
    page,
    selectors: Iterable[str],
    *,
    hints: Optional[Sequence[str]] = None,
    require_visible: bool = False,
) -> Tuple[Optional[Any], str, str]:
    """First match for the first selector that hits. Returns (loc, where, selector)."""
    for sel in selectors or ():
        loc, where = find_first(page, sel, hints=hints, require_visible=require_visible)
        if loc is not None:
            return loc, where, sel
    return None, "", ""


def describe(page) -> str:
    """One-line summary of the frame tree — useful when a field can't be found."""
    try:
        return " | ".join(frame_label(f) for f in page.frames) or "no frames"
    except Exception:
        return "frame tree unavailable"
