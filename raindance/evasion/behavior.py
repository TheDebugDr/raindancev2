"""Human-behavior layer: curved cursor movement + variable-cadence typing.

Pure stdlib + Playwright objects passed in (SYNC API only). Nothing here is
allowed to raise into the checkout path — callers wrap these in try/except and
fall back to plain Playwright actions. Track cursor position ourselves because
Playwright does not expose it.
"""
from __future__ import annotations

import math
import random
import weakref
from typing import Tuple

# Module-level RNG. Intentionally unseeded so behavior varies per action.
_rng = random.Random()

# Per-page last-known cursor position, keyed weakly so pages can be GC'd.
_cursor_pos: "weakref.WeakKeyDictionary[object, Tuple[float, float]]" = (
    weakref.WeakKeyDictionary()
)


def _viewport_center(page) -> Tuple[float, float]:
    """Best-effort viewport center, with a sane fallback."""
    try:
        vp = page.viewport_size
        if vp:
            return float(vp["width"]) / 2.0, float(vp["height"]) / 2.0
    except Exception:
        pass
    return 640.0, 400.0


def _entry_point(page) -> Tuple[float, float]:
    """Plausible cursor position for a fresh page.

    Playwright's virtual mouse starts at (0, 0), but a real user's cursor
    usually drops into the viewport from the browser chrome (tabs / address
    bar). Starting the first path at a random point along the top edge keeps
    the event stream coherent instead of teleporting mid-viewport.
    """
    cx, _ = _viewport_center(page)
    width = cx * 2.0
    return (width * _rng.uniform(0.10, 0.90), _rng.uniform(2.0, 16.0))


def _get_pos(page) -> Tuple[float, float]:
    """Last-known cursor position; default to a jittered point near center."""
    pos = _cursor_pos.get(page)
    if pos is None:
        cx, cy = _viewport_center(page)
        pos = (
            cx + _rng.uniform(-60.0, 60.0),
            cy + _rng.uniform(-60.0, 60.0),
        )
        _cursor_pos[page] = pos
    return pos


def _set_pos(page, x: float, y: float) -> None:
    _cursor_pos[page] = (float(x), float(y))


def _steps_for_distance(dist: float) -> int:
    """Fitts's-law-ish step count: more steps for longer moves, capped."""
    n = int(12 + dist / 22.0)
    return max(15, min(40, n))


def _ease(t: float) -> float:
    """Smoothstep easing: slow at both ends, fast mid-flight.

    Constant-velocity cursor moves are a classic bot tell — humans accelerate
    out of the start and decelerate into the target (Fitts's law), so the
    curve parameter is remapped before the path is sampled.
    """
    return t * t * (3.0 - 2.0 * t)


def _bezier_point(
    p0: Tuple[float, float],
    p1: Tuple[float, float],
    p2: Tuple[float, float],
    p3: Tuple[float, float],
    t: float,
) -> Tuple[float, float]:
    """Cubic Bézier evaluation at parameter t in [0, 1]."""
    mt = 1.0 - t
    a = mt * mt * mt
    b = 3.0 * mt * mt * t
    c = 3.0 * mt * t * t
    d = t * t * t
    x = a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0]
    y = a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1]
    return x, y


def _control_points(
    start: Tuple[float, float], end: Tuple[float, float]
) -> Tuple[Tuple[float, float], Tuple[float, float]]:
    """Two control points offset perpendicular to the travel line for a curve."""
    sx, sy = start
    ex, ey = end
    dx, dy = ex - sx, ey - sy
    dist = math.hypot(dx, dy) or 1.0
    # Perpendicular unit vector.
    px, py = -dy / dist, dx / dist
    # Curve magnitude scales with distance but stays bounded.
    bow = min(dist * 0.25, 120.0)
    off1 = _rng.uniform(-bow, bow)
    off2 = _rng.uniform(-bow, bow)
    c1 = (sx + dx * 0.30 + px * off1, sy + dy * 0.30 + py * off1)
    c2 = (sx + dx * 0.65 + px * off2, sy + dy * 0.65 + py * off2)
    return c1, c2


def _settle(page, start: Tuple[float, float], end: Tuple[float, float]) -> None:
    """Short correction glide from an overshoot back onto the true target.

    A few small jittered steps — never a teleport — so the event stream keeps
    reading as fine motor correction.
    """
    for i in range(1, 4):
        t = i / 3.0
        x = start[0] + (end[0] - start[0]) * t + _rng.uniform(-1.5, 1.5)
        y = start[1] + (end[1] - start[1]) * t + _rng.uniform(-1.5, 1.5)
        page.mouse.move(x, y)
        page.wait_for_timeout(_rng.randint(8, 20))


