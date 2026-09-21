"""Monitors — the catalog, drilled down to the listings you actually run.

A drop is chosen top-down: which SET is dropping, which PRODUCT LINE you want,
and which of your STORES will carry it. This tab walks exactly that path:

    set grid  →  product lines  →  per-store listings  →  Execute

Listings ARE products — core.catalog tags a normal product with the three ids —
so nothing here invents a second source of truth. It reads and writes through
CatalogStore, and hands the checked product ids to ExecuteQueue, which is what
the Execute stage materialises tasks from.
"""
from __future__ import annotations

import inspect
import re

from nicegui import run, ui

from raindance.core.catalog import PLATFORMS
from raindance.core.registry import ToolPlugin, register_tool
from raindance.ui import theme

# The profile schema is what the Fields editor offers as targets. It is read
# only, and guarded so a build without the profile store still renders the
# page — the dropdown just falls back to the paths every profile has had.
try:
    from raindance.profiles.store import DEFAULT_PROFILE as _PROFILE_SCHEMA
except Exception:  # pragma: no cover - defensive
    _PROFILE_SCHEMA = {"email": "", "phone": ""}

# The public set database is optional the same way link discovery is: a build
# without it keeps the manual "add by name" path and simply never offers to go
# looking. It is imported here, not at call time, so one missing module can
# never be the reason this tab fails to render.
try:
    from raindance.core import tcg_api
except Exception:  # pragma: no cover - defensive
    tcg_api = None  # type: ignore[assignment]

# Cadences worth offering on a drop. Slower than 30s is a watchlist, not a drop.
_FREQS = ["5s", "10s", "15s", "30s"]

# A set's accent comes from config.json and is interpolated into a style
# attribute, so only a plain hex colour is allowed through.
_HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

_CSS = """
<style>
/* breadcrumb */
.rd-crumbs { display:flex; align-items:center; gap:9px; flex-wrap:wrap;
             font-family:var(--rd-mono); font-size:11px; letter-spacing:.1em;
             text-transform:uppercase; }
.rd-crumb { color:var(--rd-ink-dim); cursor:pointer; }
.rd-crumb:hover { color:var(--rd-ink); }
.rd-crumb-now { color:var(--rd-ink); font-weight:700; }
.rd-crumb-sep { color:var(--rd-ink-faint); }

/* set grid */
.rd-setgrid { display:grid; grid-template-columns:repeat(auto-fill,minmax(174px,1fr));
              gap:14px; padding:15px; }
.rd-setcard { display:flex; flex-direction:column; gap:9px; padding:10px; cursor:pointer;
              border:1px solid var(--rd-line); border-radius:5px;
              background:var(--rd-surface); }
.rd-setcard:hover { border-color:var(--rd-watch); }
/* the tile is the well a real set image drops into. theme.thumb draws the
   image inside it and the code badge behind it — the cover crop that used to
   live here is gone on purpose: set logos are wide and the well is square, so
   they letterbox (theme's `.rd-thumb > img`) rather than being cut in half. */
.rd-tile { aspect-ratio:1/1; width:100%; border-radius:5px; overflow:hidden;
           display:grid; place-items:center; }
.rd-tilecode { font-family:var(--rd-mono); font-weight:700; letter-spacing:.12em;
               font-size:30px; color:var(--rd-ink); }

/* rows */
.rd-lnrow { display:flex; align-items:center; gap:13px; padding:12px 15px; cursor:pointer;
            border-bottom:1px solid var(--rd-line-soft); }
.rd-lnrow:last-child { border-bottom:0; }
.rd-lnrow:hover { background:var(--rd-surface-2); }
.rd-lnrow.rd-vacant { opacity:.6; }
.rd-short { font-family:var(--rd-mono); font-size:11px; letter-spacing:.08em;
            text-transform:uppercase; color:var(--rd-ink-dim); min-width:70px; }
.rd-lsrow { display:flex; align-items:center; gap:12px; padding:11px 15px;
            border-bottom:1px solid var(--rd-line-soft); }
.rd-lsrow:last-child { border-bottom:0; }
.rd-url { font-family:var(--rd-mono); font-size:11.5px; color:var(--rd-ink-faint);
          overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.rd-chip.rd-plat { color:var(--rd-ink-dim);
                   background:color-mix(in srgb,var(--rd-ink-dim) 15%,transparent); }
.rd-chip.rd-off  { color:var(--rd-ink-faint);
                   background:color-mix(in srgb,var(--rd-ink-faint) 13%,transparent); }

/* section furniture */
.rd-sechead { display:flex; align-items:center; justify-content:space-between; gap:12px;
              padding:13px 15px; border-bottom:1px solid var(--rd-line); }
.rd-form { display:flex; flex-wrap:wrap; align-items:flex-end; gap:11px;
           padding:13px 15px; border-top:1px solid var(--rd-line); }
.rd-pad { padding:15px; display:flex; flex-direction:column; gap:10px; }

/* add-set dialog. `rdm-` is this tab's own prefix: `rd-` is shared with the
   theme and `rdw-` belongs to the wizard, and a shared single-class name is
   exactly how the old .rd-tile collision happened. */
.rdm-dlg  { width:min(580px,92vw); }
.rdm-hits { max-height:44vh; overflow-y:auto; }
.rdm-hit  { display:flex; align-items:center; gap:12px; padding:10px 15px;
            cursor:pointer; border-bottom:1px solid var(--rd-line-soft); }
.rdm-hit:last-child { border-bottom:0; }
.rdm-hit:hover { background:var(--rd-surface-2); }
/* a set logo is wide, so its well in the dialog is too — still letterboxed */
.rdm-logo { width:56px; height:38px; border-radius:3px; flex:0 0 auto;
            background:var(--rd-surface-2); border:1px solid var(--rd-line-soft); }
.rdm-logo .rd-thumb-txt { font-size:11px; color:var(--rd-ink-faint); }
.rdm-sep { display:flex; align-items:center; gap:10px; padding:6px 15px 0;
           color:var(--rd-ink-faint); font-family:var(--rd-mono); font-size:10px;
           letter-spacing:.12em; text-transform:uppercase; }
.rdm-sep::before, .rdm-sep::after { content:""; flex:1; height:1px;
                                    background:var(--rd-line-soft); }
.rdm-msg { padding:14px 15px; color:var(--rd-ink-dim); font-size:13px; }

/* per-store customize dialog */
.rdm-cust { width:min(760px,94vw); max-height:88vh; display:flex; flex-direction:column; }
.rdm-cust-body { overflow-y:auto; padding:4px 15px 12px; display:flex;
                 flex-direction:column; gap:14px; }
.rdm-caps { display:flex; align-items:baseline; gap:9px; flex-wrap:wrap;
            padding:11px 15px; border-bottom:1px solid var(--rd-line-soft);
            font-size:12.5px; color:var(--rd-ink-dim); }
.rdm-caps b { font-family:var(--rd-mono); font-size:10.5px; letter-spacing:.12em;
              text-transform:uppercase; color:var(--rd-ink-faint); }
.rdm-sect { display:flex; flex-direction:column; gap:8px; }
.rdm-sect-h { display:flex; align-items:baseline; gap:10px; }
.rdm-hint { color:var(--rd-ink-faint); font-size:11.5px; }
.rdm-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(210px,1fr)); gap:10px; }
.rdm-grid textarea, .rdm-cust textarea { font-family:var(--rd-mono); font-size:11.5px; }
.rdm-frow { display:flex; align-items:center; gap:8px; }
.rdm-acts { display:flex; align-items:center; gap:10px; padding:11px 15px;
            border-top:1px solid var(--rd-line); flex-wrap:wrap; }
.rdm-acts .rdm-spacer { flex:1; }
/* the one filled button; color=None on the ui.button keeps Quasar's
   bg-primary off so this rule is the only thing that paints it */
.q-btn.rdm-save { background:var(--rd-watch); color:#fff; }
.q-btn.rdm-save:hover { filter:brightness(1.08); }
.q-btn.rdm-danger { color:var(--rd-live); }
</style>
"""


