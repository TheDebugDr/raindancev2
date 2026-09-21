"""Site handler interface — navigation, ATC, queue, checkout per retailer.

Every handler (built-in retailer, or a store the user registered) is driven by
ONE override layer so behaviour can be tuned from config without code:

    site_params = {
      "selectors": {"atc": [...], "checkout": [...], "place_order": [...],
                    "queue": [...], "confirmation": [...]},
      "fields":    {"<css>": "<profile.path>"},   # "#email": "email"
      "fields_only": false,                       # true → ONLY `fields`
      "frames":    ["<frame-name-or-url-substring>"],
      "waits":     {"atc_enable_s": 30, "queue_max_s": 2700, "confirm_s": 20},
      "confirmation": {"order_id_regex": "...", "success_text": [...]}
    }

Every key is optional; an absent key inherits. Precedence, lowest to highest:

    GenericHandler.DEFAULT_SITE_PARAMS      (generic defaults)
    <Handler>.DEFAULT_SITE_PARAMS           (handler defaults, walked along
                                             the MRO so a subclass only states
                                             what it changes)
    task["site_params"]                     (per-store config from
                                             retailer_configs[<site>].site_params
                                             merged with task-row keys)

Dicts deep-merge; lists and scalars replace. A handler reads a value with
``self.effective(task, "selectors.atc")`` and NEVER consults a module constant
directly, so a per-store override always wins. `SiteHandler.default_site_params()`
exposes the merged class defaults for the UI (the Catalog page shows them as
placeholders) via ``retailer_config.default_site_params_for(site_id)``.

Element lookup has a single path — ``find_first`` → ``frames.find_first`` —
which searches the main document and every child frame (``frames`` hints only
bias the order), so any selector may live in an iframe on any handler.

Nothing here touches the evasion layer beyond the existing click/type helpers.
"""
from __future__ import annotations

import copy
import re
import time
from abc import ABC, abstractmethod
from typing import Any, Callable, Iterable, List, Optional, Tuple

# The contract keys every handler honours through the shared layer. A handler
# that reads extra keys (Pokémon Center: selectors.guest / selectors.continue)
# extends this list on its class so `handler_capabilities` can report it.
STANDARD_HONORS: Tuple[str, ...] = (
    "selectors.atc", "selectors.checkout", "selectors.place_order",
    "selectors.queue", "selectors.confirmation",
    "fields", "fields_only", "frames",
    "waits.atc_enable_s", "waits.queue_max_s", "waits.confirm_s",
    "confirmation.order_id_regex", "confirmation.success_text",
)

# Values the runner and the generic flow assume exist even for a bare subclass.
_ROOT_SITE_PARAMS: dict = {
    "selectors": {"atc": [], "checkout": [], "place_order": [], "queue": [],
                  "confirmation": []},
    "fields": {},
    "fields_only": False,
    "frames": [],
    "waits": {"atc_enable_s": 30, "queue_max_s": 2700, "confirm_s": 20},
    "confirmation": {
        "order_id_regex": r"order\s*(?:number|#|id|no\.?)[:\s#]*([A-Z0-9-]{5,})",
        "success_text": ["thank you", "order confirmed", "order number",
                         "confirmation", "thanks for your order"],
    },
}


def deep_merge(base: dict, over: dict) -> dict:
    """Return a new dict: `over` layered on `base`. Dicts merge, else replace."""
    out = copy.deepcopy(base) if base else {}
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def as_list(value) -> list:
    """Selector values may be one string or a list; blanks are dropped."""
    if value is None or value is False:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(x) for x in value if x is not None and str(x).strip()]
    return [str(value)]


def profile_value(profile: dict, path: str):
    """Dotted lookup: "payment.card_number" → profile["payment"]["card_number"]."""
    cur: Any = profile or {}
    for part in str(path or "").split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


