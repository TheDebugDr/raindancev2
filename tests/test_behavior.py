"""Behavior-fidelity tests for raindance.evasion.behavior.

Mocked Playwright objects (page / mouse / keyboard / locator) — no browser
needed. These verify the event stream a detector would see: curved eased
paths, variable press durations, burst-pause typing, and the human_select
dropdown flow with its safe fallbacks.

Run: `.venv/bin/python tests/test_behavior.py`
"""
import inspect
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from raindance.evasion import behavior


# -- fakes --------------------------------------------------------------- #
class _Mouse:
    def __init__(self, page):
        self.page = page

    def move(self, x, y):
        self.page.events.append(("move", x, y))

    def down(self):
        self.page.events.append(("down",))

    def up(self):
        self.page.events.append(("up",))


class _Keyboard:
    def __init__(self, page):
        self.page = page

    def press(self, key):
        self.page.events.append(("press", key))

    def type(self, text):
        self.page.events.append(("type", text))


class _Page:
    def __init__(self, width=1280, height=900):
        self.viewport_size = {"width": width, "height": height}
        self.events = []
        self.mouse = _Mouse(self)
        self.keyboard = _Keyboard(self)

    def wait_for_timeout(self, ms):
        self.events.append(("wait", ms))


class _Locator:
    def __init__(self, page, box=None, select_fails=False, no_box=False):
        self.page = page
        self._box = box or {"x": 100.0, "y": 200.0, "width": 80.0, "height": 30.0}
        self._no_box = no_box
        self._select_fails = select_fails
        self.select_calls = []

    def wait_for(self, state=None, timeout=None):
        pass

    def scroll_into_view_if_needed(self, timeout=None):
        pass

    def bounding_box(self):
        return None if self._no_box else dict(self._box)

    def select_option(self, timeout=None, **kwargs):
        if self._select_fails:
            raise RuntimeError("boom")
        self.select_calls.append(kwargs)
        self.page.events.append(("select", kwargs))


def _seed(n):
    behavior._rng.seed(n)


def _moves(page):
    return [(x, y) for e in page.events if e[0] == "move" for x, y in [e[1:]]]


def _waits(page):
    return [e[1] for e in page.events if e[0] == "wait"]