def _accent(value: object) -> str:
    """A set's accent, or a neutral surface when it is not a plain hex colour."""
    text = str(value or "").strip()
    return text if _HEX.match(text) else "var(--rd-surface-2)"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _money(value: object) -> str:
    try:
        return f"${float(value):,.2f}"   # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "—"


# --------------------------------------------------------------------------- #
# per-store overrides — the shape retailer_config's site_params layer reads
# --------------------------------------------------------------------------- #
# Everything below is optional in the saved block: a key that is absent means
# "inherit the handler's default". The editor therefore never writes an empty
# list, an empty dict or a blank string — `build_site_params` strips them.
_SELECTOR_KEYS = ("atc", "checkout", "place_order", "queue", "confirmation")
_SELECTOR_LABELS = {
    "atc": "Add to cart", "checkout": "Checkout", "place_order": "Place order",
    "queue": "Queue / waiting room", "confirmation": "Confirmation",
}
_WAIT_KEYS = (
    ("atc_enable_s", "ATC enable wait (s)", 30),
    ("queue_max_s", "Queue max (s)", 2700),
    ("confirm_s", "Confirmation wait (s)", 20),
)
# Profile bookkeeping that is never a form field.
_PROFILE_SKIP = {"id", "name", "fingerprint_seed", "user_data_dir", "notes",
                 "created", "updated", "same_as_shipping"}


def _profile_paths(schema: dict | None = None) -> list[str]:
    """Every dotted leaf of the profile schema a field can be filled from —
    `email`, `shipping.zip`, `payment.card_number` … exactly what exists."""
    out: list[str] = []

    def walk(node: dict, prefix: str) -> None:
        for k, v in node.items():
            if k in _PROFILE_SKIP:
                continue
            path = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                walk(v, path)
            elif isinstance(v, str):
                out.append(path)

    walk(schema if schema is not None else _PROFILE_SCHEMA, "")
    return out


_PROFILE_PATHS = _profile_paths()


def _lines(text: object) -> list[str]:
    """One entry per non-blank line, trimmed."""
    return [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]


def _handler_defaults(site_id: str) -> dict:
    """The handler's built-in site_params, or {} when the helper is not in this
    build yet — the editor then simply shows no placeholders."""
    try:
        from raindance.core import retailer_config as rc
    except Exception:                                 # noqa: BLE001
        return {}
    fn = getattr(rc, "default_site_params_for", None)
    if not callable(fn):
        return {}
    try:
        got = fn(site_id)
    except Exception:                                 # noqa: BLE001
        return {}
    return got if isinstance(got, dict) else {}


def _handler_caps(site_id: str) -> dict:
    """What this store's handler implements, or {} when unknown."""
    try:
        from raindance import sites
    except Exception:                                 # noqa: BLE001
        return {}
    fn = getattr(sites, "handler_capabilities", None)
    if not callable(fn):
        return {}
    try:
        got = fn(site_id)
    except Exception:                                 # noqa: BLE001
        return {}
    return got if isinstance(got, dict) else {}


def _caps_text(caps: dict) -> str:
    """`caps` as one readable line. Truthy flags are listed by name, lists by
    name and count, strings as name=value; nothing known reads as generic."""
    bits: list[str] = []
    for k, v in caps.items():
        if v is True:
            bits.append(str(k))
        elif isinstance(v, (list, tuple, set, dict)):
            if v:
                bits.append(f"{k} ({len(v)})")
        elif isinstance(v, str) and v.strip():
            bits.append(f"{k}={v.strip()}")
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            bits.append(f"{k}={v}")
    return ", ".join(bits) if bits else "generic"


def build_site_params(form: dict) -> tuple[dict, str]:
    """Turn the editor's raw values into the saved block.

    `form` carries: `selectors` {key: text}, `fields` [{css, path}],
    `fields_only` bool, `frames` text, `waits` {key: number|None},
    `order_id_regex` str, `success_text` text.

    Returns (params, error). `params` holds only the keys the user actually
    filled — empties are stripped so absent still means inherit — and it is
    {} when nothing was filled. A non-empty `error` means nothing should be
    written.
    """
    out: dict = {}

    selectors: dict = {}
    for key in _SELECTOR_KEYS:
        raw = (form.get("selectors") or {}).get(key, "")
        rows = _lines(raw)
        if rows:
            selectors[key] = rows
    if selectors:
        out["selectors"] = selectors

    fields: dict = {}
    for i, row in enumerate(form.get("fields") or [], 1):
        css = str((row or {}).get("css") or "").strip()
        path = str((row or {}).get("path") or "").strip()
        if not css and not path:
            continue                                  # an untouched blank row
        if not css:
            return {}, f"Field {i}: the CSS selector is empty"
        if not path:
            return {}, f"Field {i} ({css}): pick a profile path"
        if path not in _PROFILE_PATHS:
            return {}, f"Field {i} ({css}): {path!r} is not a profile path"
        if css in fields:
            return {}, f"Field {i}: {css!r} is listed twice"
        fields[css] = path
    if fields:
        out["fields"] = fields
    if form.get("fields_only"):
        out["fields_only"] = True

    frames = _lines(form.get("frames"))
    if frames:
        out["frames"] = frames

    waits: dict = {}
    for key, label, _default in _WAIT_KEYS:
        raw = (form.get("waits") or {}).get(key)
        if raw in (None, ""):
            continue
        try:
            val = float(raw)
        except (TypeError, ValueError):
            return {}, f"{label}: {raw!r} is not a number"
        if val < 0:
            return {}, f"{label}: cannot be negative"
        waits[key] = int(val) if val.is_integer() else val
    if waits:
        out["waits"] = waits

    conf: dict = {}
    rx = str(form.get("order_id_regex") or "").strip()
    if rx:
        try:
            re.compile(rx)
        except re.error as exc:
            return {}, f"Order-id regex does not compile: {exc}"
        conf["order_id_regex"] = rx
    success = _lines(form.get("success_text"))
    if success:
        conf["success_text"] = success
    if conf:
        out["confirmation"] = conf

    return out, ""