class SiteHandler(ABC):
    """One handler per retailer. Methods mirror Phases 4–7."""

    name: str = "generic"

    #: Handler-level defaults in the site_params shape. Only state what this
    #: handler changes; the rest is inherited along the MRO (see module doc).
    DEFAULT_SITE_PARAMS: dict = {}

    #: Contract keys this handler reads. Extend on subclasses that add keys.
    HONORS: Tuple[str, ...] = STANDARD_HONORS

    def __init__(self, bus=None, captcha=None):
        self.bus = bus
        self.captcha = captcha
        self._task: Optional[dict] = None
        self._frame_hints: List[str] = []
        self._params_cache: Optional[dict] = None

    def log(self, msg: str, level: str = "info") -> None:
        if self.bus:
            self.bus.log(msg, level)

    # ------------------------------------------------------------------ #
    # override layer
    # ------------------------------------------------------------------ #
    @classmethod
    def default_site_params(cls) -> dict:
        """Handler defaults: root ← every DEFAULT_SITE_PARAMS along the MRO."""
        out = copy.deepcopy(_ROOT_SITE_PARAMS)
        for klass in reversed(cls.__mro__):
            own = klass.__dict__.get("DEFAULT_SITE_PARAMS")
            if isinstance(own, dict) and own:
                out = deep_merge(out, own)
        return out

    def bind_task(self, task: Optional[dict]) -> None:
        """Remember the task so lookups (frames, selectors) can read its
        site_params without every helper taking a `task` argument."""
        self._task = task or {}
        self._params_cache = None
        self._frame_hints = as_list(self.effective(self._task, "frames"))

    def effective_params(self, task: Optional[dict] = None) -> dict:
        """Full merged site_params: class defaults ← task["site_params"]."""
        task = task if task is not None else (self._task or {})
        if task is self._task and self._params_cache is not None:
            return self._params_cache
        over = task.get("site_params") if isinstance(task, dict) else None
        merged = deep_merge(self.default_site_params(),
                            over if isinstance(over, dict) else {})
        if task is self._task:
            self._params_cache = merged
        return merged

    def effective(self, task: Optional[dict], key: str, default=None):
        """Merged value for a dotted key, e.g. effective(task, "waits.confirm_s")."""
        cur: Any = self.effective_params(task)
        for part in key.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return default if cur is None else cur

    def selectors(self, task: Optional[dict], key: str) -> list:
        """Merged selector list for `key` (atc / checkout / place_order / ...)."""
        return as_list(self.effective(task, f"selectors.{key}", []))

    def wait_s(self, task: Optional[dict], key: str, default: float = 0.0) -> float:
        try:
            return float(self.effective(task, f"waits.{key}", default))
        except (TypeError, ValueError):
            return float(default)

    # ------------------------------------------------------------------ #
    # element lookup — the single frame-aware path
    # ------------------------------------------------------------------ #
    def find_first(self, page, selector: str):
        """Locate `selector` in the main document OR any child frame.

        Returns a Locator that may match nothing (callers keep their existing
        `.count()` guard). `frames` hints from the override layer bias which
        frame is searched first; with none, every frame is tried in order.
        """
        from raindance.sites import frames as _frames

        loc, where = _frames.find_first(page, selector, hints=self._frame_hints)
        if loc is not None:
            try:
                if where and page.frames and where != _frames.frame_label(page.frames[0]):
                    self.log(f"[{self.name}] found in {where}: {selector[:60]}", "info")
            except Exception:
                pass
            return loc
        # Nothing anywhere: a main-document locator with count 0 keeps every
        # caller's `.count()` guard behaving exactly as before. This is the only
        # direct page.locator call in the handler tree.
        return page.locator(selector).first

    def find_any(self, page, selectors: Iterable[str], *, visible: bool = True,
                 enabled: bool = False):
        """First selector that hits. Returns (locator, selector) or (None, "")."""
        for sel in selectors or ():
            try:
                loc = self.find_first(page, sel)
                if not loc.count():
                    continue
                if visible and not loc.is_visible():
                    continue
                if enabled and not loc.is_enabled():
                    continue
                return loc, sel
            except Exception:
                continue
        return None, ""

    def wait_for_first(self, page, selectors: Iterable[str], *, timeout_s: float,
                       should_stop: Optional[Callable[[], bool]] = None,
                       poll_ms: int = 500, enabled: bool = True):
        """Poll `find_any` until something is visible (and enabled) or timeout.

        Returns (locator, selector); (None, "") on timeout or stop. Always
        looks at least once, so timeout_s=0 is a single probe.
        """
        sels = list(selectors or ())
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        while True:
            if should_stop and should_stop():
                return None, ""
            loc, sel = self.find_any(page, sels, visible=True, enabled=enabled)
            if loc is not None:
                return loc, sel
            if time.monotonic() >= deadline:
                return None, ""
            try:
                page.wait_for_timeout(poll_ms)
            except Exception:
                pass

    def any_visible(self, page, selectors: Iterable[str]) -> bool:
        loc, _ = self.find_any(page, selectors, visible=True)
        return loc is not None

    def _resolve(self, page, target: Any):
        """Coerce a CSS selector str or an existing Locator into a Locator."""
        if isinstance(target, str):
            return self.find_first(page, target)
        return target

    # ------------------------------------------------------------------ #
    # shared building blocks used by the concrete flows
    # ------------------------------------------------------------------ #
    def wait_out_queue(self, page, task: dict, *, should_stop=None, on_status=None,
                       poll_ms: int = 2500, denied_selectors=("text=Access denied",)
                       ) -> bool:
        """Poll until no `selectors.queue` element is visible, or queue_max_s.

        This is the existing patient poll (no refresh storms, no pacing
        tricks): look, wait, look again. Empty queue selectors → True at once.
        """
        tid = task.get("id", "")
        queue_sels = self.selectors(task, "queue")
        if not queue_sels:
            return True
        in_queue = self.any_visible(page, queue_sels)
        if in_queue:
            if on_status:
                on_status("queued")
            self.log(f"[{tid}] virtual queue detected — waiting", "warn")
        deadline = time.monotonic() + self.wait_s(task, "queue_max_s", 2700)
        while True:
            if should_stop and should_stop():
                return False
            if self.captcha:
                if not self.captcha.handle_if_present(
                    page, task_id=tid, should_stop=should_stop,
                    on_waiting=lambda: on_status and on_status("captcha"),
                ):
                    return False
            if not self.any_visible(page, queue_sels):
                if in_queue:
                    self.log(f"[{tid}] left queue — page ready", "ok")
                return True
            if time.monotonic() >= deadline:
                break
            try:
                page.wait_for_timeout(poll_ms)
            except Exception:
                pass
            try:
                if self.any_visible(page, denied_selectors):
                    self.log(f"[{tid}] access denied in queue", "err")
                    return False
            except Exception:
                pass
        self.log(f"[{tid}] queue wait timed out", "err")
        return False

    def confirm_order(self, page, task: dict, *, poll_ms: int = 500) -> dict:
        """Read the confirmation page through the override layer.

        Polls `page.content()` for up to waits.confirm_s looking for a
        `selectors.confirmation` element, the `confirmation.order_id_regex`
        capture, or any `confirmation.success_text`. Returns the checkout
        result dict; a submitted order whose confirmation was never seen is
        still ok=True but carries a note — never a fabricated order id.
        """
        tid = task.get("id", "")
        regex = str(self.effective(task, "confirmation.order_id_regex", "") or "")
        texts = [t.lower() for t in as_list(self.effective(task, "confirmation.success_text", []))]
        conf_sels = self.selectors(task, "confirmation")
        rx = None
        if regex:
            try:
                rx = re.compile(regex, re.I)
            except re.error as exc:
                self.log(f"[{tid}] bad confirmation.order_id_regex ignored: {exc}", "warn")
        deadline = time.monotonic() + self.wait_s(task, "confirm_s", 20)
        while True:
            html = ""
            try:
                html = page.content() or ""
            except Exception:
                pass
            low = html.lower()
            order_id = ""
            if rx is not None:
                m = rx.search(html)
                if m:
                    order_id = (m.group(1) if m.groups() else m.group(0)).strip()
            seen = bool(order_id) or any(t in low for t in texts if t)
            if not seen and conf_sels:
                seen = self.any_visible(page, conf_sels)
            if seen:
                return {"ok": True, "order_id": order_id or "unknown", "error": ""}
            if time.monotonic() >= deadline:
                break
            try:
                page.wait_for_timeout(poll_ms)
            except Exception:
                pass
        return {"ok": True, "order_id": "", "error": "",
                "note": "submitted (confirmation not detected)"}

    def human_pause(self, page, lo_ms: int = 120, hi_ms: int = 480) -> None:
        from raindance.evasion import behavior
        behavior.pause(page, lo_ms, hi_ms)

    def human_click(self, page, target: Any, *, timeout: int = 15000) -> None:
        """Click with curved cursor movement; falls back to a plain click."""
        from raindance.evasion import behavior
        loc = self._resolve(page, target)
        try:
            behavior.click(page, loc, timeout=timeout)
        except Exception:
            loc.click(timeout=timeout)

    def human_type(
        self, page, target: Any, value, *, clear: bool = True, timeout: int = 8000
    ) -> bool:
        """Focus, optionally clear, then type ``value`` with human cadence.

        Returns False if ``value`` is empty/None or the element is missing.
        Best-effort: on any failure, falls back to a plain fill.
        """
        if not value:
            return False
        from raindance.evasion import behavior
        loc = self._resolve(page, target)
        try:
            if loc.count() == 0:
                return False
        except Exception:
            pass
        try:
            behavior.type_text(page, loc, value, clear=clear)
            return True
        except Exception:
            try:
                loc.fill(str(value), timeout=timeout)
                return True
            except Exception:
                return False

    def fill_if(self, page, target: Any, value, *, timeout: int = 8000) -> bool:
        """Fill a field if ``value`` is truthy; routes through human_type."""
        if not value:
            return False
        return self.human_type(page, target, value, clear=True, timeout=timeout)

    def human_select(
        self, page, target: Any, *, by_label=None, by_value=None,
        timeout: int = 15000
    ) -> bool:
        """Choose an option in a ``<select>`` like a person would.

        Moves the cursor onto the control along the curved behavior path,
        clicks to focus it, pauses, then sets the option (label preferred,
        value as fallback). Afterwards the cursor drifts a short distance
        off the control so it does not sit parked on the dropdown.

        Best-effort: any failure of the mouse path falls back to a plain
        ``select_option`` — the selection itself is never skipped for
        stealth. Returns True when the option was set, False when it
        could not be set at all.
        """
        from raindance.evasion import behavior
        loc = self._resolve(page, target)

        def _apply() -> bool:
            try:
                if by_label is not None:
                    try:
                        loc.select_option(label=str(by_label))
                    except Exception:
                        loc.select_option(str(by_label))
                else:
                    loc.select_option(str(by_value))
                return True
            except Exception:
                return False

        try:
            behavior.click(page, loc, timeout=timeout)
            behavior.pause(page, 80, 220)
        except Exception:
            pass  # mouse path failed; the selection itself still matters
        if not _apply():
            return False
        # Cosmetic drift: park the cursor off the control, not on it.
        try:
            import random as _random

            box = loc.bounding_box()
            if box:
                behavior.move(
                    page,
                    box["x"] + box["width"] / 2.0 + _random.uniform(40.0, 120.0),
                    box["y"] + box["height"] / 2.0 + _random.uniform(20.0, 70.0),
                )
        except Exception:
            pass
        return True

    # ------------------------------------------------------------------ #
    # phases (signatures are the runner contract — do not change)
    # ------------------------------------------------------------------ #
    @abstractmethod
    def navigate(self, page, task: dict) -> None:
        """Phase 4: open product / drop page."""

    @abstractmethod
    def handle_queue(
        self,
        page,
        task: dict,
        *,
        should_stop: Optional[Callable[[], bool]] = None,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> bool:
        """Phase 4: wait through virtual queue if present. True = ready to ATC."""

    @abstractmethod
    def add_to_cart(
        self,
        page,
        task: dict,
        *,
        should_stop: Optional[Callable[[], bool]] = None,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> bool:
        """Phase 5: ATC + CAPTCHA handling."""

    @abstractmethod
    def checkout(
        self,
        page,
        task: dict,
        profile: dict,
        *,
        dry_run: bool = True,
        should_stop: Optional[Callable[[], bool]] = None,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> dict:
        """Phase 6–7: fill + submit. Returns {ok, order_id, error, screenshot?}."""

    def clear_cart(self, page) -> int:
        """Empty this profile's cart. Returns how many items were removed.

        The cart lives in a persistent browser profile, so it survives runs.
        A handler that cannot do this must raise NotImplementedError so the
        runner can warn rather than silently assume an empty cart — a live
        place-order buys the cart's whole contents, not just this task's item.
        """
        raise NotImplementedError(
            f"{type(self).__name__} cannot clear the cart")

    def read_cart_total(self, page) -> Optional[float]:
        """Cart total as displayed, or None when it cannot be read."""
        return None

    def detect_stock_signals(self, html: str) -> Optional[bool]:
        """Optional site-specific stock parse from HTTP body. None = unknown."""
        return None

    def detect_seller(self, html: str, url: str) -> dict:
        """Who is selling this item, read from the page body.

        Returns {"is_official": bool|None, "seller": str|None}. `None` means
        "could not tell" — the classifier fails safe on that, so a handler must
        never return is_official=True unless it has positive evidence.
        """
        return {"is_official": None, "seller": None}
