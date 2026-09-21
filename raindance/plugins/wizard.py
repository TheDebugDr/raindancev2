"""Start — the linear four-step wizard that opens the app.

    1  Which products will you be buying?   set x line, multi-select
    2  Where are you buying them?           one of your own retailers
    3  Review & execute                     per-item run config, run settings, arm
    4  Running                              live board, results ledger, stop

The wizard invents no state of its own. A selection is a `(set_id, line_id)`
pair; step 2 turns each pair into the listing that already exists at the chosen
store (`CatalogStore.listings`), and step 3 hands those product ids to
`ExecuteQueue`, which is what materialises tasks. So the same rows the Execute
tab edits are the rows this page edits — one queue, one source of truth.

Everything reads through `ctx`. Any service may be absent, so every accessor is
guarded and a missing one degrades to a message instead of an exception.
"""
from __future__ import annotations

from datetime import datetime

from nicegui import run, ui

from raindance.core.registry import ToolPlugin, register_tool
from raindance.ui import theme
from raindance.tasks.models import MAX_QUANTITY as _MAX_QTY

# run_plan is core, but the wizard must never be the reason a page fails to
# import — a missing pre-flight check becomes a blocker, not a traceback.
try:
    from raindance.core.run_plan import live_blockers
except Exception:  # pragma: no cover - defensive
    def live_blockers(profile_store):  # type: ignore[misc]
        return ["The live-run pre-flight check could not be loaded."]

# Link discovery reads a storefront's PUBLIC product feed. It is optional the
# same way run_plan is: a build without it keeps the manual URL fields and
# simply never offers to go looking.
try:
    from raindance.core import link_discovery
except Exception:  # pragma: no cover - defensive
    link_discovery = None  # type: ignore[assignment]

try:
    from raindance.core.link_index import REJECTED
except Exception:  # pragma: no cover - defensive
    REJECTED = "rejected"

# The schedule field writes the same key the orchestrator reads on start(), so
# it parses with the orchestrator's own function. The fallback is a verbatim
# copy: a build whose orchestrator cannot import still validates identically.
try:
    from raindance.tasks.orchestrator import _parse_start_at as parse_start_at
except Exception:  # pragma: no cover - defensive
    def parse_start_at(value):  # type: ignore[misc]
        value = (value or "").strip()
        if not value:
            return None
        try:
            if "-" in value or "T" in value:
                return datetime.fromisoformat(value.replace("T", " "))
            parts = [int(x) for x in value.split(":")]
            parts += [0] * (3 - len(parts))
            h, mi, sec = parts[:3]
            return datetime.now().replace(hour=h, minute=mi, second=sec, microsecond=0)
        except (ValueError, IndexError):
            return None

try:
    from raindance.tasks import models as _M
    STATUS_SUCCESS, STATUS_FAILED, STATUS_STOPPED = (
        _M.STATUS_SUCCESS, _M.STATUS_FAILED, _M.STATUS_STOPPED)
except Exception:  # pragma: no cover - defensive
    STATUS_SUCCESS, STATUS_FAILED, STATUS_STOPPED = "success", "failed", "stopped"

ORIGIN = "execute_queue"
_CAPTCHA_MODES = {"manual": "Manual", "api": "API"}
_EVASION_TIP = ("Evasion for this row (master switch in Settings → Evasion must be "
                "on). Master off = evasion off for every row; this toggle can only "
                "turn evasion OFF for this row.")
_FREQS = ["5s", "10s", "15s", "30s"]
_STEPS = ((1, "Products"), (2, "Retailer"), (3, "Execute"), (4, "Running"))
# `idle` is technically terminal, but an untouched task is not a result.
_FINISHED = (STATUS_SUCCESS, STATUS_FAILED, STATUS_STOPPED)
_BORDER = {STATUS_SUCCESS: "var(--rd-ok)", STATUS_FAILED: "var(--rd-live)",
           # a deliberate refusal reads amber, not the red of something breaking
           "denied": "var(--rd-accent)", STATUS_STOPPED: "var(--rd-idle)"}
# What the schedule key is written as. `parse_start_at` takes anything ISO-ish;
# this is the one shape it always accepts and the orchestrator log echoes.
_AT_FMT = "%Y-%m-%d %H:%M:%S"

# Wizard state lives at module level so it survives navigation: `render()` runs
# again every time the Start tab is opened, and a local dict would send the
# user back to step 1 after a trip to Settings. `products` is a set of
# (set_id, line_id) pairs; `cand` is the proposal page per pair. First open
# starts exactly where a fresh dict would.
_STATE: dict = {"step": 1, "products": set(), "store": None, "loaded": False,
                "cand": {}}


def reset() -> None:
    """Back to a first-open wizard. Used by New run and available to callers."""
    _STATE.update(step=1, store=None, loaded=False)
    _STATE["products"].clear()
    _STATE["cand"].clear()
# Feed reader -> how to name it when saying where a proposal came from.
_FEEDS = {"read_shopify": "the Shopify product feed",
          "read_sitemap": "sitemap.xml"}
# At or above this a proposal reads as a strong match; below it stays amber.
_STRONG = 0.8

_CSS = """
<style>
/* The Monitors tab injects its own `.rd-tile` (a square image well) into the
   same page head. Head HTML accumulates across tab switches, so whichever tab
   was opened last would win the shared class. These rules carry two classes,
   which outranks both single-class definitions regardless of injection order,
   so a wizard tile always looks like a wizard tile. */
.rd-tile.rdw-tile { aspect-ratio:auto; display:flex; flex-direction:column;
                    place-items:stretch; overflow:visible; gap:6px;
                    padding:12px; text-align:left; }

.rdw-head { display:flex; align-items:center; justify-content:space-between;
            gap:12px; flex-wrap:wrap; padding:12px 15px;
            border-bottom:1px solid var(--rd-line); }
.rdw-pad  { padding:15px; display:flex; flex-direction:column; gap:10px; }
.rdw-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(158px,1fr));
            gap:12px; padding:14px 15px; }
.rdw-sgrid{ display:grid; grid-template-columns:repeat(auto-fill,minmax(186px,1fr));
            gap:14px; padding:15px; }

.rdw-code { font-family:var(--rd-mono); font-weight:700; font-size:21px;
            letter-spacing:.09em; color:var(--rd-ink); line-height:1.1; }
.rdw-name { font-size:13px; color:var(--rd-ink-dim); line-height:1.3; }
.rdw-meta { font-family:var(--rd-mono); font-size:10.5px; letter-spacing:.06em;
            color:var(--rd-ink-faint); font-variant-numeric:tabular-nums; }
.rdw-meta.rdw-has { color:var(--rd-ok); }
.rdw-meta.rdw-none { color:var(--rd-live); }

/* retailer tile: a square initials well over the name, like the set grid.
   Routed through theme.thumb so a store logo would land here the day the
   catalog has one; today there is no source for one, so it stays initials. */
.rdw-sq { aspect-ratio:1/1; width:100%; border-radius:4px; display:grid;
          place-items:center; background:var(--rd-surface-2);
          border:1px solid var(--rd-line-soft); }

/* Product art and set art both sit in a theme.thumb well, and the well only
   TAKES UP ROOM when there is actually an image in it (`.rd-thumb-has`).
   Without one it collapses to the bare code badge these tiles have always
   drawn, so an image-less catalog — the offline case — renders at exactly the
   dimensions it does today and the grids never shift. */
/* `center start` keeps the bare code badge left-aligned, exactly where the
   tile's text-align:left has always put it; with an image the well centres. */
.rdw-pic { width:100%; place-items:center start; }
.rdw-pic.rd-thumb-has { place-items:center;
                        height:70px; border-radius:4px; margin-bottom:2px;
                        background:var(--rd-surface-2);
                        border:1px solid var(--rd-line-soft);
                        --rd-thumb-bg:var(--rd-surface-2); }
.rdw-hpic.rd-thumb-has { width:38px; height:38px; border-radius:4px;
                         background:var(--rd-surface-2);
                         border:1px solid var(--rd-line-soft);
                         --rd-thumb-bg:var(--rd-surface-2); }
.rdw-init { font-family:var(--rd-mono); font-weight:700; font-size:30px;
            letter-spacing:.1em; color:var(--rd-ink-dim); }
.rd-tile-on .rdw-init { color:var(--rd-accent); }
.rdw-cov { display:flex; align-items:center; gap:6px; flex-wrap:wrap; }

.rdw-bar { position:sticky; bottom:0; z-index:5; display:flex; align-items:center;
           justify-content:space-between; gap:12px; flex-wrap:wrap;
           padding:11px 14px; background:var(--rd-surface);
           border:1px solid var(--rd-line); border-radius:5px; }

.rdw-gap { display:flex; align-items:center; gap:10px; flex-wrap:wrap;
           padding:11px 15px; border-bottom:1px solid var(--rd-line-soft); }
.rdw-gap:last-child { border-bottom:0; }

.rdw-bulk { display:flex; flex-wrap:wrap; align-items:flex-end; gap:11px;
            padding:12px 15px; border-bottom:1px solid var(--rd-line);
            background:color-mix(in srgb,var(--rd-surface-2) 42%,transparent); }
.rdw-field { display:flex; flex-direction:column; gap:2px; }

.rdw-row { display:flex; flex-direction:column; gap:8px; padding:12px 15px;
           border-bottom:1px solid var(--rd-line-soft); }
.rdw-row:last-child { border-bottom:0; }
.rdw-off { opacity:.45; }
.rdw-top { display:flex; align-items:center; gap:12px; }
.rdw-ctrls { display:flex; flex-wrap:wrap; align-items:flex-end; gap:10px; }
.rdw-url { font-family:var(--rd-mono); font-size:11px; color:var(--rd-ink-faint);
           overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:52ch; }

.rdw-arm { display:flex; flex-direction:column; align-items:center; gap:12px;
           padding:22px 16px 18px; }
/* Stop wears the live colour. The label takes --rd-surface rather than a
   fixed white: --rd-live is a dark red on the light theme and a light red on
   the dark one, and --rd-surface inverts with it, so the word stays legible on
   both instead of going white-on-light-red after dark. */
.rd-disc.rdw-stop {
  background:radial-gradient(circle at 50% 42%,var(--rd-live),var(--rd-live) 70%) !important;
  color:var(--rd-surface) !important;
  box-shadow:0 0 0 1px color-mix(in srgb,var(--rd-live) 40%,transparent),
             0 18px 50px -20px var(--rd-live); }
.rd-disc.rdw-stop .q-btn__content { color:var(--rd-surface); }
.rdw-disclab { font-family:var(--rd-mono); font-weight:700; letter-spacing:.18em;
               text-transform:uppercase; font-size:14px; line-height:1.5;
               text-align:center; white-space:normal; }
.rdw-state { font-family:var(--rd-mono); font-size:12px; color:var(--rd-ink-dim);
             display:flex; align-items:center; justify-content:center; gap:8px;
             min-height:18px; }
.rdw-dot { width:7px; height:7px; border-radius:50%; background:var(--rd-ok);
           animation:rdw-pulse 1.4s ease-in-out infinite; }
@keyframes rdw-pulse { 0%,100%{opacity:.35} 50%{opacity:1} }
@media (prefers-reduced-motion: reduce){ .rdw-dot{ animation:none } }

.rdw-board { display:flex; align-items:center; gap:12px; padding:11px 15px;
             border-bottom:1px solid var(--rd-line-soft); }
.rdw-board:last-child { border-bottom:0; }
.rd-chip.rdw-plat { color:var(--rd-ink-dim);
                    background:color-mix(in srgb,var(--rd-ink-dim) 15%,transparent); }
.rd-chip.rdw-dis  { color:var(--rd-ink-faint);
                    background:color-mix(in srgb,var(--rd-ink-faint) 13%,transparent); }

/* A cached proposal is neither a link nor a gap, so it wears neither the ok
   green nor the live red — the watch blue says "known, not yet acted on". */
.rdw-meta.rdw-found { color:var(--rd-watch); }

/* proposals: fuzzy matches waiting on a human yes or no */
.rdw-prop { display:flex; flex-direction:column; gap:8px; padding:12px 15px;
            border-bottom:1px solid var(--rd-line-soft); }
.rdw-prop:last-child { border-bottom:0; }
.rdw-cand { display:flex; align-items:center; gap:9px; flex-wrap:wrap; }
.rdw-alt  { font-family:var(--rd-mono); font-size:10.5px; letter-spacing:.06em;
            color:var(--rd-ink-faint); font-variant-numeric:tabular-nums; }
/* the candidate URL is a real link — worth opening before you accept a match */
a.rdw-url { color:var(--rd-ink-faint); text-decoration:underline;
            text-underline-offset:2px; }
a.rdw-url:hover { color:var(--rd-ink-dim); }

/* run settings: one strip of fields, same rhythm as the bulk bar above it */
.rdw-run { display:flex; flex-wrap:wrap; align-items:flex-end; gap:14px;
           padding:12px 15px; }
.rdw-count { font-family:var(--rd-mono); font-size:12px; color:var(--rd-ink-dim);
             font-variant-numeric:tabular-nums; min-height:18px; }
.rdw-count.rdw-soon { color:var(--rd-accent); }
/* a pre-set start time parks EXECUTE in "scheduled" — say so before the disc */
.rdw-stale { display:flex; align-items:center; gap:12px; flex-wrap:wrap;
             padding:10px 15px; border-bottom:1px solid var(--rd-line);
             background:color-mix(in srgb,var(--rd-accent) 10%,transparent);
             font-size:13px; color:var(--rd-ink); }
.rdw-stale b { font-family:var(--rd-mono); }

/* step 4: summary strip, ledger rows, action bar */
.rdw-sum { display:flex; align-items:center; gap:14px; flex-wrap:wrap;
           font-family:var(--rd-mono); font-size:12px; color:var(--rd-ink-dim);
           font-variant-numeric:tabular-nums; }
.rdw-sum b { color:var(--rd-ink); font-weight:700; }
.rdw-led { display:flex; align-items:center; gap:13px; padding:11px 15px 11px 14px;
           border-bottom:1px solid var(--rd-line-soft); border-left:2px solid transparent; }
.rdw-led:last-child { border-bottom:0; }
.rdw-shot { font-family:var(--rd-mono); font-size:10.5px; color:var(--rd-ink-faint);
            overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:40ch; }
.rdw-acts { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
</style>
"""