@register_tool
class MonitorsTabTool(ToolPlugin):
    id = "monitors_tab"
    name = "Monitors"
    icon = "radar"
    order = 0
    description = "Pick the set, the product line, then the listings to run."

    def render(self, ctx) -> None:
        theme.inject()
        ui.add_head_html(_CSS)

        catalog = getattr(ctx, "catalog", None)
        store = getattr(ctx, "store", None)
        queue = getattr(ctx, "queue", None)

        theme.title("Catalog")
        theme.lede("The sets, product lines, stores and listings the wizard chooses "
                   "from. Loading listings into a run happens in Start.")

        if catalog is None or store is None:
            theme.note("The catalog is not available in this context — this tab needs "
                       "<code>ctx.catalog</code> and <code>ctx.store</code>. Nothing "
                       "to select yet.")
            return

        # Where we are in the drill-down.
        view: dict = {"set": None, "line": None}

        # ------------------------------------------------------------------ #
        # navigation
        # ------------------------------------------------------------------ #
        def _go_sets() -> None:
            view["set"] = None
            view["line"] = None
            body.refresh()

        def _go_set(set_id: str) -> None:
            view["set"] = set_id
            view["line"] = None
            body.refresh()

        def _go_line(line_id: str) -> None:
            view["line"] = line_id
            body.refresh()

        def _crumbs() -> None:
            with ui.element("div").classes("rd-crumbs"):
                if view["set"] is None:
                    ui.label("All sets").classes("rd-crumb-now")
                    return
                ui.label("All sets").classes("rd-crumb").on("click", lambda: _go_sets())
                ui.label("/").classes("rd-crumb-sep")
                set_name = catalog.name_of("set", view["set"])
                if view["line"] is None:
                    ui.label(set_name).classes("rd-crumb-now")
                    return
                ui.label(set_name).classes("rd-crumb").on(
                    "click", lambda sid=view["set"]: _go_set(sid))
                ui.label("/").classes("rd-crumb-sep")
                ui.label(catalog.name_of("line", view["line"])).classes("rd-crumb-now")

        # ------------------------------------------------------------------ #
        # adding a set — from the public set database, or just by name
        # ------------------------------------------------------------------ #
        def _lookup_ready() -> bool:
            """True when this build can actually look a set up on the internet."""
            return tcg_api is not None and callable(
                getattr(tcg_api, "search_sets", None))

        def _add_set_safely(name: str, **art):
            """Create a set, passing only the artwork keywords THIS build's
            `add_set` accepts.

            The catalog's signature grows over time (image, symbol, era,
            released, code) and this tab has to work against whichever copy is
            installed — the same filtering app.py does for BrowserFactory. On an
            older add_set the extra keywords are simply dropped and the set is
            still created, with its code badge instead of a logo.
            """
            try:
                accepts = set(inspect.signature(catalog.add_set).parameters)
            except (TypeError, ValueError):       # an opaque or C-level __init__
                accepts = set()
            use = {k: v for k, v in art.items()
                   if v not in (None, "") and (not accepts or k in accepts)}
            try:
                return catalog.add_set(name, **use)
            except TypeError:                     # signature said yes, call said no
                return catalog.add_set(name)

        def _create_set(name: str, **art) -> bool:
            name = str(name or "").strip()
            if not name:
                ui.notify("Name the set first", type="warning")
                return False
            try:
                rec = _add_set_safely(name, **art)
            except Exception as exc:              # noqa: BLE001
                ui.notify(f"Could not add the set: {exc}", type="negative")
                return False
            has_art = bool(str((rec or {}).get("image") or "").strip())
            ui.notify(f"Added {name}" + ("" if has_art else
                                         " — no artwork yet, try Fetch artwork"),
                      type="positive")
            body.refresh()
            return True

        # What the last lookup produced. `searched` separates "you have not
        # looked yet" from "nothing came back", which are different messages.
        find = {"busy": False, "rows": [], "msg": "", "searched": False}

        with ui.dialog() as add_dlg, theme.card("rdm-dlg"):
            with ui.element("div").classes("rd-sechead"):
                theme.lab("Add a set")
                ui.button(icon="close", on_click=lambda: add_dlg.close()) \
                    .props("flat dense round size=sm")
            with ui.element("div").classes("rd-pad"):
                ui.label("Look the set up in the public Pokémon TCG set database "
                         "to get its logo, series and release date — or type a "
                         "name and add it by hand, which needs no network at "
                         "all.").classes("rd-pmeta")

            def _paint_busy(busy: bool) -> None:
                """Spinner + disabled input, so one lookup cannot be fired twice."""
                find["busy"] = busy
                try:
                    q_in.set_enabled(not busy)
                    go_btn.set_enabled(not busy and _lookup_ready())
                    if busy:
                        go_btn.props("loading")
                    else:
                        go_btn.props(remove="loading")
                except Exception:                 # noqa: BLE001 - cosmetic only
                    pass

            async def _search() -> None:
                if find["busy"]:
                    return
                q = str(q_in.value or "").strip()
                if not q:
                    ui.notify("Type part of the set name first", type="warning")
                    return
                if not _lookup_ready():
                    find.update(rows=[], searched=True, msg=(
                        "This build has no set lookup — the set database module "
                        "is not installed. Add the set by name below."))
                    hits.refresh()
                    return
                _paint_busy(True)
                hits.refresh()                    # paint the in-flight state
                try:
                    # Seconds of network I/O — off the UI thread, exactly the way
                    # the wizard's link discovery does it, or the page hangs.
                    found = await run.io_bound(tcg_api.search_sets, q)
                except Exception as exc:          # noqa: BLE001
                    find.update(rows=[], searched=True, msg=(
                        f"Couldn't reach the set database ({exc}). Check the "
                        f"connection, or add \u201c{q}\u201d by name below."))
                else:
                    rows = [r for r in (found or []) if isinstance(r, dict)]
                    find.update(rows=rows, searched=True, msg=("" if rows else (
                        f"No set matched \u201c{q}\u201d. Try fewer words — and "
                        "if this machine is offline the set database cannot be "
                        "reached at all, so add the set by name below.")))
                _paint_busy(False)
                hits.refresh()

            with ui.element("div").classes("rd-form"):
                q_in = ui.input("Search the set database",
                                placeholder="prismatic evolutions") \
                    .props("dense outlined").classes("grow").style("min-width:230px")
                # Fires on Enter or on the button — never once per keystroke.
                q_in.on("keydown.enter", _search)
                go_btn = ui.button("Search", icon="travel_explore", on_click=_search) \
                    .props("outline no-caps dense")
            if not _lookup_ready():
                go_btn.set_enabled(False)
                go_btn.tooltip("This build has no set database — add by name below.")

            def _use_hit(rec: dict) -> None:
                if _create_set(str(rec.get("name") or ""),
                               image=str(rec.get("image") or ""),
                               symbol=str(rec.get("symbol") or ""),
                               code=str(rec.get("code") or "")[:6],
                               released=str(rec.get("released") or ""),
                               era=str(rec.get("series") or "")):
                    add_dlg.close()

            def _hit_row(rec: dict) -> None:
                name = str(rec.get("name") or "").strip() or "(unnamed set)"
                code = (str(rec.get("code") or "")[:4].upper() or "?")
                bits = [b for b in (str(rec.get("series") or "").strip(),
                                    str(rec.get("released") or "").strip()) if b]
                with ui.element("div").classes("rdm-hit").on(
                        "click", lambda _=None, r=rec: _use_hit(r)):
                    theme.thumb(rec.get("image") or rec.get("symbol") or "",
                                code, classes="rdm-logo")
                    with ui.column().classes("gap-0 grow min-w-0"):
                        ui.label(name).classes("rd-pname")
                        ui.label(" · ".join(bits) or "—").classes("rd-pmeta")
                    ui.label(code).classes("rd-chip rd-plat")

            @ui.refreshable
            def hits() -> None:
                if find["busy"]:
                    with ui.element("div").classes("rdm-msg"):
                        with ui.row().classes("items-center gap-3"):
                            ui.spinner(size="sm")
                            ui.label("Reading the set database…")
                    return
                if find["msg"]:
                    ui.label(find["msg"]).classes("rdm-msg")
                    return
                if not find["rows"]:
                    if not find["searched"]:
                        ui.label("Search above, or add a set by name below.") \
                            .classes("rdm-msg")
                    return
                with ui.element("div").classes("rdm-hits"):
                    for rec in find["rows"]:
                        _hit_row(rec)

            hits()
            ui.label("or add it by hand").classes("rdm-sep")

            def _add_manual() -> None:
                if _create_set(man_in.value or "",
                               code=str(man_code.value or "").strip()[:6]):
                    man_in.set_value("")
                    man_code.set_value("")
                    add_dlg.close()

            with ui.element("div").classes("rd-form"):
                man_in = ui.input("Set name", placeholder="Prismatic Evolutions") \
                    .props("dense outlined").classes("grow").style("min-width:210px")
                man_code = ui.input("Code", placeholder="PRE") \
                    .props("dense outlined").style("width:104px") \
                    .tooltip("The short code the tile shows when there is no logo")
                ui.button("Add by name", icon="add", on_click=_add_manual) \
                    .props("outline no-caps dense")

        def _open_add_set() -> None:
            find.update(rows=[], msg="", searched=False, busy=False)
            try:
                hits.refresh()
                _paint_busy(False)
            except Exception:                     # noqa: BLE001 - cosmetic only
                pass
            add_dlg.open()

        # ------------------------------------------------------------------ #
        # artwork backfill — fill in the logos the catalog is missing
        # ------------------------------------------------------------------ #
        art = {"busy": False}

        async def _fetch_art(e=None) -> None:
            fn = getattr(catalog, "backfill_images", None)
            if not callable(fn):
                ui.notify("This build cannot fetch artwork yet — the catalog has "
                          "no backfill_images(). Sets keep their code badge, "
                          "which is exactly what they show today.",
                          type="warning", multi_line=True)
                return
            if art["busy"]:
                return
            btn = getattr(e, "sender", None)
            art["busy"] = True

            def _paint(busy: bool) -> None:
                try:
                    if btn is None:
                        return
                    btn.set_enabled(not busy)
                    if busy:
                        btn.props("loading")
                    else:
                        btn.props(remove="loading")
                except Exception:                 # noqa: BLE001 - cosmetic only
                    pass

            _paint(True)
            filled, failed = 0, ""
            try:
                # Network I/O again — same rule, same reason.
                filled = int(await run.io_bound(fn) or 0)
            except Exception as exc:              # noqa: BLE001
                failed = str(exc)
            art["busy"] = False
            _paint(False)
            if failed:
                ui.notify(f"Couldn't reach the set database — no artwork fetched "
                          f"({failed}). Every set keeps its code badge.",
                          type="warning", multi_line=True)
                return
            if not filled:
                ui.notify("No artwork fetched. Either every set already has a "
                          "logo, or the set database could not be reached.",
                          type="warning", multi_line=True)
                return
            ui.notify(f"Fetched artwork for {_plural(filled, 'set')}",
                      type="positive")
            body.refresh()

        # ------------------------------------------------------------------ #
        # view 1 — the set grid
        # ------------------------------------------------------------------ #
        def _sets_view() -> None:
            sets = catalog.sets()
            with ui.element("div").classes("rd-sechead"):
                theme.lab("Sets")
                with ui.row().classes("items-center gap-2"):
                    ui.button("Add set", icon="add", on_click=_open_add_set) \
                        .props("outline no-caps dense") \
                        .tooltip("Look a set up on the internet, or add one by name")
                    ui.button("Fetch artwork", icon="image_search",
                              on_click=_fetch_art) \
                        .props("outline no-caps dense") \
                        .tooltip("Fills in the set logos the catalog is missing. "
                                 "Needs a connection — offline it fetches nothing "
                                 "and says so.")
            if not sets:
                with ui.element("div").classes("rd-pad"):
                    theme.note("No sets yet. A set is the drop itself — the thing "
                               "that goes live at a known minute. <b>Add set</b> "
                               "looks one up by name, or adds it by hand.")
                return
            with ui.element("div").classes("rd-setgrid"):
                for s in sets:
                    _set_card(s)

        def _set_card(s: dict) -> None:
            try:
                counts = catalog.counts_for_set(store, s["id"])
            except Exception:                             # noqa: BLE001
                counts = {"listings": 0, "lines": 0, "stores": 0}
            accent = _accent(s.get("accent"))
            # `--rd-thumb-bg` is the ground the logo letterboxes onto, so the
            # bars beside a wide logo are the tile's own accent rather than a
            # grey slab pasted over it.
            with ui.element("div").classes("rd-setcard").on(
                    "click", lambda sid=s["id"]: _go_set(sid)):
                theme.thumb(theme.set_art(catalog, s["id"]),
                            str(s.get("code") or "?").upper(),
                            classes="rd-tile", text_class="rd-tilecode",
                            style=f"background:color-mix(in srgb,{accent} 26%,"
                                  f"var(--rd-surface-2));"
                                  f"border:1px solid color-mix(in srgb,{accent} 55%,"
                                  f"transparent);"
                                  f"--rd-thumb-bg:color-mix(in srgb,{accent} 26%,"
                                  f"var(--rd-surface-2))")
                ui.label(s.get("name") or s["id"]).classes("rd-pname")
                ui.label(f"{_plural(counts['listings'], 'listing')} · "
                         f"{_plural(counts['lines'], 'line')} · "
                         f"{_plural(counts['stores'], 'store')}").classes("rd-pmeta")

        # ------------------------------------------------------------------ #
        # view 2 — product lines within a set
        # ------------------------------------------------------------------ #
        def _lines_view(set_id: str) -> None:
            lines = catalog.lines()
            with ui.element("div").classes("rd-sechead"):
                theme.lab("Product lines")
                ui.label(catalog.name_of("set", set_id)).classes("rd-lab")
            if not lines:
                theme.note("No product lines yet — an ETB, a booster box, a bundle.")
                return
            # Every line is shown, not just the ones already carrying listings:
            # an empty line is exactly where you go to add the first one.
            for line in lines:
                n = len(catalog.listings(store, set_id=set_id, line_id=line["id"]))
                classes = "rd-lnrow" + ("" if n else " rd-vacant")
                with ui.element("div").classes(classes).on(
                        "click", lambda lid=line["id"]: _go_line(lid)):
                    theme.thumb(theme.product_art(catalog, set_id, line["id"]),
                                str(line.get("short") or "?")[:3].upper(),
                                size="28px", classes="rd-thumb-in")
                    ui.label(str(line.get("short") or "—")).classes("rd-short")
                    with ui.column().classes("gap-0 grow min-w-0"):
                        ui.label(line.get("name") or line["id"]).classes("rd-pname")
                        ui.label(f"MSRP {_money(line.get('msrp'))}").classes("rd-pmeta")
                    if n:
                        ui.label(_plural(n, "listing")).classes("rd-chip rd-watch")
                    else:
                        ui.label("No listings").classes("rd-chip rd-off")

        # ------------------------------------------------------------------ #
        # view 3 — the listings for one (set, line)
        # ------------------------------------------------------------------ #
        def _listings_view(set_id: str, line_id: str) -> None:
            rows = catalog.listings(store, set_id=set_id, line_id=line_id)
            with ui.element("div").classes("rd-sechead"):
                theme.lab("Listings · one per store")
                ui.label(f"{catalog.name_of('set', set_id)} · "
                         f"{catalog.name_of('line', line_id)}").classes("rd-lab")

            if not rows:
                with ui.element("div").classes("rd-pad"):
                    theme.note("No listings for this line yet. Add the product URL "
                               "from each store below — one row per store.")
            for p in rows:
                _listing_row(p)
            _add_listing_form(set_id, line_id)

        def _listing_row(p: dict) -> None:
            pid = p.get("id", "")
            st = catalog.get_store(p.get("store_id") or "") or {}
            store_name = st.get("name") or p.get("store_name") or "Unassigned store"
            platform = str(st.get("platform") or "custom")
            queued = bool(queue.has(pid)) if queue is not None else False
            with ui.element("div").classes("rd-lsrow"):
                theme.thumb(theme.product_art(catalog, p.get("set_id") or "",
                                              p.get("line_id") or ""),
                            str(store_name or "?")[:2].upper(),
                            size="24px", classes="rd-thumb-in")
                with ui.column().classes("gap-0 grow min-w-0"):
                    with ui.row().classes("items-center gap-2 no-wrap"):
                        ui.label(store_name).classes("rd-pname")
                        ui.label(platform).classes("rd-chip rd-plat")
                    ui.label(p.get("url") or "—").classes("rd-url")
                if queued:
                    theme.chip("queued")
                else:
                    ui.label("Not queued").classes("rd-chip rd-off")

        def _add_listing_form(set_id: str, line_id: str) -> None:
            shops = catalog.stores()
            if not shops:
                with ui.element("div").classes("rd-pad"):
                    theme.note("Add a store first — open <b>Stores</b> below. A listing "
                               "is a URL on a store you own, so the store has to exist "
                               "before the listing can point at it.")
                return

            def _add() -> None:
                url = (url_in.value or "").strip()
                if not url:
                    ui.notify("Paste the product URL first", type="warning")
                    return
                if not store_sel.value:
                    ui.notify("Pick a store", type="warning")
                    return
                raw = price_in.value
                try:
                    threshold = float(raw) if raw not in (None, "") else None
                except (TypeError, ValueError):
                    threshold = None
                try:
                    catalog.add_listing(store, set_id=set_id, line_id=line_id,
                                        store_id=store_sel.value, url=url,
                                        frequency=freq_sel.value or "5s",
                                        price_threshold=threshold)
                except Exception as exc:                  # noqa: BLE001
                    ui.notify(f"Could not add the listing: {exc}", type="negative")
                    return
                ui.notify("Listing added", type="positive")
                body.refresh()

            with ui.element("div").classes("rd-form"):
                store_sel = ui.select({s["id"]: s.get("name") or s["id"] for s in shops},
                                      value=shops[0]["id"], label="Store") \
                    .props("dense outlined options-dense").style("min-width:170px")
                url_in = ui.input("Product URL", placeholder="https://…") \
                    .props("dense outlined").classes("grow").style("min-width:230px")
                price_in = ui.number("Price cap", placeholder="optional",
                                     format="%.2f") \
                    .props("dense outlined").style("width:124px") \
                    .tooltip("Skip anything above this. Blank falls back to the "
                             "line's MSRP.")
                freq_sel = ui.select(_FREQS, value="5s", label="Check every") \
                    .props("dense outlined options-dense").style("width:118px")
                ui.button("Add listing", icon="add", on_click=_add) \
                    .props("outline no-caps dense")

        # ------------------------------------------------------------------ #
        # the drill-down body
        # ------------------------------------------------------------------ #
        with ui.column().classes("w-full gap-4"):
            @ui.refreshable
            def body() -> None:
                _crumbs()
                with theme.card():
                    if view["set"] is None:
                        _sets_view()
                    elif view["line"] is None:
                        _lines_view(view["set"])
                    else:
                        _listings_view(view["set"], view["line"])

            body()

            # -------------------------------------------------------------- #
            # stores — the other half of a listing
            # -------------------------------------------------------------- #
            with ui.expansion("Stores", icon="storefront").classes("w-full rd-card"):

                def _settings():
                    return getattr(ctx, "settings", None) or \
                        getattr(catalog, "settings", None)

                def _register_handlers() -> None:
                    """Re-register the user store handlers, the way Add store
                    does, so a running app picks the change up."""
                    settings = _settings()
                    if settings is None:
                        return
                    try:
                        from raindance.sites.user_store import register_user_stores
                        register_user_stores(settings, bus=getattr(ctx, "bus", None))
                    except Exception as exc:              # noqa: BLE001
                        ui.notify(f"Saved, but the handler did not re-register: "
                                  f"{exc}", type="warning")

                def _store_rc(sid: str) -> dict:
                    settings = _settings()
                    if settings is None:
                        return {}
                    rc = settings.data.get("retailer_configs") or {}
                    return rc.get(sid) or {}

                def _has_overrides(sid: str) -> bool:
                    return bool(_store_rc(sid).get("site_params"))

                # One dialog, rebuilt per store on open, so rows never pile up.
                cust_dlg = ui.dialog()

                def _open_customize(st: dict) -> None:
                    sid = st["id"]
                    name = st.get("name") or sid
                    current = _store_rc(sid).get("site_params") or {}
                    if not isinstance(current, dict):
                        current = {}
                    defaults = _handler_defaults(sid)
                    caps = _handler_caps(sid)

                    # The editor's live values. Widgets write into this; Save
                    # reads it, so a refresh of the fields list loses nothing.
                    form: dict = {
                        "selectors": {k: "\n".join(
                            str(x) for x in ((current.get("selectors") or {}).get(k) or []))
                            for k in _SELECTOR_KEYS},
                        "fields": [{"css": k, "path": v} for k, v in
                                   (current.get("fields") or {}).items()],
                        "fields_only": bool(current.get("fields_only")),
                        "frames": "\n".join(str(x) for x in (current.get("frames") or [])),
                        "waits": {k: (current.get("waits") or {}).get(k)
                                  for k, _l, _d in _WAIT_KEYS},
                        "order_id_regex": str(
                            (current.get("confirmation") or {}).get("order_id_regex") or ""),
                        "success_text": "\n".join(str(x) for x in (
                            (current.get("confirmation") or {}).get("success_text") or [])),
                    }

                    def _set(section: str, key: str):
                        def _on(e) -> None:
                            form[section][key] = e.value
                        return _on

                    def _set_top(key: str):
                        def _on(e) -> None:
                            form[key] = e.value
                        return _on

                    def _save() -> None:
                        params, err = build_site_params(form)
                        if err:
                            ui.notify(err, type="negative", multi_line=True)
                            return
                        settings = _settings()
                        if settings is None:
                            ui.notify("No settings in this context — nothing saved",
                                      type="negative")
                            return
                        rc = settings.data.setdefault("retailer_configs", {})
                        blk = rc.setdefault(sid, {})
                        if params:
                            blk["site_params"] = params
                        else:
                            blk.pop("site_params", None)
                        try:
                            settings.save()
                        except Exception as exc:          # noqa: BLE001
                            ui.notify(f"Could not save: {exc}", type="negative")
                            return
                        _register_handlers()
                        ui.notify(f"{name}: overrides saved" if params else
                                  f"{name}: nothing overridden — inherits the "
                                  f"handler's defaults", type="positive")
                        cust_dlg.close()
                        stores_body.refresh()

                    def _reset() -> None:
                        settings = _settings()
                        if settings is None:
                            ui.notify("No settings in this context — nothing reset",
                                      type="negative")
                            return
                        rc = settings.data.get("retailer_configs") or {}
                        blk = rc.get(sid)
                        if isinstance(blk, dict):
                            blk.pop("site_params", None)
                        try:
                            settings.save()
                        except Exception as exc:          # noqa: BLE001
                            ui.notify(f"Could not save: {exc}", type="negative")
                            return
                        _register_handlers()
                        ui.notify(f"{name}: back to the handler's defaults",
                                  type="info")
                        confirm.close()
                        cust_dlg.close()
                        stores_body.refresh()

                    cust_dlg.clear()
                    with cust_dlg, theme.card("rdm-dlg rdm-cust"):
                        with ui.element("div").classes("rd-sechead"):
                            with ui.column().classes("gap-0"):
                                theme.lab(f"Customize · {name}")
                                ui.label("Empty means inherit the handler's default — "
                                         "the greyed text shows what that is.") \
                                    .classes("rd-pmeta")
                            ui.button(icon="close", on_click=cust_dlg.close) \
                                .props("flat dense round size=sm")
                        with ui.element("div").classes("rdm-caps"):
                            ui.html("<b>handler</b>", sanitize=False)
                            ui.label(_caps_text(caps))

                        with ui.element("div").classes("rdm-cust-body"):
                            # -- selectors ---------------------------------- #
                            with ui.element("div").classes("rdm-sect"):
                                with ui.element("div").classes("rdm-sect-h"):
                                    theme.lab("Selectors")
                                    ui.label("one CSS selector per line, tried in "
                                             "order").classes("rdm-hint")
                                dsel = defaults.get("selectors") or {}
                                with ui.element("div").classes("rdm-grid"):
                                    for key in _SELECTOR_KEYS:
                                        ph = "\n".join(str(x) for x in (dsel.get(key) or []))
                                        ui.textarea(_SELECTOR_LABELS[key],
                                                    value=form["selectors"][key],
                                                    placeholder=ph or "inherit",
                                                    on_change=_set("selectors", key)) \
                                            .props("dense outlined autogrow input-style=\"min-height:56px\"")

                            # -- fields ------------------------------------- #
                            with ui.element("div").classes("rdm-sect"):
                                with ui.element("div").classes("rdm-sect-h"):
                                    theme.lab("Fields")
                                    ui.label("which profile value fills which "
                                             "input").classes("rdm-hint")

                                @ui.refreshable
                                def field_rows() -> None:
                                    if not form["fields"]:
                                        dflds = defaults.get("fields") or {}
                                        ui.label(("Inheriting " + ", ".join(
                                            f"{k} ← {v}" for k, v in dflds.items()))
                                            if dflds else
                                            "No field overrides — the handler fills "
                                            "its own form.").classes("rdm-hint")
                                    for row in form["fields"]:
                                        with ui.element("div").classes("rdm-frow"):
                                            ui.input("CSS selector", value=row["css"],
                                                     placeholder="#email",
                                                     on_change=lambda e, r=row:
                                                     r.__setitem__("css", e.value)) \
                                                .props("dense outlined").classes("grow")
                                            ui.select(_PROFILE_PATHS, label="Profile path",
                                                      value=(row["path"] if row["path"]
                                                             in _PROFILE_PATHS else None),
                                                      on_change=lambda e, r=row:
                                                      r.__setitem__("path", e.value)) \
                                                .props("dense outlined options-dense") \
                                                .style("min-width:190px")
                                            ui.button(icon="close",
                                                      on_click=lambda _=None, r=row:
                                                      (form["fields"].remove(r),
                                                       field_rows.refresh())) \
                                                .props("flat dense round size=sm")

                                field_rows()
                                with ui.row().classes("items-center gap-4"):
                                    ui.button("Add field", icon="add",
                                              on_click=lambda: (
                                                  form["fields"].append(
                                                      {"css": "", "path": ""}),
                                                  field_rows.refresh())) \
                                        .props("outline no-caps dense")
                                    ui.switch("Fields only", value=form["fields_only"],
                                              on_change=_set_top("fields_only")) \
                                        .tooltip("Fill only these fields; skip the "
                                                 "handler's own form filling")

                            # -- frames ------------------------------------- #
                            with ui.element("div").classes("rdm-sect"):
                                with ui.element("div").classes("rdm-sect-h"):
                                    theme.lab("Frames")
                                    ui.label("iframe name or URL substring, one per "
                                             "line").classes("rdm-hint")
                                dfr = "\n".join(str(x) for x in (defaults.get("frames") or []))
                                ui.textarea(value=form["frames"],
                                            placeholder=dfr or "inherit",
                                            on_change=_set_top("frames")) \
                                    .props("dense outlined autogrow").classes("w-full")

                            # -- waits -------------------------------------- #
                            with ui.element("div").classes("rdm-sect"):
                                with ui.element("div").classes("rdm-sect-h"):
                                    theme.lab("Waits")
                                    ui.label("seconds").classes("rdm-hint")
                                dw = defaults.get("waits") or {}
                                with ui.row().classes("items-end gap-3"):
                                    for key, label, fallback in _WAIT_KEYS:
                                        ph = dw.get(key, fallback)
                                        ui.number(label, value=form["waits"][key],
                                                  placeholder=str(ph), min=0,
                                                  on_change=_set("waits", key)) \
                                            .props("dense outlined").style("width:170px")

                            # -- confirmation ------------------------------- #
                            with ui.element("div").classes("rdm-sect"):
                                with ui.element("div").classes("rdm-sect-h"):
                                    theme.lab("Confirmation")
                                    ui.label("how an order is recognised").classes("rdm-hint")
                                dc = defaults.get("confirmation") or {}
                                ui.input("Order-id regex", value=form["order_id_regex"],
                                         placeholder=str(dc.get("order_id_regex") or
                                                         r"order\s*#?\s*([A-Z0-9-]+)"),
                                         on_change=_set_top("order_id_regex")) \
                                    .props("dense outlined").classes("w-full")
                                dst = "\n".join(str(x) for x in (dc.get("success_text") or []))
                                ui.textarea("Success text", value=form["success_text"],
                                            placeholder=dst or "one phrase per line",
                                            on_change=_set_top("success_text")) \
                                    .props("dense outlined autogrow").classes("w-full")

                        with ui.dialog() as confirm, theme.card("rdm-dlg"):
                            with ui.element("div").classes("rd-pad"):
                                theme.lab("Reset to handler defaults?")
                                ui.label(f"Every override on {name} is removed. The "
                                         "store keeps its proxy group, captcha and "
                                         "browser settings.").classes("rd-pmeta")
                            with ui.element("div").classes("rdm-acts"):
                                ui.button("Cancel", on_click=confirm.close, color=None) \
                                    .props("outline no-caps dense")
                                ui.element("div").classes("rdm-spacer")
                                ui.button("Reset", icon="restart_alt", on_click=_reset,
                                          color=None).props("outline no-caps dense") \
                                    .classes("rdm-danger")

                        with ui.element("div").classes("rdm-acts"):
                            ui.button("Reset to handler defaults", icon="restart_alt",
                                      on_click=confirm.open, color=None) \
                                .props("flat no-caps dense").classes("rdm-danger") \
                                .tooltip("Remove every override for this store")
                            ui.element("div").classes("rdm-spacer")
                            ui.button("Cancel", on_click=cust_dlg.close, color=None) \
                                .props("outline no-caps dense")
                            ui.button("Save", icon="save", on_click=_save, color=None) \
                                .props("unelevated no-caps dense").classes("rdm-save")
                    cust_dlg.open()

                def _remove_store(sid: str) -> None:
                    catalog.remove("store", sid)
                    ui.notify("Store removed", type="info")
                    stores_body.refresh()
                    body.refresh()

                @ui.refreshable
                def stores_body() -> None:
                    shops = catalog.stores()
                    if not shops:
                        with ui.element("div").classes("rd-pad"):
                            theme.note("No stores yet. Register the storefronts you "
                                       "own — each one gets its own handler and its "
                                       "own operational config.")
                    for s in shops:
                        with ui.element("div").classes("rd-lsrow"):
                            with ui.column().classes("gap-0 grow min-w-0"):
                                with ui.row().classes("items-center gap-2 no-wrap"):
                                    ui.label(s.get("name") or s["id"]).classes("rd-pname")
                                    ui.label(str(s.get("platform") or "custom")) \
                                        .classes("rd-chip rd-plat")
                                ui.label(s.get("base_url") or "—").classes("rd-url")
                            if _has_overrides(s["id"]):
                                ui.label("customized").classes("rd-chip rd-watch") \
                                    .tooltip("This store overrides its handler's "
                                             "selectors, fields or waits")
                            ui.button("Customize", icon="tune",
                                      on_click=lambda st=s: _open_customize(st)) \
                                .props("flat dense no-caps size=sm") \
                                .tooltip("Selectors, form fields, frames and waits "
                                         "for this store's handler")
                            ui.button(icon="close",
                                      on_click=lambda sid=s["id"]: _remove_store(sid)) \
                                .props("flat dense round size=sm") \
                                .tooltip("Remove this store")

                def _add_store() -> None:
                    name = (name_in.value or "").strip()
                    if not name:
                        ui.notify("Name the store first", type="warning")
                        return
                    try:
                        catalog.add_store(name,
                                          platform=plat_sel.value or "custom",
                                          base_url=(base_in.value or "").strip())
                    except Exception as exc:              # noqa: BLE001
                        ui.notify(f"Could not add the store: {exc}", type="negative")
                        return
                    # Register the handler now, so the store is runnable without a
                    # restart — the store id IS the handler id.
                    settings = getattr(ctx, "settings", None)
                    if settings is not None:
                        try:
                            from raindance.sites.user_store import register_user_stores
                            register_user_stores(settings, bus=getattr(ctx, "bus", None))
                        except Exception as exc:          # noqa: BLE001
                            ui.notify(f"Store saved, but its handler did not "
                                      f"register: {exc}", type="warning")
                    ui.notify(f"Added {name} — its handler is registered",
                              type="positive")
                    name_in.set_value("")
                    base_in.set_value("")
                    stores_body.refresh()
                    body.refresh()

                stores_body()
                with ui.element("div").classes("rd-form"):
                    name_in = ui.input("Store name", placeholder="My Card Shop") \
                        .props("dense outlined").style("min-width:170px")
                    plat_sel = ui.select(list(PLATFORMS), value="custom",
                                         label="Platform") \
                        .props("dense outlined options-dense").style("width:140px")
                    base_in = ui.input("Base URL", placeholder="https://shop.example") \
                        .props("dense outlined").classes("grow").style("min-width:210px")
                    ui.button("Add store", icon="add", on_click=_add_store) \
                        .props("outline no-caps dense")

            # -------------------------------------------------------------- #
            # product lines — the other axis of every listing
            # -------------------------------------------------------------- #
            with ui.expansion("Product lines", icon="inventory_2") \
                    .classes("w-full rd-card"):

                def _remove_line(lid: str) -> None:
                    try:
                        catalog.remove("line", lid)
                    except Exception as exc:              # noqa: BLE001
                        ui.notify(f"Could not remove the line: {exc}",
                                  type="negative")
                        return
                    ui.notify("Product line removed", type="info")
                    lines_body.refresh()
                    body.refresh()

                @ui.refreshable
                def lines_body() -> None:
                    rows = catalog.lines()
                    if not rows:
                        with ui.element("div").classes("rd-pad"):
                            theme.note("No product lines yet — an ETB, a booster "
                                       "box, a bundle.")
                    for ln in rows:
                        with ui.element("div").classes("rd-lsrow"):
                            ui.label(str(ln.get("short") or "—")).classes("rd-short")
                            with ui.column().classes("gap-0 grow min-w-0"):
                                ui.label(ln.get("name") or ln["id"]).classes("rd-pname")
                                ui.label(f"MSRP {_money(ln.get('msrp'))}") \
                                    .classes("rd-pmeta")
                            ui.button(icon="close",
                                      on_click=lambda lid=ln["id"]: _remove_line(lid)) \
                                .props("flat dense round size=sm") \
                                .tooltip("Remove this product line from every set")

                def _add_line() -> None:
                    name = (ln_name.value or "").strip()
                    if not name:
                        ui.notify("Name the product line first", type="warning")
                        return
                    raw = ln_msrp.value
                    try:
                        msrp = float(raw) if raw not in (None, "") else None
                    except (TypeError, ValueError):
                        msrp = None
                    try:
                        catalog.add_line(name,
                                         short=(ln_short.value or "").strip(),
                                         msrp=msrp)
                    except Exception as exc:              # noqa: BLE001
                        ui.notify(f"Could not add the product line: {exc}",
                                  type="negative")
                        return
                    ui.notify(f"Added {name} — every set now offers it",
                              type="positive")
                    ln_name.set_value("")
                    ln_short.set_value("")
                    ln_msrp.set_value(None)
                    lines_body.refresh()
                    body.refresh()

                with ui.element("div").classes("rd-pad"):
                    ui.label("A product line is global: every set is crossed with "
                             "every line, so one added here appears on every set "
                             "at once — including in step 1 of Start.") \
                        .classes("rd-pmeta")
                lines_body()
                with ui.element("div").classes("rd-form"):
                    ln_name = ui.input("Line name", placeholder="Elite Trainer Box") \
                        .props("dense outlined").classes("grow") \
                        .style("min-width:190px")
                    ln_short = ui.input("Short code", placeholder="ETB") \
                        .props("dense outlined").style("width:118px") \
                        .tooltip("What the tile and the row show when there is no "
                                 "product image")
                    ln_msrp = ui.number("MSRP", placeholder="59.99", format="%.2f") \
                        .props("dense outlined").style("width:118px") \
                        .tooltip("Publish MSRPs writes this into the pricing table")
                    ui.button("Add product line", icon="add", on_click=_add_line) \
                        .props("outline no-caps dense")

            # -------------------------------------------------------------- #
            # pricing
            # -------------------------------------------------------------- #
            def _sync_msrp() -> None:
                try:
                    n = catalog.sync_msrp_table()
                except Exception as exc:                  # noqa: BLE001
                    ui.notify(f"Could not publish MSRPs: {exc}", type="negative")
                    return
                ui.notify(f"Published {_plural(n, 'MSRP row')} to the pricing table",
                          type="positive")

            with ui.row().classes("items-center gap-3"):
                ui.button("Publish MSRPs", icon="sell", on_click=_sync_msrp) \
                    .props("outline no-caps dense")
                ui.label("Writes each line's MSRP into the pricing table — that is "
                         "what lets a listing on your own store clear the MSRP gate "
                         "instead of stalling at unverified.").classes("rd-pmeta")