# -- API compatibility --------------------------------------------------- #
def test_signatures_backward_compatible():
    sig = inspect.signature(behavior.click)
    assert list(sig.parameters) == ["page", "locator", "timeout"]
    assert sig.parameters["timeout"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["timeout"].default == 15000

    sig = inspect.signature(behavior.type_text)
    assert list(sig.parameters) == ["page", "locator", "text", "clear"]
    assert sig.parameters["clear"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["clear"].default is True

    sig = inspect.signature(behavior.pause)
    assert sig.parameters["lo_ms"].default == 120
    assert sig.parameters["hi_ms"].default == 480

    sig = inspect.signature(behavior.human_select)
    assert list(sig.parameters) == ["page", "locator", "by_label", "by_value", "timeout"]
    for name in ("by_label", "by_value", "timeout"):
        assert sig.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["by_label"].default is None
    assert sig.parameters["by_value"].default is None
    assert sig.parameters["timeout"].default == 15000


# -- movement ------------------------------------------------------------ #
def test_move_curved_and_eased():
    _seed(7)
    page = _Page()
    behavior.move(page, 900.0, 500.0)
    pts = _moves(page)
    assert 15 <= len(pts) <= 43, f"unexpected step count {len(pts)}"

    # Curved: path longer than the chord.
    straight = math.hypot(pts[-1][0] - pts[0][0], pts[-1][1] - pts[0][1])
    path = sum(math.hypot(b[0] - a[0], b[1] - a[1])
               for a, b in zip(pts, pts[1:]))
    assert path > straight * 1.01, "path is suspiciously straight"

    # Eased: second quarter of steps covers more ground than the first.
    dists = [math.hypot(b[0] - a[0], b[1] - a[1])
             for a, b in zip(pts, pts[1:])]
    q = len(dists) // 4
    q1 = sum(dists[:q]) / q
    q2 = sum(dists[q:2 * q]) / q
    assert q2 > q1, "no acceleration out of the start (constant velocity)"


def test_fresh_page_enters_from_top_edge():
    _seed(11)
    page = _Page()
    behavior.move(page, 900.0, 500.0)
    x0, y0 = _moves(page)[0]
    assert 0.0 <= y0 <= 120.0, f"first point {y0=} not near the top chrome"
    assert 0.0 <= x0 <= 1280.0


def test_micro_retarget_dwells_in_place():
    _seed(61)
    page = _Page()
    behavior.move(page, 900.0, 500.0)
    n_moves = len(_moves(page))
    behavior.move(page, 901.0, 501.0)  # ~1.4 px away
    assert len(_moves(page)) == n_moves, "micro move emitted a full path"
    assert 25 <= _waits(page)[-1] <= 80


def test_cursor_position_tracked():
    _seed(62)
    page = _Page()
    behavior.move(page, 900.0, 500.0)
    assert behavior._cursor_pos[page] == (900.0, 500.0)


# -- clicking ------------------------------------------------------------ #
def test_click_has_variable_press_duration():
    holds = []
    for seed in (21, 22, 23):
        _seed(seed)
        page = _Page()
        behavior.click(page, _Locator(page))
        ev = page.events
        di = next(i for i, e in enumerate(ev) if e[0] == "down")
        ui = next(i for i, e in enumerate(ev) if e[0] == "up")
        assert di < ui
        between = [e[1] for e in ev[di + 1:ui] if e[0] == "wait"]
        assert between, "zero hold between down and up"
        assert 55 <= between[0] <= 145, f"hold {between[0]}ms out of range"
        holds.append(between[0])
        # hover dwell before the press
        pre = [e[1] for e in ev[:di] if e[0] == "wait"]
        assert any(40 <= w <= 130 for w in pre), "no pre-click hover dwell"
    assert len(set(holds)) > 1, "press duration never varies"


# -- typing -------------------------------------------------------------- #
def test_type_text_burst_pause_cadence():
    _seed(31)
    page = _Page()
    behavior.type_text(page, _Locator(page), "Hello, 123!")
    typed = [e[1] for e in page.events if e[0] == "type"]
    assert typed == list("Hello, 123!")
    presses = [e[1] for e in page.events if e[0] == "press"]
    assert presses[:2] == ["Control+A", "Backspace"]
    assert page.events.index(("press", "Control+A")) < \
        page.events.index(("type", "H"))
    assert len(set(_waits(page))) > 3, "typing delays are metronomic"


def test_key_delay_char_bias():
    _seed(41)
    digits = [behavior._key_delay("5") for _ in range(60)]
    syms = [behavior._key_delay("!") for _ in range(60)]
    assert sum(digits) / 60 < sum(syms) / 60, "digits should type faster than symbols"
    assert all(d >= 25 for d in digits + syms)


def test_type_text_no_clear():
    _seed(32)
    page = _Page()
    behavior.type_text(page, _Locator(page), "AB", clear=False)
    assert not [e for e in page.events if e[0] == "press"]
    assert [e[1] for e in page.events if e[0] == "type"] == ["A", "B"]


# -- human_select -------------------------------------------------------- #
def test_human_select_happy_path():
    _seed(51)
    page = _Page()
    loc = _Locator(page)
    behavior.human_select(page, loc, by_label="2")
    assert loc.select_calls == [{"label": "2"}]
    ev = page.events
    first_move = next(i for i, e in enumerate(ev) if e[0] == "move")
    first_down = next(i for i, e in enumerate(ev) if e[0] == "down")
    sel = next(i for i, e in enumerate(ev) if e[0] == "select")
    assert first_move < first_down < sel, "no mouse activity before the select"
    # scan pause between opening the menu and choosing
    scan = [e[1] for e in ev[first_down:sel] if e[0] == "wait"]
    assert any(120 <= w <= 380 for w in scan), "no option-scan pause"
    # cursor drifted away afterwards — nobody hover-dwells on a select
    lx, ly = _moves(page)[-1]
    assert math.hypot(lx - 140.0, ly - 215.0) > 150.0


def test_human_select_by_value():
    _seed(52)
    page = _Page()
    loc = _Locator(page)
    behavior.human_select(page, loc, by_value="2")
    assert loc.select_calls == [{"value": "2"}]


def test_human_select_requires_an_option():
    page = _Page()
    try:
        behavior.human_select(page, _Locator(page))
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


def test_human_select_fallback_never_raises():
    _seed(53)
    # No bounding box -> fancy path fails, raw select_option still applies.
    page = _Page()
    loc = _Locator(page, no_box=True)
    behavior.human_select(page, loc, by_value="2")  # must not raise
    assert loc.select_calls == [{"value": "2"}]
    # Even total failure must not raise out of the helper.
    page2 = _Page()
    loc2 = _Locator(page2, no_box=True, select_fails=True)
    behavior.human_select(page2, loc2, by_value="2")  # must not raise


_TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    failed = 0
    for fn in _TESTS:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - report, don't hide
            failed += 1
            print(f"FAIL {fn.__name__}: {exc!r}")
        else:
            print(f"ok   {fn.__name__}")
    print(f"{len(_TESTS) - failed}/{len(_TESTS)} passed")
    raise SystemExit(1 if failed else 0)