def _initials(name: str) -> str:
    """Up to two letters standing in for a retailer's logo."""
    words = [w for w in str(name or "").replace("-", " ").split() if w]
    if not words:
        return "??"
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][:1] + words[1][:1]).upper()


def _short(text: object, limit: int = 52) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _plural(n: int, word: str, many: str = "") -> str:
    return word if n == 1 else (many or word + "s")


@register_tool
class WizardTool(ToolPlugin):
    """The app's front door: pick products, pick a retailer, review, execute."""

    id = "wizard"
    name = "Start"
    icon = "auto_awesome"
    order = -1
    description = ("The three-step drop flow: which products, which retailer, "
                   "then review and arm the run.")

    def render(self, ctx) -> None:
        theme.inject()
        ui.add_head_html(_CSS)

        catalog = getattr(ctx, "catalog", None)
        queue = getattr(ctx, "queue", None)
        store = getattr(ctx, "store", None)
        # Optional: an older config has no link index, and no build is obliged
        # to ship link discovery. Every use below is guarded, so with both
        # absent the retailer step behaves exactly as it did without them.
        links = getattr(ctx, "links", None)
        orch = getattr(ctx, "orchestrator", None)
        settings = getattr(ctx, "settings", None)
        bus = getattr(ctx, "bus", None)
        task_store = getattr(ctx, "task_store", None)
        profile_store = getattr(ctx, "profile_store", None)

        # `products` holds (set_id, line_id) pairs; `loaded` guards the one-shot
        # hand-off into the queue when step 3 is entered.
        # `cand` remembers which proposal the user is looking at for each pair,
        # so paging through matches survives the refresh every action triggers.
        # The dict itself is module-level (`_STATE`), so leaving the page and
        # coming back lands on the same step with the same selection.
        state = _STATE
        # `sig` starts as None, not "": an empty board has an empty signature,
        # so "" would swallow the very first paint.
        cache = {"idx": {}, "links": {}, "sig": None, "lsig": None, "running": None,
                 # a running task carries a URL, not the three ids, so the task
                 # board resolves its thumbnail through this
                 "by_url": {}}
        refs: dict = {}
        guard = {"busy": False, "finding": False}

        # ------------------------------------------------------------------ #
        # guarded service access — a missing service degrades, never raises
        # ------------------------------------------------------------------ #
        def _log(text: str, level: str = "info") -> None:
            try:
                if bus is not None:
                    bus.log(text, level)
            except Exception:
                pass

        def _sets() -> list:
            try:
                return list(catalog.sets() or [])
            except Exception as e:
                _log(f"wizard: sets unavailable — {e}", "warn")
                return []

        def _lines() -> list:
            try:
                return list(catalog.lines() or [])
            except Exception as e:
                _log(f"wizard: lines unavailable — {e}", "warn")
                return []

        def _stores() -> list:
            try:
                return list(catalog.stores() or [])
            except Exception as e:
                _log(f"wizard: stores unavailable — {e}", "warn")
                return []

        def _name_of(kind: str, ident: str) -> str:
            if not ident:
                return ""
            try:
                return catalog.name_of(kind, ident) or str(ident)
            except Exception:
                return str(ident)

        def _build_index() -> dict:
            """{(set_id, line_id): {store_id: [product, ...]}} in one pass.

            Built from the same `catalog.listings()` the per-cell counts would
            call, just read once per render instead of once per tile.
            """
            idx: dict = {}
            by_url: dict = {}
            try:
                rows = catalog.listings(store)
            except Exception as e:
                _log(f"wizard: listings unavailable — {e}", "warn")
                cache["by_url"] = {}
                return {}
            for p in rows:
                key = (p.get("set_id") or "", p.get("line_id") or "")
                idx.setdefault(key, {}).setdefault(p.get("store_id") or "", []).append(p)
                url = str(p.get("url") or "")
                if url:
                    by_url.setdefault(url, p)
            cache["by_url"] = by_url
            return idx

        def _at(set_id: str, line_id: str, store_id: str = ""):
            """The listing for a pair, optionally at one store. None if absent."""
            per_store = cache["idx"].get((set_id, line_id)) or {}
            if store_id:
                found = per_store.get(store_id) or []
                return found[0] if found else None
            for rows in per_store.values():
                if rows:
                    return rows[0]
            return None

        def _retailers_for(set_id: str, line_id: str) -> int:
            return len([s for s, rows in (cache["idx"].get((set_id, line_id)) or {}).items()
                        if s and rows])

        def _score_of(entry: dict) -> float:
            try:
                return float(entry.get("score") or 0)
            except (TypeError, ValueError):
                return 0.0

        def _build_links() -> dict:
            """{(store_id, set_id, line_id): [entry, ...]} best-first, one pass.

            Same trick as `_build_index`: the retailer grid asks about every
            store x every selected pair, and LinkIndex.candidates() walks the
            whole index per question. Read it once per render instead.

            An absent index (older config) yields {}, which makes `_has_link`
            false everywhere — i.e. exactly the listings-only behaviour.
            """
            if links is None:
                return {}
            try:
                rows = list(links.items or [])
            except Exception as e:
                _log(f"wizard: link index unavailable — {e}", "warn")
                return {}
            out: dict = {}
            for e in rows:
                if e.get("status") == REJECTED:      # a "no" is never re-proposed
                    continue
                key = (e.get("store_id") or "", e.get("set_id") or "",
                       e.get("line_id") or "")
                out.setdefault(key, []).append(e)
            for group in out.values():
                group.sort(key=lambda r: -_score_of(r))
            return out

        def _candidates(store_id: str, set_id: str, line_id: str) -> list:
            """Proposals for one pair at one store, best score first."""
            return list(cache["links"].get((store_id, set_id, line_id)) or [])

        def _has_link(store_id: str, set_id: str, line_id: str) -> bool:
            return bool(cache["links"].get((store_id, set_id, line_id)))

        def _selected_pairs() -> list:
            """Selections in catalog order, so the UI never reshuffles."""
            order = {(s.get("id"), l.get("id")): (si, li)
                     for si, s in enumerate(_sets())
                     for li, l in enumerate(_lines())}
            return sorted(state["products"], key=lambda k: order.get(k, (999, 999)))

        def _covered(store_id: str) -> tuple:
            """(linked, found, missing) pairs for the current selection.

            `linked` already has a listing the runner can open. `found` has a
            cached proposal and nothing else — a fuzzy title match is not a link
            until a human says so, which is why it is its own bucket rather than
            being folded into either side. `missing` has neither.
            """
            have, found, missing = [], [], []
            for sid, lid in _selected_pairs():
                if _at(sid, lid, store_id) is not None:
                    have.append((sid, lid))
                elif _has_link(store_id, sid, lid):
                    found.append((sid, lid))
                else:
                    missing.append((sid, lid))
            return have, found, missing

        def _pair_label(sid: str, lid: str) -> str:
            return " · ".join([b for b in (_name_of("set", sid), _name_of("line", lid)) if b]) or "—"

        # ------------------------------------------------------------------ #
        # navigation
        # ------------------------------------------------------------------ #
        def _goto(step: int) -> None:
            try:
                step = max(1, min(len(_STEPS), int(step)))
            except (TypeError, ValueError):
                return
            if step == state["step"]:
                return
            if step >= 2 and not state["products"]:
                ui.notify("Pick at least one product first.", type="warning")
                return
            if step >= 3 and not state["store"]:
                ui.notify("Pick a retailer first.", type="warning")
                return
            if step < state["step"] and step < 3:
                # Going back means the selection may change, so the queue
                # hand-off has to happen again on the way forward. Running →
                # Execute is not that: the queue stays exactly as it ran.
                state["loaded"] = False
            state["step"] = step
            body.refresh()

        # ------------------------------------------------------------------ #
        # stepper
        # ------------------------------------------------------------------ #
        def _stepper() -> None:
            now = state["step"]
            with ui.element("div").classes("rd-steps"):
                for i, (n, label) in enumerate(_STEPS):
                    if i:
                        ui.element("div").classes("rd-step-sep")
                    cls = "rd-step"
                    if n == now:
                        cls += " rd-step-on"
                    elif n < now:
                        cls += " rd-step-done"
                    node = ui.element("div").classes(cls)
                    with node:
                        ui.label(str(n)).classes("rd-step-n")
                        ui.label(label)
                    if n < now:
                        node.on("click", lambda _=None, target=n: _goto(target))
                        node.tooltip("Go back to this step")

        # ------------------------------------------------------------------ #
        # step 1 — which products
        # ------------------------------------------------------------------ #
        def _paint_selection() -> None:
            n = len(state["products"])
            lbl = refs.get("count")
            if lbl is not None:
                lbl.set_text(f"{n} {_plural(n, 'product')} selected")
            btn = refs.get("next")
            if btn is not None:
                btn.set_enabled(n > 0)

        def _toggle_pair(key, tile) -> None:
            if key in state["products"]:
                state["products"].discard(key)
            else:
                state["products"].add(key)
            state["loaded"] = False          # selection changed → reload step 3
            on = key in state["products"]
            tile.classes(replace="rd-tile rdw-tile" + (" rd-tile-on" if on else ""))
            _paint_selection()

        def _product_tile(set_id: str, line: dict) -> None:
            line_id = str(line.get("id") or "")
            key = (set_id, line_id)
            on = key in state["products"]
            tile = ui.element("div").classes("rd-tile rdw-tile" + (" rd-tile-on" if on else ""))
            with tile:
                # The image where the catalog has one, the short code where it
                # does not — same box, same class, one code path.
                theme.thumb(theme.product_art(catalog, set_id, line_id),
                            str(line.get("short") or line.get("name") or "?"),
                            classes="rdw-pic", text_class="rdw-code")
                ui.label(str(line.get("name") or line_id)).classes("rdw-name")
                msrp = line.get("msrp")
                try:
                    price = f"MSRP ${float(msrp):.2f}" if msrp else "MSRP —"
                except (TypeError, ValueError):
                    price = "MSRP —"
                ui.label(price).classes("rdw-meta")
                n = _retailers_for(set_id, line_id)
                ui.label(f"{n} {_plural(n, 'retailer')} linked" if n else "No link yet") \
                    .classes("rdw-meta " + ("rdw-has" if n else "rdw-none"))
            tile.on("click", lambda _=None, k=key, t=tile: _toggle_pair(k, t))

        def _step_products() -> None:
            sets, lines = _sets(), _lines()
            ui.label("Which products will you be buying?").classes("text-2xl font-bold")
            ui.label("Pick every box you want from every set that is dropping. "
                     "You choose the retailer next.").classes("rd-lede")

            if not sets:
                theme.note("<b>No sets in the catalog.</b> Add the set that is dropping "
                           "in the <b>Monitors</b> tab, then come back here.")
                return
            if not lines:
                theme.note("<b>No product lines in the catalog.</b> Add at least one "
                           "line — Elite Trainer Box, Booster Bundle, and so on — in "
                           "the <b>Monitors</b> tab.")
                return

            for s in sets:
                set_id = str(s.get("id") or "")
                with theme.card():
                    with ui.element("div").classes("rdw-head"):
                        with ui.row().classes("items-center gap-3 min-w-0"):
                            # With a logo this is the set's logo; without one it
                            # is the code chip the header has always carried.
                            theme.thumb(theme.set_art(catalog, set_id),
                                        str(s.get("code") or "?").upper(),
                                        classes="rdw-hpic",
                                        text_class="rd-chip rdw-plat",
                                        tip=str(s.get("name") or set_id))
                            ui.label(str(s.get("name") or set_id)).classes("rd-title")
                        picked = sum(1 for (sid, _l) in state["products"] if sid == set_id)
                        if picked:
                            ui.label(f"{picked} picked").classes("rd-lab rd-num")
                    with ui.element("div").classes("rdw-grid"):
                        for line in lines:
                            _product_tile(set_id, line)

            with ui.element("div").classes("rdw-bar"):
                refs["count"] = ui.label("").classes("rd-lab rd-num")
                with ui.row().classes("items-center gap-2"):
                    ui.button("Clear", on_click=_clear_products) \
                        .props("flat dense no-caps size=sm")
                    refs["next"] = ui.button("Next · retailer",
                                             on_click=lambda: _goto(2)) \
                        .props("unelevated no-caps")
            _paint_selection()

        def _clear_products() -> None:
            if not state["products"]:
                return
            state["products"].clear()
            state["loaded"] = False
            body.refresh()

        # ------------------------------------------------------------------ #
        # step 2 — which retailer
        # ------------------------------------------------------------------ #
        def _pick_store(store_id: str) -> None:
            if state["store"] != store_id:
                state["cand"].clear()        # paging is per store
            state["store"] = store_id
            state["loaded"] = False
            _, found, missing = _covered(store_id)
            if missing or found:
                # Selected, but incomplete — stay here and say exactly what is
                # short, with the form to fix it right where the gap is named.
                # Proposals count as short: step 3 resolves listings, and a
                # proposal is not one until it is confirmed here.
                body.refresh()
                return
            state["step"] = 3
            body.refresh()

        def _store_tile(st: dict) -> None:
            store_id = str(st.get("id") or "")
            on = state["store"] == store_id
            enabled = bool(st.get("enabled", True))
            have, found, missing = _covered(store_id)
            tile = ui.element("div").classes("rd-tile rdw-tile" + (" rd-tile-on" if on else ""))
            with tile:
                # Stores have no logo source in the catalog, so this is the
                # initials well it has always been — via the shared helper, so
                # the day a store carries an image it needs no new code here.
                theme.thumb(str(st.get("image") or ""),
                            _initials(st.get("name") or store_id),
                            classes="rdw-sq", text_class="rdw-init")
                ui.label(str(st.get("name") or store_id)).classes("rdw-name") \
                    .style("color:var(--rd-ink);font-size:14px")
                with ui.element("div").classes("rdw-cov"):
                    ui.label(str(st.get("platform") or "custom")).classes("rd-chip rdw-plat")
                    if not enabled:
                        ui.label("disabled").classes("rd-chip rdw-dis")
                with ui.element("div").classes("rdw-cov"):
                    # Two sources kept visibly apart: a listing is something the
                    # runner can open now, a found link is a proposal awaiting a
                    # yes. Green only when every product is a real listing.
                    ui.label(f"{len(have)} linked").classes(
                        "rdw-meta " + ("rdw-has" if have and not found and not missing
                                       else ""))
                    if found:
                        ui.label(f"· {len(found)} found").classes("rdw-meta rdw-found") \
                            .tooltip("Cached proposals — confirm them to make them links")
                if missing:
                    ui.label(f"{len(missing)} missing {_plural(len(missing), 'link')}") \
                        .classes("rdw-meta rdw-none")
                base = str(st.get("base_url") or "")
                if base:
                    ui.label(_short(base, 26)).classes("rdw-meta").tooltip(base)
            tile.on("click", lambda _=None, sid=store_id: _pick_store(sid))

        # ------------------------------------------------------------------ #
        # discovery — ask the storefront's own public feed what it sells
        # ------------------------------------------------------------------ #
        def _paint_find(busy: bool) -> None:
            """Spinner on every Find button, so one crawl can't be started twice."""
            for btn in (refs.get("find_btns") or []):
                try:
                    btn.set_enabled(not busy)
                    if busy:
                        btn.props("loading")
                    else:
                        btn.props(remove="loading")
                except Exception:
                    pass

        def _find_button(store_id: str):
            """The button that goes looking. Inert when the build cannot."""
            async def _go() -> None:
                await _discover(store_id)

            btn = ui.button("Find product links", icon="travel_explore",
                            on_click=_go).props("outline dense no-caps size=sm")
            if links is None or link_discovery is None:
                btn.set_enabled(False)
                btn.tooltip("This build has no link discovery — paste URLs by hand.")
                return btn
            btn.tooltip("Reads this store's public product feed (products.json or "
                        "sitemap.xml) and proposes matching URLs")
            refs.setdefault("find_btns", []).append(btn)
            return btn

        def _wanted_pairs() -> list:
            """Selected pairs resolved to the real (set, line) dicts.

            Copies, not the catalog's own dicts: `propose` runs on a worker
            thread and has no business sharing objects with the UI thread.
            """
            out = []
            for sid, lid in _selected_pairs():
                try:
                    s, l = catalog.get_set(sid), catalog.get_line(lid)
                except Exception as e:
                    _log(f"wizard: catalog lookup failed — {e}", "warn")
                    continue
                if s and l:
                    out.append((dict(s), dict(l)))
            return out

        async def _discover(store_id: str) -> None:
            """Crawl once, cache the proposals, and say honestly what happened."""
            if links is None or link_discovery is None:
                ui.notify("This build has no link discovery — paste the URLs by hand.",
                          type="warning")
                return
            if guard["finding"]:
                return
            try:
                shop = catalog.get_store(store_id)
            except Exception as e:
                _log(f"wizard: store lookup failed — {e}", "warn")
                shop = None
            if not shop:
                ui.notify("That retailer is no longer in the catalog.", type="warning")
                return
            shop_name = _name_of("store", store_id)
            if not str(shop.get("base_url") or "").strip():
                ui.notify(f"{shop_name} has no base URL, so there is no feed to read. "
                          "Set one in the Monitors tab, or paste the URLs by hand.",
                          type="warning", multi_line=True)
                return
            wanted = _wanted_pairs()
            if not wanted:
                ui.notify("Nothing selected to look for.", type="warning")
                return

            guard["finding"] = True
            _paint_find(True)
            ui.notify(f"Reading {shop_name}'s public product feed…", type="info")
            try:
                # Network I/O, seconds of it — off the UI thread or the page hangs.
                result = await run.io_bound(link_discovery.propose, dict(shop), wanted)
            except Exception as e:
                guard["finding"] = False
                _paint_find(False)
                _log(f"wizard: link discovery failed at {shop_name} — {e}", "warn")
                ui.notify(f"Could not read {shop_name}: {e}", type="negative",
                          multi_line=True)
                return
            guard["finding"] = False
            _paint_find(False)

            result = result or {}
            error = str(result.get("error") or "")
            if error:
                # A store with no public feed is an ordinary outcome, not a
                # failure — name it and leave the manual fields where they were.
                _log(f"wizard: no feed at {shop_name} — {error}", "info")
                ui.notify(f"{shop_name}: {error}. Paste the URLs by hand below.",
                          type="warning", multi_line=True)
                return

            records = list(result.get("records") or [])
            misses = list(result.get("misses") or [])
            raw_how = str(result.get("how") or "")
            how = _FEEDS.get(raw_how, raw_how or "its feed")
            try:
                added = links.put_many(records)
            except Exception as e:
                _log(f"wizard: could not cache proposals — {e}", "err")
                ui.notify(f"Found matches but could not cache them: {e}", type="negative")
                return

            hits = len({(r.get("set_id"), r.get("line_id")) for r in records})
            scanned = _as_int(result.get("scanned"), 0, 0)
            ui.notify(f"{hits} {_plural(hits, 'product')} matched · "
                      f"{len(misses)} found nothing · {scanned} storefront "
                      f"{_plural(scanned, 'product')} read from {how}",
                      type="positive" if hits else "warning", multi_line=True)
            _log(f"wizard: discovery at {shop_name} — {hits} matched, {len(misses)} "
                 f"missed, {added} new {_plural(added, 'proposal')} cached from {how}",
                 "info")
            state["loaded"] = False
            body.refresh()

        # ------------------------------------------------------------------ #
        # proposals — fuzzy matches waiting on a human yes or no
        # ------------------------------------------------------------------ #
        def _cand_index(key, total: int) -> int:
            return max(0, min(_as_int(state["cand"].get(key), 0, 0), max(0, total - 1)))

        def _page_cand(key, delta: int, total: int) -> None:
            state["cand"][key] = (_cand_index(key, total) + delta) % max(1, total)
            body.refresh()

        def _use_candidate(entry: dict) -> None:
            """Confirm a proposal. From here on it is an ordinary listing, which
            is why step 3 needs no knowledge of the link index at all."""
            if links is None:
                return
            eid = str(entry.get("id") or "")
            if not eid:
                return
            if store is None:
                ui.notify("The product store is unavailable.", type="negative")
                return
            try:
                product = links.promote(eid, catalog=catalog, store=store)
            except Exception as e:
                _log(f"wizard: promote failed — {e}", "err")
                ui.notify(f"Could not use that link: {e}", type="negative")
                return
            if product is None:
                ui.notify("That proposal is no longer in the index.", type="warning")
                body.refresh()
                return
            sid, lid = entry.get("set_id") or "", entry.get("line_id") or ""
            _log(f"wizard: confirmed {_pair_label(sid, lid)} at "
                 f"{_name_of('store', entry.get('store_id') or '')} — "
                 f"{entry.get('url') or ''}", "info")
            ui.notify("Linked — it is a real listing now", type="positive")
            state["cand"].pop((sid, lid), None)
            state["loaded"] = False
            body.refresh()

        def _reject_candidate(entry: dict) -> None:
            """A no here outlives the cache — a later crawl never revives it."""
            if links is None:
                return
            eid = str(entry.get("id") or "")
            if not eid:
                return
            try:
                links.set_status(eid, REJECTED)
            except Exception as e:
                ui.notify(f"Could not save that: {e}", type="negative")
                return
            ui.notify("Dismissed — it will not be proposed again")
            state["cand"].pop((entry.get("set_id") or "", entry.get("line_id") or ""), None)
            body.refresh()

        def _score_chip(score) -> None:
            """Confidence as a percentage. Green reads as strong, amber as iffy."""
            pct = max(0.0, min(1.0, _as_float(score, 0.0, 0.0))) * 100
            tone = "rd-in_stock" if pct >= _STRONG * 100 else "rd-unknown"
            ui.label(f"{pct:.0f}% match").classes(f"rd-chip {tone}") \
                .tooltip("How closely the storefront's own title matches this set "
                         "and product line")

        def _prop_row(store_id: str, sid: str, lid: str) -> None:
            cands = _candidates(store_id, sid, lid)
            if not cands:
                return
            key, total = (sid, lid), len(cands)
            i = _cand_index(key, total)
            e = cands[i]
            url = str(e.get("url") or "")
            with ui.element("div").classes("rdw-prop"):
                with ui.element("div").classes("rdw-top"):
                    theme.thumb(theme.product_art(catalog, sid, lid),
                                _initials(_name_of("line", lid)),
                                size="26px", classes="rd-thumb-in")
                    with ui.column().classes("gap-0 grow min-w-0"):
                        ui.label(_pair_label(sid, lid)).classes("rd-pname")
                        title = str(e.get("title") or "")
                        ui.label(_short(title or "(the feed gave no title)", 68)) \
                            .classes("rd-pmeta").tooltip(title or url)
                    _score_chip(e.get("score"))
                    price = e.get("price")
                    if price is not None:
                        ui.label(f"${_as_float(price, 0.0, 0.0):.2f}") \
                            .classes("rd-lab rd-num") \
                            .tooltip("Price this store's feed advertises")
                with ui.element("div").classes("rdw-cand"):
                    if url:
                        ui.link(_short(url, 58), url, new_tab=True) \
                            .classes("rdw-url").tooltip(url)
                    if total > 1:
                        ui.button(icon="chevron_left",
                                  on_click=lambda _=None, k=key, n=total:
                                      _page_cand(k, -1, n)) \
                            .props("flat dense round size=sm").tooltip("Previous match")
                        ui.label(f"{i + 1} of {total}").classes("rdw-alt") \
                            .tooltip(f"{total} candidates were proposed for this product")
                        ui.button(icon="chevron_right",
                                  on_click=lambda _=None, k=key, n=total:
                                      _page_cand(k, 1, n)) \
                            .props("flat dense round size=sm").tooltip("Next match")
                    ui.element("div").classes("grow")
                    ui.button("Use this",
                              on_click=lambda _=None, c=e: _use_candidate(c)) \
                        .props("unelevated dense no-caps size=sm") \
                        .tooltip("Accept this match — it becomes a real listing")
                    ui.button("Not this one",
                              on_click=lambda _=None, c=e: _reject_candidate(c)) \
                        .props("flat dense no-caps size=sm") \
                        .tooltip("Never propose this URL for this product again")

        def _proposals_panel(store_id: str, found: list) -> None:
            shop_name = _name_of("store", store_id)
            n = len(found)
            with theme.card():
                with ui.element("div").classes("rdw-head"):
                    theme.lab("Proposed links")
                    ui.label(f"{n} {_plural(n, 'product')} matched at {shop_name} · "
                             "awaiting your confirmation").classes("rd-lab")
                with ui.element("div").classes("rdw-pad"):
                    theme.note(
                        "<b>These are proposals, not links.</b> Discovery matched a "
                        "storefront title to your set and product line, and title "
                        "matching is fuzzy — open the URL and check it is the right box "
                        "before accepting. <b>Use this</b> turns it into a real listing "
                        "that step 3 can run; <b>Not this one</b> stops it being "
                        "proposed again.")
                for sid, lid in found:
                    _prop_row(store_id, sid, lid)

        def _add_link(set_id: str, line_id: str, store_id: str,
                      url_in, freq_sel) -> None:
            url = str(url_in.value or "").strip()
            if not url:
                ui.notify("Paste the product URL first.", type="warning")
                return
            if store is None:
                ui.notify("The product store is unavailable.", type="negative")
                return
            try:
                catalog.add_listing(store, set_id=set_id, line_id=line_id,
                                    store_id=store_id, url=url,
                                    frequency=str(freq_sel.value or "5s"),
                                    priority="high")
            except Exception as e:
                ui.notify(f"Could not add the link: {e}", type="negative")
                return
            _log(f"wizard: linked {_pair_label(set_id, line_id)} at "
                 f"{_name_of('store', store_id)}", "info")
            ui.notify("Link added", type="positive")
            state["loaded"] = False
            body.refresh()

        def _gap_panel(store_id: str, missing: list) -> None:
            shop_name = _name_of("store", store_id)
            with theme.card():
                with ui.element("div").classes("rdw-head"):
                    theme.lab("Missing links")
                    with ui.row().classes("items-center gap-3"):
                        ui.label(f"{len(missing)} of {len(state['products'])} "
                                 f"{_plural(len(state['products']), 'product')} have no "
                                 f"URL at {shop_name}").classes("rd-lab")
                        _find_button(store_id)
                with ui.element("div").classes("rdw-pad"):
                    theme.block(
                        f"<b>{theme.esc(shop_name)} cannot cover this selection yet.</b> "
                        "A listing is a product URL on one store — without it there is "
                        "nothing for the runner to open. <b>Find product links</b> reads "
                        "the store's own public feed and proposes matches; paste the "
                        "rest by hand below, or continue with what is already linked.")
                for sid, lid in missing:
                    with ui.element("div").classes("rdw-gap"):
                        theme.thumb(theme.product_art(catalog, sid, lid),
                                    _initials(_name_of("line", lid)),
                                    size="24px", classes="rd-thumb-in")
                        with ui.column().classes("gap-0 grow min-w-0"):
                            ui.label(_pair_label(sid, lid)).classes("rd-pname")
                            ui.label(f"No URL at {shop_name}").classes("rd-pmeta")
                        url_in = ui.input(placeholder="https://…") \
                            .props("dense outlined").classes("grow") \
                            .style("min-width:230px")
                        freq_sel = ui.select(_FREQS, value="5s") \
                            .props("dense outlined options-dense").style("width:100px") \
                            .tooltip("How often this listing is polled")
                        ui.button("Add link",
                                  on_click=lambda _=None, s=sid, l=lid, u=url_in, f=freq_sel:
                                      _add_link(s, l, store_id, u, f)) \
                            .props("outline dense no-caps size=sm")

        def _step_retailer() -> None:
            ui.label("Where are you buying them?").classes("text-2xl font-bold")
            ui.label("Pick the retailer that will carry this drop. Each tile shows how "
                     "much of your selection it can already cover.").classes("rd-lede")

            if not state["products"]:
                theme.note("<b>Nothing selected.</b> Go back to step 1 and pick the "
                           "products you want first.")
                ui.button("Back to products", on_click=lambda: _goto(1)) \
                    .props("outline no-caps")
                return

            shops = _stores()
            if not shops:
                theme.note("<b>You have no retailers yet.</b> Add a store in the "
                           "<b>Monitors</b> tab first — a listing is a product URL on a "
                           "store, so the store has to exist before this step can "
                           "resolve anything.")
                ui.button("Back to products", on_click=lambda: _goto(1)) \
                    .props("outline no-caps")
                return

            with theme.card():
                with ui.element("div").classes("rdw-head"):
                    theme.lab("Your retailers")
                    n = len(state["products"])
                    ui.label(f"{n} {_plural(n, 'product')} selected").classes("rd-lab rd-num")
                with ui.element("div").classes("rdw-sgrid"):
                    for st in shops:
                        _store_tile(st)

            chosen = state["store"]
            if chosen:
                have, found, missing = _covered(chosen)
                total = len(have) + len(found) + len(missing)
                if found:
                    _proposals_panel(chosen, found)
                if missing:
                    _gap_panel(chosen, missing)
                with ui.element("div").classes("rdw-bar"):
                    bits = [_name_of("store", chosen), f"{len(have)} of {total} linked"]
                    if found:
                        bits.append(f"{len(found)} awaiting confirmation")
                    ui.label(" · ".join(bits)).classes("rd-lab rd-num")
                    with ui.row().classes("items-center gap-2"):
                        ui.button("Back", on_click=lambda: _goto(1)) \
                            .props("flat dense no-caps size=sm")
                        _find_button(chosen)
                        cont = ui.button(
                            f"Continue with {len(have)} {_plural(len(have), 'product')}",
                            on_click=lambda: _goto(3)).props("unelevated no-caps")
                        cont.set_enabled(bool(have))
            else:
                with ui.element("div").classes("rdw-bar"):
                    ui.label("Pick a retailer to continue").classes("rd-lab")
                    ui.button("Back", on_click=lambda: _goto(1)) \
                        .props("flat dense no-caps size=sm")

        # ------------------------------------------------------------------ #
        # step 3 — review & execute
        # ------------------------------------------------------------------ #
        def _as_int(value, fallback: int, low: int, high: int = 10 ** 6) -> int:
            try:
                v = int(float(value))
            except (TypeError, ValueError):
                return fallback
            return max(low, min(high, v))

        def _as_float(value, fallback: float, low: float) -> float:
            try:
                v = float(value)
            except (TypeError, ValueError):
                return fallback
            return v if v >= low else low

        def _rows() -> list:
            try:
                return list(queue.rows(store))
            except Exception as e:
                _log(f"wizard: queue read failed — {e}", "warn")
                return []

        def _enabled_entries() -> list:
            return [r["entry"] for r in _rows() if r["entry"].get("enabled", True)]

        def _profile_options() -> dict:
            opts = {"": "(default)"}
            try:
                for p in (profile_store.list() if profile_store is not None else []):
                    pid = str(p.get("id") or "")
                    if pid:
                        opts[pid] = p.get("name") or p.get("email") or pid
            except Exception as e:
                _log(f"wizard: profile list failed — {e}", "warn")
            return opts

        def _group_options() -> list:
            try:
                names = [str(n) for n in orch.proxy_groups.group_names()]
            except Exception:
                names = []
            return names or ["default"]

        def _set(pid: str, **fields) -> None:
            if not pid:
                return
            try:
                if queue.update(pid, **fields) is None:
                    ui.notify("That listing is no longer in the queue.", type="warning")
            except Exception as e:
                ui.notify(f"Could not save: {e}", type="negative")

        def _set_quiet(pid: str, **fields) -> bool:
            try:
                return bool(pid) and queue.update(pid, **fields) is not None
            except Exception:
                return False

        def _gate_dry() -> bool:
            """True while `checkout.force_dry_run` holds every task pre-payment."""
            try:
                return bool((settings.data.get("checkout") or {}).get("force_dry_run", True))
            except Exception:
                return True

        def _set_gate(dry: bool) -> None:
            try:
                settings.data.setdefault("checkout", {})["force_dry_run"] = bool(dry)
                settings.save()
            except Exception as e:
                ui.notify(f"Could not save the mode: {e}", type="negative")

        def _is_running() -> bool:
            try:
                return bool(orch.is_running) if orch is not None else False
            except Exception:
                return False

        def _blockers() -> list:
            try:
                return [str(r) for r in (live_blockers(profile_store) or [])]
            except Exception as e:
                return [f"The live pre-flight check failed: {e}"]

        def _evasion_master() -> bool:
            try:
                return bool((settings.data.get("evasion") or {}).get("enabled", False))
            except Exception:
                return False

        # -- run settings: the keys the orchestrator and monitor actually read - #
        def _sdata() -> dict:
            try:
                return settings.data
            except Exception:
                return {}

        def _start_at() -> str:
            return str((_sdata().get("schedule") or {}).get("start_at") or "")

        def _save_setting(section: str, key: str, value, what: str) -> bool:
            try:
                settings.data.setdefault(section, {})[key] = value
                settings.save()
                return True
            except Exception as e:
                ui.notify(f"Could not save {what}: {e}", type="negative")
                return False

        def _set_start_at(value: str) -> None:
            """Write `schedule.start_at`; "" means start immediately."""
            if _save_setting("schedule", "start_at", value, "the start time"):
                _log(f"wizard: start at {value or 'now'}", "info")
            _paint_countdown()

        def _clear_start() -> None:
            _set_start_at("")
            box = refs.get("stale")
            if box is not None:
                box.set_visibility(False)
            sw = refs.get("start_mode")
            if sw is not None:
                sw.value = "now"
            inp = refs.get("start_inp")
            if inp is not None:
                inp.value = ""
                inp.set_visibility(False)
            ui.notify("Start now — the run begins the moment you execute.")

        def _choose_start_mode(mode: str) -> None:
            inp = refs.get("start_inp")
            if mode == "now":
                if inp is not None:
                    inp.set_visibility(False)
                if _start_at():
                    _set_start_at("")
                box = refs.get("stale")
                if box is not None:
                    box.set_visibility(False)
                return
            if inp is not None:
                inp.set_visibility(True)
                _start_input_changed(inp.value)

        def _start_input_changed(value) -> None:
            raw = str(value or "").strip()
            if not raw:
                if _start_at():
                    _set_start_at("")
                _paint_countdown()
                return
            target = parse_start_at(raw)
            if target is None:
                ui.notify("That isn't a time — use the picker or HH:MM.", type="warning")
                _paint_countdown()
                return
            # Normalise to the one shape that round-trips through the parser
            # and the picker alike (datetime-local hands back "…THH:MM").
            _set_start_at(target.strftime(_AT_FMT))

        def _picker_value(raw: str) -> str:
            """`schedule.start_at` as a datetime-local value, or "" if unparseable."""
            target = parse_start_at(raw)
            return target.strftime("%Y-%m-%dT%H:%M:%S") if target else ""

        def _countdown_text(raw: str) -> tuple:
            """(text, soon) for the countdown label."""
            if not raw:
                return "Starts the moment you execute", False
            target = parse_start_at(raw)
            if target is None:
                return "Invalid start time — fix it or choose Start now", True
            secs = (target - datetime.now()).total_seconds()
            if secs <= 0:
                return f"Start time {target:%Y-%m-%d %H:%M:%S} has passed — runs immediately", False
            h, rem = divmod(int(secs), 3600)
            mi, sec = divmod(rem, 60)
            return (f"Starts in {h:02d}:{mi:02d}:{sec:02d} · at {target:%Y-%m-%d %H:%M:%S}",
                    secs < 60)

        def _paint_countdown() -> None:
            lbl = refs.get("countdown")
            if lbl is None:
                return
            text, soon = _countdown_text(_start_at())
            lbl.set_text(text)
            lbl.classes(replace="rdw-count" + (" rdw-soon" if soon else ""))

        def _force_checkout() -> bool:
            try:
                return bool((_sdata().get("wizard") or {}).get("force_checkout", False))
            except Exception:
                return False

        def _set_force_checkout(value) -> None:
            on = (value == "checkout")
            if _save_setting("wizard", "force_checkout", on, "the run mode"):
                _log("wizard: mode — " + ("straight to checkout" if on
                                          else "watch for stock first"), "info")

        def _workers() -> int:
            return _as_int((_sdata().get("orchestrator") or {}).get("max_workers"), 8, 1, 64)

        def _save_workers(value) -> None:
            _save_setting("orchestrator", "max_workers", _as_int(value, 8, 1, 64), "workers")

        def _cadence() -> float:
            return _as_float((_sdata().get("poll") or {}).get("interval_seconds"), 10.0, 0.5)

        def _save_cadence(value) -> None:
            _save_setting("poll", "interval_seconds", _as_float(value, 10.0, 0.5), "cadence")

        def _do_clear_queue() -> None:
            dlg = refs.get("clear_dialog")
            if dlg is not None:
                dlg.close()
            try:
                queue.clear()
            except Exception as e:
                ui.notify(f"Could not clear: {e}", type="negative")
                return
            state["loaded"] = True          # an emptied queue is a choice, not a miss
            _log("wizard: queue cleared", "warn")
            ui.notify("Queue cleared")
            queue_body.refresh()
            _paint_mode()

        def _run_now(tid: str) -> None:
            if orch is None or not tid:
                ui.notify("Unavailable", type="warning")
                return
            try:
                ok = bool(orch.run_task_now(tid))
            except Exception as e:
                ui.notify(f"Run failed: {e}", type="negative")
                return
            ui.notify("Running…" if ok else "That task is gone",
                      type="positive" if ok else "warning")
            _tick()

        def _stop_task(tid: str) -> None:
            """Stop one task where the orchestrator can; otherwise the run."""
            if orch is None or not tid:
                ui.notify("Unavailable", type="warning")
                return
            single = getattr(orch, "stop_task", None)
            try:
                if callable(single):
                    single(tid)
                    ui.notify("Stopping that task", type="info")
                else:
                    orch.stop()
                    ui.notify("This build stops the whole run — stopped.", type="info")
            except Exception as e:
                ui.notify(f"Stop failed: {e}", type="negative")
                return
            _tick()

        def _ensure_loaded() -> None:
            """Hand the resolved listings to the queue — once per entry to step 3."""
            if state.get("loaded"):
                return
            state["loaded"] = True
            shop = state["store"] or ""
            pids = [p["id"] for p in
                    (_at(sid, lid, shop) for sid, lid in _selected_pairs())
                    if p and p.get("id")]
            if not pids:
                return
            try:
                queue.add_many(pids)
            except Exception as e:
                _log(f"wizard: could not load the queue — {e}", "err")
                ui.notify(f"Could not load the queue: {e}", type="negative")
                return
            _log(f"wizard: loaded {len(pids)} {_plural(len(pids), 'listing')} from "
                 f"{_name_of('store', shop)} into the execute queue", "info")

        def _remove(pid: str) -> None:
            try:
                queue.remove(pid)
            except Exception as e:
                ui.notify(f"Could not remove: {e}", type="negative")
                return
            ui.notify("Removed from the run")
            queue_body.refresh()
            _paint_mode()

        def _field(label: str, width: str = "9rem"):
            col = ui.column().classes("rdw-field").style(f"width:{width}")
            with col:
                theme.lab(label)
            return col

        def _render_queue_row(row: dict, profiles: dict, groups: list) -> None:
            e, p = row["entry"], row["product"]
            pid = str(e.get("product_id") or p.get("id") or "")
            url = str(p.get("url") or "")
            on = bool(e.get("enabled", True))
            bits = [b for b in (_name_of("set", p.get("set_id", "")),
                                _name_of("line", p.get("line_id", "")),
                                _name_of("store", p.get("store_id", ""))) if b]

            holder = ui.element("div").classes("rdw-row" + ("" if on else " rdw-off"))
            with holder:
                with ui.element("div").classes("rdw-top"):
                    def _toggle(ev, _pid=pid, _h=holder) -> None:
                        live = bool(ev.value)
                        _set(_pid, enabled=live)
                        _h.classes(replace="rdw-row" + ("" if live else " rdw-off"))
                        _paint_mode()

                    ui.checkbox(value=on, on_change=_toggle).props("dense")
                    theme.thumb(theme.product_art(catalog, p.get("set_id") or "",
                                                  p.get("line_id") or ""),
                                _initials(_name_of("line", p.get("line_id", ""))),
                                size="26px", classes="rd-thumb-in")
                    with ui.column().classes("gap-0 grow min-w-0"):
                        ui.label(p.get("name") or url or pid).classes("rd-pname")
                        if bits:
                            ui.label(" · ".join(bits)).classes("rd-pmeta")
                        if url:
                            ui.label(_short(url)).classes("rdw-url").tooltip(url)
                    ui.button(icon="close", on_click=lambda _=None, _pid=pid: _remove(_pid)) \
                        .props("flat dense round size=sm").tooltip("Remove from the run")

                with ui.element("div").classes("rdw-ctrls"):
                    with _field("Qty", "5.5rem"):
                        ui.number(value=_as_int(e.get("quantity"), 1, 1, _MAX_QTY),
                                  min=1, max=_MAX_QTY, step=1,
                                  on_change=lambda ev, _pid=pid:
                                      _set(_pid, quantity=_as_int(ev.value, 1, 1, _MAX_QTY))) \
                            .props("outlined dense").classes("w-full")

                    with _field("Poll (s)", "6.5rem"):
                        ui.number(value=_as_float(e.get("poll_seconds"), 5.0, 0.5),
                                  min=0.5, step=0.5, format="%.1f",
                                  on_change=lambda ev, _pid=pid:
                                      _set(_pid, poll_seconds=_as_float(ev.value, 5.0, 0.5))) \
                            .props("outlined dense").classes("w-full")

                    with _field("Profile", "10.5rem"):
                        popts = dict(profiles)
                        cur = str(e.get("profile_id") or "")
                        if cur not in popts:
                            popts[cur] = cur
                        ui.select(popts, value=cur,
                                  on_change=lambda ev, _pid=pid:
                                      _set(_pid, profile_id=ev.value or "")) \
                            .props("outlined dense options-dense").classes("w-full")

                    with _field("Proxy group", "9.5rem"):
                        gopts = list(groups)
                        cur = str(e.get("proxy_group") or "default")
                        if cur not in gopts:
                            gopts.append(cur)
                        ui.select(gopts, value=cur,
                                  on_change=lambda ev, _pid=pid:
                                      _set(_pid, proxy_group=ev.value or "default")) \
                            .props("outlined dense options-dense").classes("w-full")

                    with _field("CAPTCHA", "8rem"):
                        cur = str(e.get("captcha_mode") or "manual")
                        if cur not in _CAPTCHA_MODES:
                            cur = "manual"
                        ui.select(dict(_CAPTCHA_MODES), value=cur,
                                  on_change=lambda ev, _pid=pid:
                                      _set(_pid, captcha_mode=ev.value or "manual")) \
                            .props("outlined dense options-dense").classes("w-full")

                    with _field("Evasion", "5rem"):
                        # The master switch in Settings → Evasion is a hard
                        # kill: off means no evasion for any row, and a row's
                        # own toggle can only turn evasion OFF for that row.
                        # The runner enforces that; this just says so.
                        master = _evasion_master()
                        ev_sw = ui.switch(value=bool(e.get("evasion", True)),
                                          on_change=lambda ev, _pid=pid:
                                              _set(_pid, evasion=bool(ev.value))) \
                            .props("dense").tooltip(_EVASION_TIP)
                        if not master:
                            ev_sw.set_enabled(False)

                    with _field("Dry run", "5rem"):
                        ui.switch(value=bool(e.get("dry_run", True)),
                                  on_change=lambda ev, _pid=pid: (
                                      _set(_pid, dry_run=bool(ev.value)), _paint_mode())) \
                            .props("dense") \
                            .tooltip("Off = this listing may place a real order")

        @ui.refreshable
        def queue_body() -> None:
            rows = _rows()
            if not rows:
                with ui.element("div").classes("rdw-pad"):
                    theme.note("<b>Nothing to run.</b> None of the products you picked "
                               "resolved to a listing at this retailer. Step back to "
                               "<b>Retailer</b> and add the missing links.")
                return
            profiles = _profile_options()
            groups = _group_options()
            for row in rows:
                _render_queue_row(row, profiles, groups)

        def _live_count() -> int:
            return sum(1 for e in _enabled_entries() if not e.get("dry_run", True))

        def _live_now() -> bool:
            """Will this run actually spend money?

            Two things have to agree: the global `checkout.force_dry_run` gate
            must be released, and at least one enabled listing must have its own
            dry-run switch off. A stored config can ship with the gate already
            open, so the badge reads the effect rather than the flag — and
            turning the badge to LIVE always costs the typed confirmation.
            """
            return (not _gate_dry()) and _live_count() > 0

        def _paint_mode() -> None:
            """Repaint everything that depends on the queue or the dry-run gate.

            Reads its widgets out of `refs`, so it is harmless to call from a
            row handler after the user has navigated away from step 3.
            """
            rows = _rows()
            enabled = _enabled_entries()
            live_n = _live_count()

            count_lbl = refs.get("count3")
            if count_lbl is not None:
                count_lbl.set_text(f"{len(enabled)} enabled · {len(rows)} total")

            badge, note_lbl, livebar = (refs.get("badge"), refs.get("mode_note"),
                                        refs.get("livebar"))
            if badge is None:
                return
            live = _live_now()

            sw = refs.get("live_sw")
            if sw is not None and bool(sw.value) != live:
                guard["busy"] = True
                sw.value = live
                guard["busy"] = False

            if live:
                badge.set_text("LIVE")
                badge.classes(replace="rd-badge rd-badge-live")
                if livebar is not None:
                    livebar.set_visibility(True)
                if note_lbl is not None:
                    note_lbl.set_text(
                        f"{live_n} of {len(enabled)} enabled "
                        f"{_plural(len(enabled), 'listing')} will place a real order.")
                return

            badge.set_text("Dry run")
            badge.classes(replace="rd-badge rd-badge-dry")
            if livebar is not None:
                livebar.set_visibility(False)
            if note_lbl is None:
                return
            if _gate_dry() and live_n:
                note_lbl.set_text(
                    f"{live_n} {_plural(live_n, 'listing')} set to live, but the "
                    "dry-run gate holds them. Flip LIVE to release it.")
            elif _gate_dry():
                note_lbl.set_text("The gate holds every task before the pay button. "
                                  "Nothing is charged.")
            else:
                note_lbl.set_text("The gate is open, but every enabled listing is still "
                                  "on dry run, so nothing will be charged.")

        def _apply_bulk() -> None:
            rows = _rows()
            if not rows:
                ui.notify("Nothing in the run yet.", type="warning")
                return
            fields = {
                "profile_id": refs["bulk_profile"].value or "",
                "proxy_group": refs["bulk_group"].value or "default",
                "poll_seconds": _as_float(refs["bulk_poll"].value, 5.0, 0.5),
                "dry_run": (refs["bulk_mode"].value != "live"),
            }
            n = sum(1 for r in rows
                    if _set_quiet(r["entry"].get("product_id", ""), **fields))
            ui.notify(f"Applied to {n} {_plural(n, 'listing')}")
            queue_body.refresh()
            _paint_mode()

        # -- the dry-run / LIVE gate ---------------------------------------- #
        def _toggle_live(on: bool) -> None:
            if guard["busy"]:
                return
            sw = refs.get("live_sw")
            if on:
                guard["busy"] = True
                if sw is not None:
                    sw.value = False        # stays dry until the word is typed
                guard["busy"] = False
                _open_live()
                return
            _set_gate(True)
            for row in _rows():
                _set_quiet(row["entry"].get("product_id", ""), dry_run=True)
            queue_body.refresh()
            _log("wizard: dry-run gate re-armed", "info")
            ui.notify("Dry run — nothing will be charged.", type="positive")
            _paint_mode()

        def _open_live() -> None:
            reasons = _blockers()
            box = refs.get("blockers")
            if box is not None:
                box.clear()
                with box:
                    if reasons:
                        theme.block(
                            "<b>Live is blocked.</b> A live order needs a profile with "
                            "a shipping address and a payment method. Fix these in "
                            "<b>Profiles</b>, then try again.")
                        for r in reasons:
                            ui.label(f"· {r}").classes("rd-lede") \
                                .style("font-size:12.5px")
            typed = refs.get("typed")
            if typed is not None:
                typed.value = ""
                typed.set_enabled(not reasons)
            go = refs.get("go_live")
            if go is not None:
                go.set_enabled(not reasons)
            dlg = refs.get("live_dialog")
            if dlg is not None:
                dlg.open()

        def _confirm_live() -> None:
            reasons = _blockers()
            if reasons:
                ui.notify("Live is blocked — see the reasons listed.", type="negative")
                _open_live()
                return
            typed = refs.get("typed")
            if str(getattr(typed, "value", "") or "").strip().upper() != "LIVE":
                ui.notify("Type LIVE to confirm.", type="warning")
                return
            if typed is not None:
                typed.value = ""
            dlg = refs.get("live_dialog")
            if dlg is not None:
                dlg.close()
            _set_gate(False)
            for row in _rows():
                _set_quiet(row["entry"].get("product_id", ""), dry_run=False)
            queue_body.refresh()
            guard["busy"] = True
            sw = refs.get("live_sw")
            if sw is not None:
                sw.value = True
            guard["busy"] = False
            _log("wizard: dry-run gate released — LIVE", "warn")
            ui.notify("LIVE — real orders will be placed.", type="warning")
            _paint_mode()

        # -- arm / stop ------------------------------------------------------ #
        def _disc_click() -> None:
            if orch is None:
                ui.notify("The orchestrator isn't available.", type="warning")
                return
            if _is_running():
                try:
                    orch.stop()
                except Exception as e:
                    ui.notify(f"Stop failed: {e}", type="negative")
                    return
                ui.notify("Stopped", type="info")
                _tick()
                return

            if task_store is None or profile_store is None:
                ui.notify("Tasks or profiles are unavailable — cannot execute.",
                          type="warning")
                return
            if not _enabled_entries():
                ui.notify("Enable at least one product first.", type="warning")
                return
            try:
                made = queue.materialize(store=store, task_store=task_store,
                                         profile_store=profile_store)
            except Exception as e:
                _log(f"wizard: materialize failed — {e}", "err")
                ui.notify(f"Could not build tasks: {e}", type="negative")
                return
            n = len(made or [])
            straight = _force_checkout()
            try:
                orch.reload_managers()
                # Watch first is the orchestrator's default; only the other
                # mode passes the flag, so a stub without it keeps working.
                if straight:
                    orch.start(force_checkout=True)
                else:
                    orch.start()
            except Exception as e:
                _log(f"wizard: start failed — {e}", "err")
                ui.notify(f"Start failed: {e}", type="negative")
                return
            live = not _gate_dry()
            mode = "LIVE" if live else "dry run"
            how = "straight to checkout" if straight else "watching for stock"
            _log(f"wizard: executed {n} {_plural(n, 'task')} — {mode}, {how}",
                 "warn" if live else "info")
            ui.notify(f"Created {n} {_plural(n, 'task')} — {mode}",
                      type="warning" if live else "positive")
            # The run has started: the page becomes the run.
            state["step"] = 4
            cache.update(sig=None, running=None, lsig=None)
            body.refresh()

        # -- the live board -------------------------------------------------- #
        def _exec_tasks() -> list:
            if task_store is None:
                return []
            try:
                return [t for t in task_store.list() if t.get("origin") == ORIGIN]
            except Exception:
                return []

        def _render_board(tasks: list) -> None:
            box = refs.get("board")
            if box is None:
                return
            try:
                box.clear()
            except Exception:
                return
            with box:
                if not tasks:
                    with ui.element("div").classes("rdw-pad"):
                        ui.label("No tasks yet. EXECUTE turns the products above into "
                                 "tasks and they appear here.").classes("rd-lede")
                    return
                for t in tasks:
                    p = cache["by_url"].get(str(t.get("url") or "")) or {}
                    with ui.element("div").classes("rdw-board"):
                        theme.thumb(theme.product_art(catalog, p.get("set_id") or "",
                                                      p.get("line_id") or ""),
                                    _initials(_name_of("line", p.get("line_id", ""))),
                                    size="22px", classes="rd-thumb-in")
                        with ui.column().classes("gap-0 grow min-w-0"):
                            ui.label(t.get("name") or t.get("url") or t.get("id") or "—") \
                                .classes("rd-pname")
                            detail = t.get("last_error") or t.get("message") or ""
                            if detail:
                                ui.label(_short(detail, 72)).classes("rd-pmeta") \
                                    .tooltip(str(detail))
                        ui.label(f"try {_as_int(t.get('attempt'), 0, 0)}") \
                            .classes("rd-lab rd-num")
                        theme.chip(str(t.get("status") or "idle"))
                        tid = str(t.get("id") or "")
                        ui.button(icon="play_arrow",
                                  on_click=lambda _=None, _t=tid: _run_now(_t)) \
                            .props("flat dense round size=sm") \
                            .tooltip("Run now — skip the watch and check out this one")
                        if str(t.get("status")) not in _FINISHED and \
                                str(t.get("status")) != "idle":
                            ui.button(icon="stop",
                                      on_click=lambda _=None, _t=tid: _stop_task(_t)) \
                                .props("flat dense round size=sm").tooltip("Stop")

        # -- the results ledger (step 4) ------------------------------------ #
        def _explain(task: dict) -> str:
            """One plain sentence per finished task. Same words as Results."""
            status = task.get("status")
            if status == STATUS_SUCCESS:
                if task.get("dry_run", True):
                    return "Dry run reached the final step — stopped before paying."
                oid = task.get("order_id")
                return f"Order placed · {oid}" if oid else "Order placed."
            if status == STATUS_FAILED:
                reason = task.get("last_error") or task.get("message")
                if task.get("denied"):
                    # The gate refused the page on purpose. Say so plainly,
                    # without repeating the "aborted before add-to-cart"
                    # prefix already in reason.
                    why = (reason or "").split("—", 1)[-1].strip() or "the page was refused"
                    return (f"Denied before add-to-cart: {why}. "
                            "Nothing was carted and nothing was bought.")
                return reason or "Failed — no reason recorded."
            if status == STATUS_STOPPED:
                return "Stopped before it finished."
            return task.get("message") or ""

        def _finished(tasks: list) -> list:
            return [t for t in tasks if t.get("status") in _FINISHED]

        def _render_ledger(tasks: list) -> None:
            box = refs.get("ledger")
            if box is None:
                return
            try:
                box.clear()
            except Exception:
                return
            with box:
                if not tasks:
                    with ui.element("div").classes("rdw-pad"):
                        ui.label("Nothing has finished yet. Results appear here as each "
                                 "task ends — including a dry run, which records how "
                                 "far it got.").classes("rd-lede")
                    return
                for t in tasks:
                    status = str(t.get("status") or "")
                    p = cache["by_url"].get(str(t.get("url") or "")) or {}
                    site = p.get("site") or t.get("site") or "—"
                    edge = _BORDER.get("denied" if t.get("denied") else status, "transparent")
                    with ui.element("div").classes("rdw-led") \
                            .style(f"border-left-color:{edge}"):
                        theme.chip(status or "idle")
                        theme.thumb(theme.product_art(catalog, p.get("set_id") or "",
                                                      p.get("line_id") or ""),
                                    _initials(t.get("name") or site),
                                    size="24px", classes="rd-thumb-in")
                        with ui.column().classes("gap-0 grow min-w-0"):
                            ui.label(t.get("name") or t.get("url") or t.get("id") or "—") \
                                .classes("rd-pname")
                            ui.label(_explain(t)).classes("rd-pmeta")
                            shot = str(t.get("screenshot") or "")
                            if shot:
                                # A screenshot is a file on disk; the runner
                                # logs the same path. Show it where it can be
                                # found, as a link where the path is one.
                                if shot.startswith(("http://", "https://", "/")):
                                    ui.link("screenshot: " + _short(shot, 40), shot,
                                            new_tab=True).classes("rdw-shot").tooltip(shot)
                                else:
                                    ui.label("screenshot: " + _short(shot, 40)) \
                                        .classes("rdw-shot").tooltip(shot)
                        ui.label(str(site)).classes("rd-lab")

        def _summary_html(tasks: list) -> str:
            done = _finished(tasks)
            running = len(tasks) - len(done)
            ok = sum(1 for t in done if t.get("status") == STATUS_SUCCESS)
            denied = sum(1 for t in done if t.get("status") == STATUS_FAILED and t.get("denied"))
            failed = sum(1 for t in done if t.get("status") == STATUS_FAILED) - denied
            stopped = sum(1 for t in done if t.get("status") == STATUS_STOPPED)
            bits = [f"<b>{running}</b> running", f"<b>{ok}</b> success",
                    f"<b>{denied}</b> denied", f"<b>{failed}</b> failed"]
            if stopped:
                bits.append(f"<b>{stopped}</b> stopped")
            return '<span class="rdw-sum">' + " · ".join(bits) + "</span>"

        def _tick_ledger() -> None:
            """The 2s refresh Results has always had, signature-gated."""
            if refs.get("ledger") is None:
                return
            tasks = _exec_tasks()
            done = _finished(tasks)
            sig = "|".join(
                f"{t.get('id')}~{t.get('status')}~{t.get('denied')}~{t.get('order_id')}~"
                f"{t.get('last_error')}~{t.get('message')}~{t.get('screenshot')}~"
                f"{t.get('updated')}" for t in done)
            if sig != cache.get("lsig"):
                cache["lsig"] = sig
                _render_ledger(done)
                lbl = refs.get("ledger_lbl")
                if lbl is not None:
                    lbl.set_text(f"{len(done)} finished")

        def _tick() -> None:
            running = _is_running()
            disc, disc_lbl = refs.get("disc"), refs.get("disc_lbl")
            if disc is not None and running != cache["running"]:
                cache["running"] = running
                if running:
                    disc.classes(add="rdw-stop")
                    if disc_lbl is not None:
                        disc_lbl.set_content('<div class="rdw-disclab">Stop</div>')
                else:
                    disc.classes(remove="rdw-stop")
                    if disc_lbl is not None:
                        disc_lbl.set_content('<div class="rdw-disclab">Execute</div>')
                sw = refs.get("live_sw")
                if sw is not None:
                    sw.set_enabled(not running)
            _paint_countdown()
            stop_btn = refs.get("stop_all")
            if stop_btn is not None:
                stop_btn.set_enabled(running)

            state_lbl = refs.get("state_lbl")
            if state_lbl is not None:
                try:
                    snap = orch.snapshot() if orch is not None else {}
                except Exception:
                    snap = {}
                if running:
                    active = _as_int(snap.get("active_tasks"), 0, 0)
                    bit = (f"checking out {active} task(s)" if active
                           else f"watching {_as_int(snap.get('enabled_tasks'), 0, 0)} task(s)")
                    state_lbl.set_content(
                        f'<span class="rdw-state"><span class="rdw-dot"></span>'
                        f'{theme.esc(bit)} · phase {theme.esc(snap.get("phase") or "…")}'
                        f'</span>')
                else:
                    state_lbl.set_content('<span class="rdw-state">Not armed</span>')

            tasks = _exec_tasks()
            sig = "|".join(
                f"{t.get('id')}~{t.get('status')}~{t.get('attempt')}~"
                f"{t.get('message')}~{t.get('last_error')}~{t.get('updated')}"
                for t in tasks)
            if sig != cache["sig"]:
                cache["sig"] = sig
                _render_board(tasks)
                lbl = refs.get("board_lbl")
                if lbl is not None:
                    lbl.set_text(f"{len(tasks)} {_plural(len(tasks), 'task')}")
                summ = refs.get("summary")
                if summ is not None:
                    summ.set_content(_summary_html(tasks))

        def _step_execute() -> None:
            shop = state["store"] or ""
            ui.label("Review & execute").classes("text-2xl font-bold")
            ui.label(f"Everything you picked, resolved to its listing at "
                     f"{_name_of('store', shop) or 'your retailer'}. Set how each one "
                     "runs, then execute.").classes("rd-lede")

            if queue is None:
                theme.note("<b>The execute queue isn't available.</b> This build has no "
                           "<code>ctx.queue</code>, so there is nothing to arm.")
                ui.button("Back to retailer", on_click=lambda: _goto(2)) \
                    .props("outline no-caps")
                return

            try:
                queue.prune(store)
            except Exception as e:
                _log(f"wizard: prune failed — {e}", "warn")
            _ensure_loaded()

            # -- the LIVE confirmation dialog ------------------------------- #
            with ui.dialog() as live_dialog, ui.card().classes("w-[27rem] max-w-full"):
                ui.label("Go live?").classes("text-lg font-bold") \
                    .style("color:var(--rd-live)")
                ui.label("This releases the dry-run gate and switches every listing in "
                         "this run to live. The moment one shows stock it places a REAL "
                         "order and charges the card on its profile.").classes("rd-lede")
                refs["blockers"] = ui.column().classes("w-full gap-2")
                refs["typed"] = ui.input(placeholder="type LIVE") \
                    .props("outlined dense").classes("w-full").style("font-family:var(--rd-mono)")
                with ui.row().classes("justify-end gap-2 w-full"):
                    ui.button("Cancel", on_click=live_dialog.close).props("flat no-caps")
                    refs["go_live"] = ui.button("Go live", color="red",
                                                on_click=lambda: _confirm_live()) \
                        .props("no-caps")
            refs["live_dialog"] = live_dialog

            # -- the run ---------------------------------------------------- #
            with theme.card():
                with ui.element("div").classes("rdw-head"):
                    theme.lab("The run")
                    refs["count3"] = ui.label("").classes("rd-lab rd-num")
                with ui.element("div").classes("rdw-bulk"):
                    ui.label("Apply to all").classes("rd-lab").style("align-self:center")
                    with _field("Profile", "10rem"):
                        refs["bulk_profile"] = ui.select(_profile_options(), value="") \
                            .props("outlined dense options-dense").classes("w-full")
                    with _field("Proxy group", "9rem"):
                        groups_now = _group_options()
                        refs["bulk_group"] = ui.select(groups_now, value=groups_now[0]) \
                            .props("outlined dense options-dense").classes("w-full")
                    with _field("Poll (s)", "6.5rem"):
                        refs["bulk_poll"] = ui.number(value=5.0, min=0.5, step=0.5,
                                                      format="%.1f") \
                            .props("outlined dense").classes("w-full")
                    with _field("Mode", "8rem"):
                        refs["bulk_mode"] = ui.select({"dry": "Dry run", "live": "Live"},
                                                      value="dry") \
                            .props("outlined dense options-dense").classes("w-full")
                    ui.button("Apply", on_click=lambda: _apply_bulk()) \
                        .props("outline dense no-caps size=sm")
                queue_body()

            # -- run settings: when, how, how wide, how often ---------------- #
            with ui.dialog() as clear_dialog, ui.card().classes("w-[23rem] max-w-full"):
                ui.label("Clear the queue?").classes("text-lg font-bold")
                ui.label("Every listing in this run is removed. Your product and "
                         "retailer picks stay; step back to reload them.").classes("rd-lede")
                with ui.row().classes("justify-end gap-2 w-full"):
                    ui.button("Cancel", on_click=clear_dialog.close).props("flat no-caps")
                    ui.button("Clear queue", color="red",
                              on_click=lambda: _do_clear_queue()).props("no-caps")
            refs["clear_dialog"] = clear_dialog

            preset = _start_at()
            with theme.card():
                with ui.element("div").classes("rdw-head"):
                    theme.lab("Run settings")
                    ui.button("Clear queue", on_click=clear_dialog.open) \
                        .props("flat dense no-caps size=sm")
                # A start time set on an earlier day — or on the Drop Timer
                # page — silently parks EXECUTE in "scheduled". Say so first.
                refs["stale"] = ui.element("div").classes("rdw-stale")
                with refs["stale"]:
                    when = parse_start_at(preset)
                    ui.html(
                        "A start time is already set: <b>"
                        + theme.esc(f"{when:%Y-%m-%d %H:%M:%S}" if when else preset)
                        + "</b>"
                        + (" — EXECUTE will wait until then." if when and when > datetime.now()
                           else " — it has passed, so the run starts immediately."),
                        sanitize=False)
                    ui.button("Clear — start now", on_click=lambda: _clear_start()) \
                        .props("outline dense no-caps size=sm")
                refs["stale"].set_visibility(bool(preset))

                with ui.element("div").classes("rdw-run"):
                    with _field("Start", "13rem"):
                        refs["start_mode"] = ui.radio(
                            {"now": "Start now", "at": "Start at"},
                            value="at" if preset else "now",
                            on_change=lambda ev: _choose_start_mode(ev.value)) \
                            .props("inline dense")
                    with _field("Start at", "14rem"):
                        refs["start_inp"] = ui.input(
                            value=_picker_value(preset),
                            on_change=lambda ev: _start_input_changed(ev.value)) \
                            .props('outlined dense type="datetime-local" step="1"') \
                            .classes("w-full")
                    refs["start_inp"].set_visibility(bool(preset))
                    refs["countdown"] = ui.label("").classes("rdw-count") \
                        .style("align-self:center;min-width:16rem")

                with ui.element("div").classes("rdw-run").style(
                        "border-top:1px solid var(--rd-line-soft)"):
                    with _field("Mode", "22rem"):
                        ui.radio({"watch": "Watch for stock first",
                                  "checkout": "Go straight to checkout"},
                                 value="checkout" if _force_checkout() else "watch",
                                 on_change=lambda ev: _set_force_checkout(ev.value)) \
                            .props("inline dense") \
                            .tooltip("Straight to checkout skips the stock watch and "
                                     "runs every enabled task the moment you execute")
                    with _field("Parallel workers", "8rem"):
                        ui.number(value=_workers(), min=1, max=64, step=1,
                                  on_change=lambda ev: _save_workers(ev.value)) \
                            .props("outlined dense").classes("w-full") \
                            .tooltip("How many tasks may check out at once")
                    with _field("Monitor cadence (s)", "9rem"):
                        ui.number(value=_cadence(), min=0.5, step=0.5, format="%.1f",
                                  on_change=lambda ev: _save_cadence(ev.value)) \
                            .props("outlined dense").classes("w-full") \
                            .tooltip("Base seconds between stock checks while watching")
                _paint_countdown()

            # -- the drop moment --------------------------------------------- #
            with theme.card():
                with ui.element("div").classes("rdw-arm"):
                    with ui.element("div").classes("rd-ritual"):
                        ui.element("i")
                        ui.element("i")
                        ui.element("i")
                        # color=None keeps NiceGUI from adding Quasar's
                        # bg-primary, whose utilities are !important.
                        refs["disc"] = ui.button(on_click=lambda: _disc_click(),
                                                 color=None) \
                            .classes("rd-disc").props("round")
                        with refs["disc"]:
                            refs["disc_lbl"] = ui.html(
                                '<div class="rdw-disclab">Execute</div>', sanitize=False)
                    refs["state_lbl"] = ui.html(
                        '<span class="rdw-state">Not armed</span>', sanitize=False)
                    ui.element("div").style(
                        "height:1px;width:100%;background:var(--rd-line);margin:2px 0")
                    with ui.row().classes("items-center gap-3"):
                        refs["badge"] = ui.label("Dry run").classes("rd-badge rd-badge-dry")
                        refs["live_sw"] = ui.switch(
                            "LIVE", value=_live_now(),
                            on_change=lambda ev: _toggle_live(bool(ev.value))) \
                            .props("dense")
                    refs["mode_note"] = ui.label("").classes("rd-lede") \
                        .style("font-size:12px;text-align:center;max-width:44ch")
                    refs["livebar"] = theme.block(
                        "<b>LIVE.</b> Real orders, real money, the moment a listing "
                        "shows stock.")

            # -- the board ---------------------------------------------------- #
            with theme.card():
                with ui.element("div").classes("rdw-head"):
                    theme.lab("Live board")
                    refs["board_lbl"] = ui.label("").classes("rd-lab rd-num")
                refs["board"] = ui.column().classes("w-full gap-0")

            with ui.element("div").classes("rdw-bar"):
                ui.label(f"{_name_of('store', shop)} · "
                         f"{len(state['products'])} "
                         f"{_plural(len(state['products']), 'product')} picked") \
                    .classes("rd-lab rd-num")
                with ui.row().classes("items-center gap-2"):
                    ui.button("Back", on_click=lambda: _goto(2)) \
                        .props("flat dense no-caps size=sm")
                    ui.button("Start over", on_click=_start_over) \
                        .props("flat dense no-caps size=sm")

            _paint_mode()
            _tick()
            # Lives inside this step's slot, so a step change disposes of it.
            ui.timer(1.0, _tick)

        def _start_over() -> None:
            reset()
            cache.update(sig=None, running=None, lsig=None)
            body.refresh()

        # ------------------------------------------------------------------ #
        # step 4 — running
        # ------------------------------------------------------------------ #
        def _stop_all() -> None:
            if orch is None:
                ui.notify("The orchestrator isn't available.", type="warning")
                return
            try:
                orch.stop()
            except Exception as e:
                ui.notify(f"Stop failed: {e}", type="negative")
                return
            _log("wizard: STOP ALL", "warn")
            ui.notify("Stopped", type="info")
            _tick()

        def _run_again() -> None:
            # Back to the review with the queue exactly as it ran.
            state["loaded"] = True
            _goto(3)

        def _new_run(clear_queue: bool) -> None:
            dlg = refs.get("new_dialog")
            if dlg is not None:
                dlg.close()
            if clear_queue:
                try:
                    queue.clear()
                except Exception as e:
                    ui.notify(f"Could not clear the queue: {e}", type="negative")
                    return
                _log("wizard: queue cleared for a new run", "info")
            _start_over()

        def _clear_results() -> None:
            if task_store is None:
                return
            n = 0
            for t in _finished(_exec_tasks()):
                try:
                    task_store.remove(t["id"])
                    n += 1
                except Exception as e:
                    ui.notify(f"Could not remove {t.get('name') or t.get('id')}: {e}",
                              type="negative")
            ui.notify(f"Cleared {n} finished {_plural(n, 'task')}", type="positive")
            cache.update(sig=None, lsig=None)
            _tick()
            _tick_ledger()

        def _step_running() -> None:
            shop = state["store"] or ""
            ui.label("Running").classes("text-2xl font-bold")
            ui.label(f"The run at {_name_of('store', shop) or 'your retailer'}. Every task "
                     "reports here as it moves; finished ones land in the ledger "
                     "below.").classes("rd-lede")

            with ui.dialog() as new_dialog, ui.card().classes("w-[25rem] max-w-full"):
                ui.label("Start a new run?").classes("text-lg font-bold")
                ui.label("Your product and retailer picks are cleared and the wizard "
                         "goes back to step 1. The queue can stay for next time or "
                         "go with them.").classes("rd-lede")
                with ui.row().classes("justify-end gap-2 w-full"):
                    ui.button("Cancel", on_click=new_dialog.close).props("flat no-caps")
                    ui.button("Keep queue", on_click=lambda: _new_run(False)) \
                        .props("outline no-caps")
                    ui.button("Clear queue too", color="red",
                              on_click=lambda: _new_run(True)).props("no-caps")
            refs["new_dialog"] = new_dialog

            with theme.card():
                with ui.element("div").classes("rdw-head"):
                    refs["summary"] = ui.html(_summary_html([]), sanitize=False)
                    refs["state_lbl"] = ui.html(
                        '<span class="rdw-state">Not armed</span>', sanitize=False)
                with ui.element("div").classes("rdw-acts").style("padding:11px 15px"):
                    # Explicit red like the Go live button; the themed buttons
                    # next to it take color=None so Quasar's bg-primary stays off.
                    refs["stop_all"] = ui.button("STOP ALL", icon="stop", color="red",
                                                 on_click=lambda: _stop_all()) \
                        .props("unelevated no-caps")
                    ui.button("Run again", icon="replay", on_click=lambda: _run_again(),
                              color=None).props("outline no-caps") \
                        .tooltip("Back to Execute with the queue as it is")
                    ui.button("New run", icon="add", on_click=new_dialog.open,
                              color=None).props("outline no-caps") \
                        .tooltip("Clear the selection and start from step 1")
                    ui.button("Clear results", icon="delete_sweep",
                              on_click=lambda: _clear_results(), color=None) \
                        .props("flat no-caps") \
                        .tooltip("Remove finished tasks from this run only")

            with theme.card():
                with ui.element("div").classes("rdw-head"):
                    theme.lab("Live board")
                    refs["board_lbl"] = ui.label("").classes("rd-lab rd-num")
                refs["board"] = ui.column().classes("w-full gap-0")

            with theme.card():
                with ui.element("div").classes("rdw-head"):
                    theme.lab("Results")
                    refs["ledger_lbl"] = ui.label("").classes("rd-lab rd-num")
                refs["ledger"] = ui.column().classes("w-full gap-0")

            with ui.element("div").classes("rdw-bar"):
                ui.label(f"{_name_of('store', shop)} · "
                         f"{len(state['products'])} "
                         f"{_plural(len(state['products']), 'product')} picked") \
                    .classes("rd-lab rd-num")
                ui.button("Back to execute", on_click=lambda: _goto(3)) \
                    .props("flat dense no-caps size=sm")

            _tick()
            _tick_ledger()
            # Both live inside this step's slot, so a step change disposes of them.
            ui.timer(1.0, _tick)
            ui.timer(2.0, _tick_ledger)

        # ------------------------------------------------------------------ #
        # the page
        # ------------------------------------------------------------------ #
        @ui.refreshable
        def body() -> None:
            cache["idx"] = _build_index()
            cache["links"] = _build_links()
            refs.clear()
            # Fresh widgets, fresh signatures: the board and ledger paint
            # once on entry to a step rather than waiting for a change.
            cache.update(sig=None, lsig=None, running=None)
            with ui.row().classes("w-full items-center"):
                _stepper()
            step = state["step"]
            if step == 1:
                _step_products()
            elif step == 2:
                _step_retailer()
            elif step == 4:
                _step_running()
            else:
                _step_execute()

        if catalog is None or store is None:
            theme.note("<b>The wizard can't run in this build.</b> It needs "
                       "<code>ctx.catalog</code> and <code>ctx.store</code> to turn a "
                       "set and a product line into a listing. Use the "
                       "<b>Monitors</b> tab instead.")
            return

        body()
