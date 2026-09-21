"""Execute — the drop queue: what runs, how each listing runs, and the arm.

Monitors decides *what* to watch. This page decides *how each watched listing
checks out*. One row is one `ExecuteQueue` entry, and every inline control
writes straight through `queue.update()` — the entry is the only source of
truth, so nothing here holds state a re-render could lose.

START materialises the enabled entries into tasks (replacing only the ones this
queue owns — anything hand-built in the Tasks page survives), reloads the
managers, then arms the orchestrator. Dry run is the resting state; releasing
the `checkout.force_dry_run` gate costs a typed confirmation.

The per-row evasion switch only sets that entry's `evasion` flag, which
materialize() forwards as a site param. The evasion layer itself is untouched
by this file.
"""
from __future__ import annotations

from contextlib import contextmanager

from nicegui import ui

from raindance.core.registry import ToolPlugin, register_tool
from raindance.ui import theme
from raindance.tasks.models import MAX_QUANTITY as _MAX_QTY

ORIGIN = "execute_queue"
_CAPTCHA_MODES = {"manual": "Manual", "api": "API"}
_LOG_LINES = 14
_URL_CHARS = 46

_EXEC_CSS = """
<style>
.rdx-grid { display:grid; grid-template-columns:1.45fr 1fr; gap:16px; align-items:start; }
@media (max-width:1040px){ .rdx-grid { grid-template-columns:1fr; } }

.rdx-head { display:flex; align-items:center; justify-content:space-between; gap:12px;
            padding:13px 15px; border-bottom:1px solid var(--rd-line); }
.rdx-bulk { display:flex; flex-wrap:wrap; align-items:flex-end; gap:11px;
            padding:12px 15px; border-bottom:1px solid var(--rd-line);
            background:color-mix(in srgb,var(--rd-surface-2) 42%,transparent); }
.rdx-field { display:flex; flex-direction:column; gap:2px; }

.rdx-row { display:flex; flex-direction:column; gap:8px; padding:12px 15px;
           border-bottom:1px solid var(--rd-line-soft); }
.rdx-row:last-child { border-bottom:0; }
.rdx-row:hover { background:color-mix(in srgb,var(--rd-surface-2) 45%,transparent); }
.rdx-off { opacity:.45; }
.rdx-top { display:flex; align-items:center; gap:12px; }
.rdx-ctrls { display:flex; flex-wrap:wrap; align-items:flex-end; gap:10px; padding-left:34px; }
.rdx-url { font-family:var(--rd-mono); font-size:11px; color:var(--rd-ink-faint);
           overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:46ch; }

.rdx-arm { display:flex; flex-direction:column; align-items:center; gap:12px;
           padding:22px 16px 18px; }
.rd-disc.rdx-stop {
  background:radial-gradient(circle at 50% 42%,var(--rd-live),var(--rd-live) 70%) !important;
  color:#fff !important;
  box-shadow:0 0 0 1px color-mix(in srgb,var(--rd-live) 40%,transparent),
             0 18px 50px -20px var(--rd-live); }
.rd-disc.rdx-stop .q-btn__content { color:#fff; }
.rdx-disclab { font-family:var(--rd-mono); font-weight:700; letter-spacing:.18em;
               text-transform:uppercase; font-size:13px; line-height:1.5;
               text-align:center; white-space:normal; }
.rdx-state { font-family:var(--rd-mono); font-size:12px; color:var(--rd-ink-dim);
             display:flex; align-items:center; justify-content:center; gap:8px;
             min-height:18px; }
.rdx-dot { width:7px; height:7px; border-radius:50%; background:var(--rd-ok);
           animation:rdx-pl 1.4s ease-in-out infinite; }
@keyframes rdx-pl { 0%,100%{opacity:.35} 50%{opacity:1} }
@media (prefers-reduced-motion: reduce){ .rdx-dot{ animation:none } }

.rdx-board { display:flex; align-items:center; gap:12px; padding:11px 15px;
             border-bottom:1px solid var(--rd-line-soft); }
.rdx-board:last-child { border-bottom:0; }
.rdx-pad { padding:14px 15px; }
</style>
"""