def move(page, x: float, y: float) -> None:
    """Move the cursor to (x, y) along a curved Bézier path with jittered timing."""
    fresh = page not in _cursor_pos
    start = _entry_point(page) if fresh else _get_pos(page)
    end = (float(x), float(y))
    dist = math.hypot(end[0] - start[0], end[1] - start[1])

    if dist < 4.0:
        # Micro re-target: dwell in place instead of emitting a full path of
        # near-identical points, which reads as a bot holding still.
        pause(page, 25, 80)
        _set_pos(page, end[0], end[1])
        return

    steps = _steps_for_distance(dist)
    c1, c2 = _control_points(start, end)

    # Occasional overshoot-and-correct on longer moves.
    overshoot = dist > 220.0 and _rng.random() < 0.25
    if overshoot:
        ox = end[0] + (end[0] - start[0]) / dist * _rng.uniform(6.0, 18.0)
        oy = end[1] + (end[1] - start[1]) / dist * _rng.uniform(6.0, 18.0)
        aim = (ox, oy)
    else:
        aim = end

    for i in range(1, steps + 1):
        t = _ease(i / steps)
        bx, by = _bezier_point(start, c1, c2, aim, t)
        page.mouse.move(bx, by)
        page.wait_for_timeout(_rng.randint(3, 11))

    if overshoot:
        _settle(page, aim, end)

    _set_pos(page, end[0], end[1])


def _target_point(page, locator, *, timeout: int) -> Tuple[float, float]:
    """Resolve a locator to a clickable point with slight inward jitter."""
    locator.wait_for(state="visible", timeout=timeout)
    try:
        locator.scroll_into_view_if_needed(timeout=timeout)
    except Exception:
        pass
    box = locator.bounding_box()
    if not box:
        raise RuntimeError("no bounding box")
    w, h = box["width"], box["height"]
    # Aim near the middle but not dead-center; inset from the edges.
    jx = _rng.uniform(0.30, 0.70)
    jy = _rng.uniform(0.35, 0.65)
    x = box["x"] + w * jx
    y = box["y"] + h * jy
    return x, y


def _click_at(page, x: float, y: float) -> None:
    """Press with a variable hold time instead of an atomic click.

    Playwright's mouse.click() is down+up with zero gap; real presses dwell
    on the order of 50–150 ms. Splitting the press gives the hold duration
    somewhere to vary.
    """
    page.mouse.move(x, y)
    page.mouse.down()
    page.wait_for_timeout(_rng.randint(55, 145))
    page.mouse.up()


def click(page, locator, *, timeout: int = 15000) -> None:
    """Move along a curve to the element, hover, then click at a jittered point."""
    x, y = _target_point(page, locator, timeout=timeout)
    move(page, x, y)
    pause(page, 40, 130)  # hover dwell
    _click_at(page, x, y)
    _set_pos(page, x, y)
    pause(page, 60, 200)


def _key_delay(ch: str) -> int:
    """Per-character delay with a burst-pause rhythm and char-class bias.

    Mostly fast bursts, sometimes a measured stretch, occasionally a real
    hesitation. Digits are quickest; shifted/symbol keys take a beat longer.
    """
    roll = _rng.random()
    if roll < 0.10:
        base = _rng.randint(200, 420)
    elif roll < 0.30:
        base = _rng.randint(110, 190)
    else:
        base = _rng.randint(40, 120)
    if ch.isdigit():
        base = int(base * 0.8)
    elif ch and not ch.isalnum() and not ch.isspace():
        base = int(base * 1.35)
    return max(25, base)


def type_text(page, locator, text, *, clear: bool = True) -> None:
    """Focus the field (via a human click), optionally clear, then type per-key."""
    text = str(text)
    click(page, locator, timeout=8000)

    if clear:
        try:
            page.keyboard.press("Control+A")
            page.keyboard.press("Backspace")
        except Exception:
            try:
                locator.fill("")
            except Exception:
                pass

    for ch in text:
        page.keyboard.type(ch)
        page.wait_for_timeout(_key_delay(ch))


def human_select(page, locator, *, by_label=None, by_value=None, timeout: int = 15000) -> None:
    """Drive a <select> dropdown like a person: move in, open it, choose, move on.

    A bare select_option() changes the value with zero preceding mouse
    activity — a real user moves the cursor to the control and opens the menu
    first. Best-effort: any failure falls back to a plain select_option so a
    checkout never dies here.
    """
    if by_label is None and by_value is None:
        raise ValueError("human_select needs by_label or by_value")

    def _apply() -> None:
        if by_label is not None:
            locator.select_option(label=by_label, timeout=timeout)
        else:
            locator.select_option(value=by_value, timeout=timeout)

    try:
        x, y = _target_point(page, locator, timeout=timeout)
        move(page, x, y)
        pause(page, 60, 160)   # hover dwell on the control
        _click_at(page, x, y)  # open the dropdown
        pause(page, 120, 380)  # options "render" / the user scans them
        _apply()
        pause(page, 80, 220)
        # Drift to a neutral point — nobody hover-dwells on a select.
        cx, cy = _viewport_center(page)
        move(page, cx + _rng.uniform(-160.0, 160.0),
             cy + _rng.uniform(-110.0, 110.0))
    except Exception:
        try:
            _apply()
        except Exception:
            pass


def pause(page, lo_ms: int = 120, hi_ms: int = 480) -> None:
    """Jittered wait between actions."""
    page.wait_for_timeout(int(_rng.uniform(lo_ms, hi_ms)))