def _short(text: str, limit: int = _URL_CHARS) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


@contextmanager
def _field(label: str, width: str = "9rem"):
    """A labelled control cell — the caption carries the monospace label role."""
    with ui.column().classes("rdx-field").style(f"width:{width}"):
        theme.lab(label)
        yield


@register_tool
class ExecuteTabTool(ToolPlugin):
    id = "execute_tab"
    name = "Execute"
    icon = "bolt"
    order = 1
    description = "The drop queue: per-listing run config, the arm control and a live board."

    def render(self, ctx) -> None:
        theme.inject()
        ui.add_head_html(_EXEC_CSS)

        queue = getattr(ctx, "queue", None)
        store = getattr(ctx, "store", None)
        catalog = getattr(ctx, "catalog", None)
        orch = getattr(ctx, "orchestrator", None)
        settings = getattr(ctx, "settings", None)
        bus = getattr(ctx, "bus", None)
        task_store = getattr(ctx, "task_store", None)
        profile_store = getattr(ctx, "profile_store", None)

        cache = {"running": None, "sig": "", "logpos": 0}
        guard = {"busy": False}

        # ------------------------------------------------------------------ #
        # guarded accessors — a missing service degrades, never raises
        # ------------------------------------------------------------------ #
        def _log(text: str, level: str = "info") -> None:
            try:
                if bus is not None:
                    bus.log(text, level)
            except Exception:
                pass

        def _rows() -> list:
            try:
                return list(queue.rows(store))
            except Exception as e:
                _log(f"execute: queue read failed — {e}", "warn")
                return []

        def _entries_enabled() -> list:
            return [r["entry"] for r in _rows() if r["entry"].get("enabled", True)]

        def _label_of(kind: str, ident: str) -> str:
            if not ident:
                return ""
            if catalog is None:
                return str(ident)
            try:
                return catalog.name_of(kind, ident) or str(ident)
            except Exception:
                return str(ident)

        def _profile_options() -> dict:
            opts = {"": "(default)"}
            try:
                for p in (profile_store.list() if profile_store is not None else []):
                    pid = str(p.get("id") or "")
                    if pid:
                        opts[pid] = p.get("name") or p.get("email") or pid
            except Exception as e:
                _log(f"execute: profile list failed — {e}", "warn")
            return opts

        def _group_options() -> list:
            try:
                names = [str(n) for n in orch.proxy_groups.group_names()]
            except Exception:
                names = []
            return names or ["default"]

        def _as_float(value, fallback: float, low: float) -> float:
            try:
                v = float(value)
            except (TypeError, ValueError):
                return fallback
            return v if v >= low else low

        def _as_int(value, fallback: int, low: int, high: int = 10 ** 6) -> int:
            try:
                v = int(float(value))
            except (TypeError, ValueError):
                return fallback
            return max(low, min(high, v))

        def _gate_dry() -> bool:
            """True while `checkout.force_dry_run` holds every task to a dry run."""
            try:
                return bool((settings.data.get("checkout") or {}).get("force_dry_run", True))
            except Exception:
                return True

        def _is_running() -> bool:
            try:
                return bool(orch.is_running) if orch is not None else False
            except Exception:
                return False

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

        # ------------------------------------------------------------------ #
        # header
        # ------------------------------------------------------------------ #
        ui.label("Execute").classes("text-2xl font-bold")
        ui.label("The queue of listings loaded from Monitors, each with its own run "
                 "config. Arm it here — start builds the tasks, then watches them.") \
            .classes("rd-lede")

        if queue is None or store is None:
            theme.note("<b>The execute queue isn't available.</b> This build has no "
                       "<code>ctx.queue</code> or <code>ctx.store</code>, so there is "
                       "nothing to configure. Everything else on this page depends on "
                       "them, so it stays hidden rather than half-working.")
            return

        # Deleted products must not leave ghost rows behind.
        try:
            gone = int(queue.prune(store) or 0)
            if gone:
                _log(f"execute: pruned {gone} queue entr{'y' if gone == 1 else 'ies'} "
                     "whose product no longer exists", "warn")
        except Exception as e:
            _log(f"execute: prune failed — {e}", "warn")

        # ------------------------------------------------------------------ #
        # one queue row
        # ------------------------------------------------------------------ #
        def _render_row(row: dict, profiles: dict, groups: list) -> None:
            e, p = row["entry"], row["product"]
            pid = e.get("product_id") or p.get("id") or ""
            bits = [b for b in (_label_of("set", p.get("set_id", "")),
                                _label_of("line", p.get("line_id", "")),
                                _label_of("store", p.get("store_id", ""))) if b]
            url = str(p.get("url") or "")
            on = bool(e.get("enabled", True))

            holder = ui.element("div").classes("rdx-row" + ("" if on else " rdx-off"))
            with holder:
                with ui.element("div").classes("rdx-top"):
                    def _toggle(ev, _pid=pid, _h=holder) -> None:
                        state = bool(ev.value)
                        _set(_pid, enabled=state)
                        _h.classes(replace="rdx-row" + ("" if state else " rdx-off"))
                        _paint_mode()

                    ui.checkbox(value=on, on_change=_toggle)
                    with ui.column().classes("gap-0 grow min-w-0"):
                        ui.label(p.get("name") or url or pid).classes("rd-pname")
                        if bits:
                            ui.label(" · ".join(bits)).classes("rd-pmeta")
                        if url:
                            ui.label(_short(url)).classes("rdx-url").tooltip(url)
                    ui.button(icon="close", on_click=lambda _pid=pid: _remove(_pid)) \
                        .props("flat dense round size=sm").tooltip("Remove from the queue")

                with ui.element("div").classes("rdx-ctrls"):
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
                        ui.switch(value=bool(e.get("evasion", True)),
                                  on_change=lambda ev, _pid=pid:
                                      _set(_pid, evasion=bool(ev.value))) \
                            .props("dense").tooltip("Sets this listing's evasion flag")

                    with _field("Poll (s)", "6.5rem"):
                        ui.number(value=_as_float(e.get("poll_seconds"), 5.0, 0.5),
                                  min=0.5, step=0.5, format="%.1f",
                                  on_change=lambda ev, _pid=pid:
                                      _set(_pid, poll_seconds=_as_float(ev.value, 5.0, 0.5))) \
                            .props("outlined dense").classes("w-full")

                    with _field("Qty", "5.5rem"):
                        ui.number(value=_as_int(e.get("quantity"), 1, 1, _MAX_QTY),
                                  min=1, max=_MAX_QTY, step=1,
                                  on_change=lambda ev, _pid=pid:
                                      _set(_pid, quantity=_as_int(ev.value, 1, 1, _MAX_QTY))) \
                            .props("outlined dense").classes("w-full")

                    with _field("Dry run", "5rem"):
                        ui.switch(value=bool(e.get("dry_run", True)),
                                  on_change=lambda ev, _pid=pid: (
                                      _set(_pid, dry_run=bool(ev.value)), _paint_mode())) \
                            .props("dense").tooltip("Off = this listing may place a real order")

        @ui.refreshable
        def queue_body() -> None:
            rows = _rows()
            if not rows:
                with ui.element("div").classes("rdx-pad"):
                    theme.note("<b>The queue is empty.</b> Load listings from the "
                               "<b>Monitors</b> tab — pick the set, line and store you "
                               "want and send them here. Each one arrives with its own "
                               "profile, proxy group, cadence and dry-run switch.")
                return
            profiles = _profile_options()
            groups = _group_options()
            for row in rows:
                _render_row(row, profiles, groups)

        # ------------------------------------------------------------------ #
        # dialogs
        # ------------------------------------------------------------------ #
        with ui.dialog() as live_dialog, ui.card().classes("w-[26rem] max-w-full"):
            ui.label("Go live?").classes("text-lg font-bold").style("color:var(--rd-live)")
            ui.label("Releasing the gate lets any queued listing whose own dry-run "
                     "switch is off place a REAL order and charge the card on its "
                     "profile.").classes("rd-lede")
            typed_i = ui.input(placeholder="type LIVE").props("outlined dense") \
                .classes("w-full").style("font-family:var(--rd-mono)")
            also_items = ui.checkbox("Also switch every queued listing to live")
            with ui.row().classes("justify-end gap-2 w-full"):
                ui.button("Cancel", on_click=live_dialog.close).props("flat no-caps")
                ui.button("Go live", color="red",
                          on_click=lambda: _confirm_live()).props("no-caps")

        with ui.dialog() as clear_dialog, ui.card().classes("w-[23rem] max-w-full"):
            ui.label("Clear the queue?").classes("text-lg font-bold")
            ui.label("Every listing and its run config leaves Execute. The products "
                     "themselves stay in Monitors.").classes("rd-lede")
            with ui.row().classes("justify-end gap-2 w-full"):
                ui.button("Cancel", on_click=clear_dialog.close).props("flat no-caps")
                ui.button("Clear", color="red",
                          on_click=lambda: _do_clear()).props("no-caps")

        # ------------------------------------------------------------------ #
        # layout
        # ------------------------------------------------------------------ #
        with ui.element("div").classes("rdx-grid w-full"):
            with ui.column().classes("w-full min-w-0 gap-4"):
                with theme.card():
                    with ui.element("div").classes("rdx-head"):
                        theme.lab("Queue")
                        count_lbl = ui.label("").classes("rd-lab rd-num")
                    with ui.element("div").classes("rdx-bulk"):
                        ui.label("Apply to all").classes("rd-lab").style("align-self:center")
                        with _field("Profile", "10rem"):
                            bulk_profile = ui.select(_profile_options(), value="") \
                                .props("outlined dense options-dense").classes("w-full")
                        with _field("Proxy group", "9rem"):
                            _groups_now = _group_options()
                            bulk_group = ui.select(_groups_now, value=_groups_now[0]) \
                                .props("outlined dense options-dense").classes("w-full")
                        with _field("Poll (s)", "6.5rem"):
                            bulk_poll = ui.number(value=5.0, min=0.5, step=0.5, format="%.1f") \
                                .props("outlined dense").classes("w-full")
                        with _field("Mode", "8rem"):
                            bulk_mode = ui.select({"dry": "Dry run", "live": "Live"}, value="dry") \
                                .props("outlined dense options-dense").classes("w-full")
                        ui.button("Apply", on_click=lambda: _apply_bulk()) \
                            .props("outline dense no-caps size=sm")
                        ui.space()
                        ui.button("Clear queue", on_click=clear_dialog.open) \
                            .props("flat dense no-caps size=sm") \
                            .style("color:var(--rd-live)")
                    queue_body()

            with ui.column().classes("w-full min-w-0 gap-4"):
                with theme.card():
                    with ui.element("div").classes("rdx-arm"):
                        with ui.element("div").classes("rd-ritual"):
                            ui.element("i")
                            ui.element("i")
                            ui.element("i")
                            # color=None keeps NiceGUI from adding Quasar's
                            # bg-primary, whose utilities are !important.
                            disc = ui.button(on_click=lambda: _disc_click(), color=None) \
                                .classes("rd-disc").props("round")
                            with disc:
                                disc_lbl = ui.html('<div class="rdx-disclab">Start</div>',
                                                   sanitize=False)
                        state_lbl = ui.html('<span class="rdx-state">Not armed</span>',
                                            sanitize=False)
                        ui.element("div").style(
                            "height:1px;width:100%;background:var(--rd-line);margin:2px 0")
                        with ui.row().classes("items-center gap-3"):
                            mode_badge = ui.label("Dry run").classes("rd-badge rd-badge-dry")
                            live_sw = ui.switch(
                                "LIVE", value=not _gate_dry(),
                                on_change=lambda ev: _toggle_live(bool(ev.value))) \
                                .props("dense")
                        mode_note = ui.label("").classes("rd-lede").style(
                            "font-size:12px;text-align:center;max-width:36ch")
                        livebar = theme.block(
                            "<b>LIVE.</b> Real orders, real money, the moment a listing "
                            "shows stock.")

                with theme.card():
                    with ui.element("div").classes("rdx-pad flex flex-col gap-3"):
                        theme.lab("Runner")
                        with ui.row().classes("items-end gap-4 flex-wrap"):
                            with _field("Parallel workers", "8rem"):
                                ui.number(value=_workers_now(settings), min=1, max=64, step=1,
                                          on_change=lambda ev: _save_workers(ev.value)) \
                                    .props("outlined dense").classes("w-full")
                            with _field("Monitor cadence (s)", "9.5rem"):
                                ui.number(value=_interval_now(settings), min=0.5, step=0.5,
                                          format="%.1f",
                                          on_change=lambda ev: _save_interval(ev.value)) \
                                    .props("outlined dense").classes("w-full")
                        ui.label("Workers cap how many checkouts run at once; the rest "
                                 "queue. Cadence is the monitor's base tick — a listing's "
                                 "own poll seconds wins over it.") \
                            .classes("rd-lede").style("font-size:12px")

        with theme.card() as board_card:
            with board_card:
                with ui.row().classes("items-center justify-between w-full px-4 pt-3"):
                    theme.lab("Live board")
                    board_lbl = ui.label("").classes("rd-lab").style("color:var(--rd-ink-faint)")
            board_box = ui.column().classes("w-full gap-0")

        with theme.card() as log_card:
            with log_card:
                ui.label("Log").classes("rd-lab").style("padding:12px 15px 6px")
            logbox = ui.log(max_lines=_LOG_LINES).classes("w-full").style(
                "height:212px;background:var(--rd-term-bg);color:var(--rd-term-ink);"
                "font-family:var(--rd-mono);font-size:11px;border:1px solid var(--rd-line);"
                "border-radius:5px;margin:0 8px 8px")

        # ------------------------------------------------------------------ #
        # settings writes
        # ------------------------------------------------------------------ #
        def _save_workers(value) -> None:
            try:
                settings.data.setdefault("orchestrator", {})["max_workers"] = \
                    _as_int(value, 8, 1, 64)
                settings.save()
            except Exception as e:
                ui.notify(f"Could not save workers: {e}", type="negative")

        def _save_interval(value) -> None:
            try:
                settings.data.setdefault("poll", {})["interval_seconds"] = \
                    _as_float(value, 10.0, 0.5)
                settings.save()
            except Exception as e:
                ui.notify(f"Could not save cadence: {e}", type="negative")

        def _set_gate(dry: bool) -> None:
            try:
                settings.data.setdefault("checkout", {})["force_dry_run"] = bool(dry)
                settings.save()
            except Exception as e:
                ui.notify(f"Could not save the mode: {e}", type="negative")

        # ------------------------------------------------------------------ #
        # queue actions
        # ------------------------------------------------------------------ #
        def _remove(pid: str) -> None:
            try:
                queue.remove(pid)
            except Exception as e:
                ui.notify(f"Could not remove: {e}", type="negative")
                return
            ui.notify("Removed from the queue")
            queue_body.refresh()
            _paint_mode()

        def _apply_bulk() -> None:
            rows = _rows()
            if not rows:
                ui.notify("The queue is empty.", type="warning")
                return
            fields = {
                "profile_id": bulk_profile.value or "",
                "proxy_group": bulk_group.value or "default",
                "poll_seconds": _as_float(bulk_poll.value, 5.0, 0.5),
                "dry_run": (bulk_mode.value != "live"),
            }
            n = sum(1 for r in rows
                    if _set_quiet(r["entry"].get("product_id", ""), **fields))
            ui.notify(f"Applied to {n} listing{'s' if n != 1 else ''}")
            queue_body.refresh()
            _paint_mode()

        def _do_clear() -> None:
            clear_dialog.close()
            try:
                queue.clear()
            except Exception as e:
                ui.notify(f"Could not clear: {e}", type="negative")
                return
            _log("execute: queue cleared", "warn")
            ui.notify("Queue cleared")
            queue_body.refresh()
            _paint_mode()

        # ------------------------------------------------------------------ #
        # the dry-run / LIVE gate
        # ------------------------------------------------------------------ #
        def _toggle_live(on: bool) -> None:
            if guard["busy"]:
                return
            if on:
                guard["busy"] = True
                live_sw.value = False       # stays dry until the word is typed
                guard["busy"] = False
                typed_i.value = ""
                also_items.value = False
                live_dialog.open()
                return
            _set_gate(True)
            _log("execute: dry-run gate re-armed", "info")
            ui.notify("Dry run — nothing will be charged.", type="positive")
            _paint_mode()

        def _confirm_live() -> None:
            if (typed_i.value or "").strip().upper() != "LIVE":
                ui.notify("Type LIVE to confirm", type="warning")
                return
            typed_i.value = ""
            live_dialog.close()
            _set_gate(False)
            if also_items.value:
                for row in _rows():
                    _set_quiet(row["entry"].get("product_id", ""), dry_run=False)
                queue_body.refresh()
            guard["busy"] = True
            live_sw.value = True
            guard["busy"] = False
            _log("execute: dry-run gate released — LIVE", "warn")
            ui.notify("LIVE — real orders will be placed", type="warning")
            _paint_mode()

        def _paint_mode() -> None:
            rows = _rows()
            enabled = [e for e in _entries_enabled()]
            live_n = sum(1 for e in enabled if not e.get("dry_run", True))
            count_lbl.set_text(f"{len(enabled)} enabled · {len(rows)} total")
            if _gate_dry():
                mode_badge.set_text("Dry run")
                mode_badge.classes(replace="rd-badge rd-badge-dry")
                livebar.set_visibility(False)
                mode_note.set_text("The gate holds every task before the pay button. "
                                   "Nothing is charged.")
            else:
                mode_badge.set_text("LIVE")
                mode_badge.classes(replace="rd-badge rd-badge-live")
                livebar.set_visibility(True)
                if live_n:
                    mode_note.set_text(
                        f"{live_n} of {len(enabled)} enabled listing"
                        f"{'s' if len(enabled) != 1 else ''} will place a real order.")
                else:
                    mode_note.set_text("Gate released — but every enabled listing is "
                                       "still on dry run, so nothing will be charged.")

        # ------------------------------------------------------------------ #
        # arm / stop
        # ------------------------------------------------------------------ #
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
                ui.notify("Tasks or profiles are unavailable — cannot arm.", type="warning")
                return
            if not _entries_enabled():
                ui.notify("Enable at least one listing first.", type="warning")
                return
            try:
                made = queue.materialize(store=store, task_store=task_store,
                                         profile_store=profile_store)
            except Exception as e:
                _log(f"execute: materialize failed — {e}", "err")
                ui.notify(f"Could not build tasks: {e}", type="negative")
                return
            n = len(made or [])
            try:
                orch.reload_managers()
                orch.start()
            except Exception as e:
                _log(f"execute: start failed — {e}", "err")
                ui.notify(f"Start failed: {e}", type="negative")
                return
            live = not _gate_dry()
            mode = "LIVE" if live else "dry run"
            _log(f"execute: armed {n} task{'s' if n != 1 else ''} from the queue — {mode}",
                 "warn" if live else "info")
            ui.notify(f"Created {n} task{'s' if n != 1 else ''} — {mode}",
                      type="warning" if live else "positive")
            _tick()

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

        # ------------------------------------------------------------------ #
        # live board + log tail
        # ------------------------------------------------------------------ #
        def _exec_tasks() -> list:
            if task_store is None:
                return []
            try:
                return [t for t in task_store.list() if t.get("origin") == ORIGIN]
            except Exception:
                return []

        def _render_board(tasks: list) -> None:
            board_box.clear()
            with board_box:
                if not tasks:
                    with ui.element("div").classes("rdx-pad"):
                        ui.label("No tasks yet. START turns the enabled listings above "
                                 "into tasks and they appear here.").classes("rd-lede")
                    return
                for t in tasks:
                    with ui.element("div").classes("rdx-board"):
                        with ui.column().classes("gap-0 grow min-w-0"):
                            ui.label(t.get("name") or t.get("url") or t.get("id") or "—") \
                                .classes("rd-pname")
                            detail = t.get("last_error") or t.get("message") or ""
                            if detail:
                                ui.label(_short(str(detail), 72)).classes("rd-pmeta") \
                                    .tooltip(str(detail))
                        ui.label(f"try {_as_int(t.get('attempt'), 0, 0)}") \
                            .classes("rd-lab rd-num")
                        theme.chip(str(t.get("status") or "idle"))
                        ui.button("Run now", on_click=lambda tid=t.get("id"): _run_now(tid)) \
                            .props("flat dense no-caps size=sm")

        def _history() -> list:
            try:
                return list(bus.history) if bus is not None else []
            except Exception:
                return []

        def _tick() -> None:
            running = _is_running()
            if running != cache["running"]:
                cache["running"] = running
                if running:
                    disc.classes(add="rdx-stop")
                    disc_lbl.set_content('<div class="rdx-disclab">Stop</div>')
                else:
                    disc.classes(remove="rdx-stop")
                    disc_lbl.set_content('<div class="rdx-disclab">Start</div>')
                live_sw.set_enabled(not running)

            snap = {}
            try:
                snap = orch.snapshot() if orch is not None else {}
            except Exception:
                snap = {}
            if running:
                active = _as_int(snap.get("active_tasks"), 0, 0)
                phase = theme.esc(snap.get("phase") or "…")
                bit = (f"checking out {active} task(s)" if active
                       else f"watching {_as_int(snap.get('enabled_tasks'), 0, 0)} task(s)")
                state_lbl.set_content(
                    f'<span class="rdx-state"><span class="rdx-dot"></span>'
                    f'{theme.esc(bit)} · phase {phase}</span>')
            else:
                state_lbl.set_content('<span class="rdx-state">Not armed</span>')

            tasks = _exec_tasks()
            sig = "|".join(
                f"{t.get('id')}~{t.get('status')}~{t.get('attempt')}~"
                f"{t.get('message')}~{t.get('last_error')}~{t.get('updated')}"
                for t in tasks)
            if sig != cache["sig"]:
                cache["sig"] = sig
                _render_board(tasks)
                board_lbl.set_text(f"{len(tasks)} task{'s' if len(tasks) != 1 else ''}")

            hist = _history()
            if cache["logpos"] > len(hist):
                cache["logpos"] = max(0, len(hist) - _LOG_LINES)
            for line in hist[cache["logpos"]:]:
                logbox.push(f"[{getattr(line, 'ts', '')}] {getattr(line, 'text', line)}")
            cache["logpos"] = len(hist)

        # seed the log tail from history — never drains the bus
        for line in _history()[-_LOG_LINES:]:
            logbox.push(f"[{getattr(line, 'ts', '')}] {getattr(line, 'text', line)}")
        cache["logpos"] = len(_history())

        _paint_mode()
        _tick()
        ui.timer(1.0, _tick)


def _workers_now(settings) -> int:
    try:
        return max(1, min(64, int((settings.data.get("orchestrator") or {})
                                  .get("max_workers") or 8)))
    except Exception:
        return 8


def _interval_now(settings) -> float:
    try:
        return max(0.5, float((settings.data.get("poll") or {})
                              .get("interval_seconds") or 10))
    except Exception:
        return 10.0
