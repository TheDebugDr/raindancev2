"""Monitor — the single surface that carries a drop end to end.

One page instead of four. You pick the products to watch (Selection), press one
START disc that arms the multi-task bot (Control), watch every product's live
state fill in (Results), and — because the plan monitors with `monitor=True` —
the bot auto-fires checkout the instant a retailer shows stock (Action, inline
in each row). Nothing here re-implements the backend: START runs the exact
`run_plan` → `Orchestrator` sequence the Arm page used, and the Results table is
a read-only mirror of `TaskStore`.

Safety: dry-run is the default. Going LIVE needs a profile that can ship + bill
(`run_plan.live_blockers`) *and* a typed confirmation — and we explicitly clear
the `checkout.force_dry_run` guard a previous dry run leaves set, which would
otherwise silently downgrade a live run to dry.
"""
from __future__ import annotations

from nicegui import ui

from raindance.core import run_plan
from raindance.core.registry import ToolPlugin, register_tool
from raindance.core.search import placeholder_image
from raindance.core.sites import detect_site
from raindance.ui import theme

# Checkout pipeline, in order. Index a task's status onto this to draw the bar.
_PHASES = [
    ("launching", "Launch"),
    ("queued", "Queue"),
    ("atc", "Cart"),
    ("checkout", "Checkout"),
    ("submitting", "Submit"),
]
# status → how far along the 5-phase bar it is (captcha sits at the cart step).
_PHASE_AT = {
    "triggered": 0, "launching": 1, "queued": 2, "atc": 3, "captcha": 3,
    "checkout": 4, "submitting": 5, "success": 5,
}
_PIPELINE = set(_PHASE_AT) | {"retrying"}
_TERMINAL = {"success", "failed", "stopped"}

_FLOW_CSS = """
<style>
.rd-flow-grid { display:grid; grid-template-columns:1.12fr 1fr; gap:16px; align-items:start; }
@media (max-width:920px){ .rd-flow-grid { grid-template-columns:1fr; } }

.rd-selhead { display:flex; align-items:center; justify-content:space-between;
              padding:13px 15px; border-bottom:1px solid var(--rd-line); }
.rd-grouphead { display:flex; align-items:center; gap:10px; padding:9px 15px;
                background:color-mix(in srgb,var(--rd-surface-2) 40%,transparent);
                border-bottom:1px solid var(--rd-line-soft); }
.rd-grouphead .rd-rname { flex:1; }
.rd-selitem { display:flex; align-items:center; gap:12px; padding:10px 15px 10px 26px;
              border-bottom:1px solid var(--rd-line-soft); }
.rd-selitem:hover { background:color-mix(in srgb,var(--rd-surface-2) 55%,transparent); }
.rd-selfoot { display:flex; gap:9px; padding:12px 15px; border-top:1px solid var(--rd-line); }

.rd-ctrl { padding:22px 16px 18px; display:flex; flex-direction:column;
           align-items:center; gap:13px; }
.rd-disc.rd-stop {
  background:radial-gradient(circle at 50% 42%,#ff6b66,var(--rd-live) 62%) !important;
  color:#fff !important;
  box-shadow:0 0 0 1px color-mix(in srgb,var(--rd-live) 40%,transparent),
             0 18px 50px -20px var(--rd-live); }
.rd-disc.disabled, .rd-disc[disabled] { opacity:.42; filter:saturate(.4); cursor:not-allowed; }
.rd-disc .rd-disclab { font-family:var(--rd-mono); font-weight:700; letter-spacing:.14em;
  text-transform:uppercase; font-size:12px; line-height:1.4; text-align:center; white-space:normal; }
.rd-progline { font-family:var(--rd-mono); font-size:12px; color:var(--rd-ink-dim);
               display:flex; align-items:center; gap:8px; min-height:18px; }
.rd-livedot { width:7px; height:7px; border-radius:50%; background:var(--rd-ok);
              animation:rd-pl 1.4s ease-in-out infinite; }
@keyframes rd-pl { 0%,100%{opacity:.35} 50%{opacity:1} }
@media (prefers-reduced-motion: reduce){ .rd-livedot{ animation:none } }

.rd-resrow { display:flex; align-items:center; gap:13px; padding:12px 15px;
             border-bottom:1px solid var(--rd-line-soft); }
.rd-resrow:last-child { border-bottom:0; }
.rd-thumb { width:34px; height:34px; min-width:34px; border-radius:5px; object-fit:cover;
            background:var(--rd-surface-2); border:1px solid var(--rd-line-soft); }
.rd-openlink { font-family:var(--rd-mono); font-size:10px; letter-spacing:.06em;
  text-transform:uppercase; color:var(--rd-ink-dim); text-decoration:none;
  border:1px solid var(--rd-line); border-radius:4px; padding:5px 9px; white-space:nowrap; }
.rd-openlink:hover { border-color:var(--rd-watch); color:var(--rd-ink); }

.rd-phbar { display:flex; gap:0; margin-top:2px; width:100%; }
.rd-ph { flex:1; display:flex; flex-direction:column; gap:5px; padding-right:6px; }
.rd-ph .rd-track { height:3px; border-radius:2px; background:var(--rd-line); }
.rd-ph.rd-done .rd-track { background:var(--rd-ok); }
.rd-ph.rd-now  .rd-track { background:var(--rd-watch); }
.rd-ph .rd-plab { font-family:var(--rd-mono); font-size:8.5px; letter-spacing:.05em;
                  text-transform:uppercase; color:var(--rd-ink-faint); }
.rd-ph.rd-done .rd-plab { color:var(--rd-ok); }
.rd-ph.rd-now  .rd-plab { color:var(--rd-watch); }
.rd-reveal { animation:rd-reveal .4s ease-out; }
@keyframes rd-reveal { from{opacity:0;transform:translateY(-3px)} to{opacity:1;transform:none} }
</style>
"""


def _time_of(value: str) -> str:
    """The clock part of an ISO/`HH:MM:SS` timestamp, for a compact 'last check'."""
    if not value:
        return "—"
    return (value.split("T")[-1] if "T" in value else value)[:8]


def _phase_index(status: str) -> int:
    return _PHASE_AT.get(status, -1)


@register_tool
class FlowTool(ToolPlugin):
    id = "flow"
    name = "Monitor"
    icon = "radar"
    order = 0
    description = "Select products, start monitoring, and act the moment stock lands."

    def render(self, ctx) -> None:
        theme.inject()
        ui.add_head_html(_FLOW_CSS)

        store = ctx.store
        orch = ctx.orchestrator
        # Every service the flow drives must exist; degrade honestly if not.
        if store is None or orch is None or ctx.task_store is None:
            theme.title("Monitor")
            theme.note("Monitoring services are unavailable in this build.")
            return

        plan = run_plan.prune(run_plan.load(ctx.settings), store)
        # Ephemeral: which products to watch *now*. Seed from the saved plan,
        # else select everything so START works on first run.
        selected: set[str] = set(plan["product_ids"]) or {p["id"] for p in store.items}
        ui_state = {"live": False}
        # last rendered results signature — rebuild the table only when it changes,
        # so a 1s timer never flickers a table the user is reading.
        cache = {"sig": None, "logpos": len(ctx.bus.history)}

        # ---- header ------------------------------------------------------- #
        theme.title("Monitor")
        theme.lede("Pick what to watch, press start, and RainDance checks every "
                   "product on repeat — firing checkout automatically the moment "
                   "one shows in stock.")

        grid = ui.element("div").classes("rd-flow-grid w-full")

        # ================================================================== #
        # SECTION 1 — Product selection
        # ================================================================== #
        with grid:
            with theme.card() as sel_card:
                with sel_card:
                    theme.lab("① Select · what to monitor")
                with ui.element("div").classes("rd-selhead"):
                    theme.lab("Products, grouped by retailer")
                    count_lbl = ui.label("").classes("rd-lab").style("color:var(--rd-ink-dim)")

                @ui.refreshable
                def selection_body() -> None:
                    grouped = store.by_site()
                    if not store.items:
                        theme.note("No products yet. Add one below, or open "
                                   "<b>Discover</b> to pull popular Pokémon products "
                                   "from the web.")
                        return
                    for site in sorted(grouped):
                        items = grouped[site]
                        site_ids = {p["id"] for p in items}
                        all_on = site_ids <= selected

                        def _toggle_group(e, ids=site_ids) -> None:
                            if e.value:
                                selected.update(ids)
                            else:
                                selected.difference_update(ids)
                            selection_body.refresh()
                            _sync()

                        with ui.element("div").classes("rd-grouphead"):
                            ui.checkbox(value=all_on, on_change=_toggle_group).props("dense")
                            ui.label(site).classes("rd-rname rd-pname")
                            ui.label(f"{len(items)}").classes("rd-lab rd-num")
                        for p in items:
                            _selection_row(p)

                def _selection_row(p: dict) -> None:
                    def _toggle(e, pid=p["id"]) -> None:
                        if e.value:
                            selected.add(pid)
                        else:
                            selected.discard(pid)
                        _sync()   # cheap: only the counter + disc state, no rebuild

                    def _remove(pid=p["id"]) -> None:
                        selected.discard(pid)
                        store.remove(pid)
                        ui.notify("Removed", type="info")
                        selection_body.refresh()
                        _sync()

                    def _set_priority(e, pid=p["id"]) -> None:
                        store.update(pid, priority=e.value or "normal")

                    with ui.element("div").classes("rd-selitem"):
                        ui.checkbox(value=p["id"] in selected, on_change=_toggle).props("dense")
                        with ui.column().classes("gap-0 grow min-w-0"):
                            ui.label(p.get("name") or p["url"]).classes("rd-pname")
                            thr = p.get("price_threshold")
                            meta = f"{theme.esc(p.get('site', '—'))}"
                            if thr not in (None, ""):
                                meta += f" · alert ≤ ${theme.esc(thr)}"
                            ui.html(meta, sanitize=False).classes("rd-pmeta")
                        ui.select({"high": "High", "normal": "Normal", "low": "Low"},
                                  value=p.get("priority") or "normal",
                                  on_change=_set_priority) \
                            .props("dense outlined options-dense") \
                            .classes("rd-prio").style("min-width:88px") \
                            .tooltip("Priority — High polls fastest and checks out first")
                        ui.button(icon="close", on_click=_remove) \
                            .props("flat dense round size=sm").classes("rd-idle") \
                            .tooltip("Remove product")

                selection_body()
                with ui.element("div").classes("rd-selfoot"):
                    ui.button("Add product", icon="add",
                              on_click=lambda: add_dialog.open()).props("outline no-caps dense")
                    ui.button("Discover", icon="travel_explore",
                              on_click=lambda: _open_search()).props("outline no-caps dense")

            # ============================================================== #
            # SECTION 2 — Control
            # ============================================================== #
            with theme.card() as ctrl_card:
                with ctrl_card:
                    theme.lab("② Control")
                with ui.element("div").classes("rd-ctrl"):
                    with ui.element("div").classes("rd-ritual"):
                        ui.element("i")
                        ui.element("i")
                        ui.element("i")
                        # color=None stops NiceGUI adding Quasar's !important bg-primary.
                        disc = ui.button(on_click=lambda: _disc_click(), color=None) \
                            .classes("rd-disc").props("round")
                        with disc:
                            disc_lbl = ui.html('<div class="rd-disclab">Start<br>monitoring</div>',
                                               sanitize=False)
                    prog_lbl = ui.html('<span class="rd-progline">Not monitoring</span>',
                                       sanitize=False)

                    ui.element("div").style(
                        "height:1px;width:100%;background:var(--rd-line);margin:2px 0")

                    with ui.row().classes("items-center gap-2"):
                        live_sw = ui.switch("Checkout mode: LIVE", value=False,
                                            on_change=lambda e: _toggle_live(e.value))
                        mode_badge = ui.label("Dry run").classes("rd-badge rd-badge-dry")
                    ui.label("Dry run walks the whole checkout and stops before paying. "
                             "LIVE needs a saved profile and a typed confirmation.") \
                        .classes("rd-lede").style("font-size:12px;text-align:center;max-width:34ch")

        # ================================================================== #
        # SECTION 3 — Live results (+ inline actions = Section 4)
        # ================================================================== #
        with theme.card() as res_card:
            with res_card:
                with ui.row().classes("items-center justify-between w-full px-1"):
                    theme.lab("③ Live results")
                    updated_lbl = ui.label("").classes("rd-lab").style("color:var(--rd-ink-faint)")
            results_box = ui.column().classes("w-full gap-0")

            with res_card:
                theme.lab("Live log")
            logbox = ui.log(max_lines=400).classes("w-full").style(
                "height:150px;background:var(--rd-term-bg);color:var(--rd-term-ink);"
                "font-family:var(--rd-mono);font-size:11px;border:1px solid var(--rd-line);"
                "border-radius:5px;margin:0 8px 8px")
            for line in ctx.bus.history[-60:]:
                logbox.push(f"[{line.ts}] {line.text}")

        # ================================================================== #
        # add-product dialog
        # ================================================================== #
        with ui.dialog() as add_dialog, ui.card().classes("w-[26rem] max-w-full"):
            ui.label("Add a product").classes("text-lg font-bold")
            add_name = ui.input("Name (optional)").classes("w-full")
            add_url = ui.input("Product URL", placeholder="https://retailer.com/…").classes("w-full")
            add_thr = ui.number("Alert at or below ($) — optional").classes("w-full")
            with ui.row().classes("justify-end gap-2 w-full"):
                ui.button("Cancel", on_click=add_dialog.close).props("flat no-caps")
                ui.button("Add", color="primary", on_click=lambda: _submit_add()).props("no-caps")

        def _submit_add() -> None:
            url = (add_url.value or "").strip()
            if not url:
                ui.notify("Enter a product URL", type="warning")
                return
            p = store.add(name=(add_name.value or "").strip(), url=url,
                          price_threshold=add_thr.value)
            selected.add(p["id"])
            add_name.value = add_url.value = ""
            add_thr.value = None
            add_dialog.close()
            ui.notify(f"Added → {detect_site(url)}", type="positive")
            selection_body.refresh()
            _sync()

        # ================================================================== #
        # discover slide-over
        # ================================================================== #
        backdrop = (ui.element("div")
                    .style("position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:40;"
                           "display:none").on("click", lambda: _close_search()))
        panel = ui.column().classes("gap-3").style(
            "position:fixed;top:0;right:0;height:100vh;width:25rem;max-width:100vw;z-index:50;"
            "background:var(--rd-surface);padding:16px;box-shadow:-8px 0 24px rgba(0,0,0,.4);"
            "transform:translateX(100%);transition:transform .25s ease;overflow:auto")
        with panel:
            with ui.row().classes("items-center justify-between w-full"):
                ui.label("Discover products").classes("text-lg font-bold")
                ui.button(icon="close", on_click=lambda: _close_search()).props("flat dense round")
            ui.label(f"source: {ctx.search.provider_name}").classes("rd-lab") \
                .style("color:var(--rd-ink-faint)")
            with ui.row().classes("w-full no-wrap gap-2 items-center"):
                query_i = (ui.input(placeholder="Search Pokémon products…")
                           .props("dense outlined").classes("grow")
                           .on("keydown.enter", lambda: _do_search()))
                ui.button(icon="search", on_click=lambda: _do_search()).props("dense")
            search_results = ui.column().classes("w-full gap-2")
        search_state = {"loaded": False}

        def _open_search() -> None:
            panel.style("transform:translateX(0)")
            backdrop.style("display:block")
            if not search_state["loaded"]:
                search_state["loaded"] = True
                _do_search()

        def _close_search() -> None:
            panel.style("transform:translateX(100%)")
            backdrop.style("display:none")

        def _do_search() -> None:
            results = ctx.search.search(query_i.value or "")
            search_results.clear()
            with search_results:
                if not results:
                    ui.label("No results.").classes("rd-lede")
                for r in results:
                    _search_card(r)

        def _search_card(r) -> None:
            holder: dict = {}

            def _add() -> None:
                p = store.add(name=r.name, url=r.url, price=r.price, site=r.site, image=r.image)
                selected.add(p["id"])
                if holder.get("btn"):
                    holder["btn"].set_text("Added")
                    holder["btn"].props("disable")
                ui.notify(f"Added {r.name} → {r.site}", type="positive")
                selection_body.refresh()
                _sync()

            ph = placeholder_image(r.name)
            src = r.image or ph
            img = (f'<img src="{src}" referrerpolicy="no-referrer" loading="lazy" '
                   f'onerror="this.onerror=null;this.src=\'{ph}\'" class="rd-thumb" '
                   'style="width:48px;height:48px">')
            with theme.card("p-2"):
                with ui.row().classes("items-center gap-3 no-wrap w-full"):
                    ui.html(img, sanitize=False)
                    with ui.column().classes("gap-1 min-w-0 grow"):
                        ui.label(r.name).classes("rd-pname").style("white-space:normal")
                        with ui.row().classes("items-center gap-2"):
                            ui.badge(r.site, color="primary")
                            ui.label(f"${r.price:.2f}" if r.price is not None else "—") \
                                .classes("rd-pmeta")
                    holder["btn"] = ui.button("Add", icon="add", on_click=_add) \
                        .props("dense unelevated no-caps")

        # ================================================================== #
        # live confirm dialog
        # ================================================================== #
        with ui.dialog() as live_dialog, ui.card().classes("w-[26rem] max-w-full"):
            ui.label("Go live?").classes("text-lg font-bold").style("color:var(--rd-live)")
            ui.label("This will place a REAL order and charge the card on your saved "
                     "profile the moment a product is in stock.").classes("rd-lede")
            typed_i = ui.input(placeholder="type LIVE").props("outlined dense") \
                .classes("w-full").style("font-family:var(--rd-mono)")
            with ui.row().classes("justify-end gap-2 w-full"):
                ui.button("Cancel", on_click=live_dialog.close).props("flat no-caps")
                ui.button("Arm live", color="red",
                          on_click=lambda: _confirm_live()).props("no-caps")

        def _confirm_live() -> None:
            if (typed_i.value or "").strip().upper() != "LIVE":
                ui.notify("Type LIVE to confirm", type="warning")
                return
            typed_i.value = ""
            live_dialog.close()
            _do_start(live=True)

        # ================================================================== #
        # control logic
        # ================================================================== #
        def _sync() -> None:
            n, total = len(selected), len(store.items)
            count_lbl.set_text(f"{n} of {total} selected")
            if not orch.is_running:
                disc.set_enabled(n > 0)

        def _toggle_live(on: bool) -> None:
            if on:
                blockers = run_plan.live_blockers(ctx.profile_store)
                if blockers:
                    live_sw.value = False
                    ui_state["live"] = False
                    _paint_mode()
                    ui.notify(blockers[0], type="warning")
                    return
            ui_state["live"] = bool(on)
            _paint_mode()

        def _paint_mode() -> None:
            if ui_state["live"]:
                mode_badge.set_text("LIVE")
                mode_badge.classes(replace="rd-badge rd-badge-live")
            else:
                mode_badge.set_text("Dry run")
                mode_badge.classes(replace="rd-badge rd-badge-dry")

        def _disc_click() -> None:
            if orch.is_running:
                orch.stop()
                ui.notify("Monitoring stopped", type="info")
                return
            if not selected:
                ui.notify("Select at least one product to monitor", type="warning")
                return
            if ui_state["live"]:
                blockers = run_plan.live_blockers(ctx.profile_store)
                if blockers:
                    ui.notify(blockers[0], type="warning")
                    return
                live_dialog.open()
            else:
                _do_start(live=False)

        def _do_start(live: bool) -> None:
            ids = [p["id"] for p in store.items if p["id"] in selected]
            if not ids:
                ui.notify("Select at least one product to monitor", type="warning")
                return
            plan["product_ids"] = ids
            plan["retailers"] = sorted({p["site"] for p in store.items if p["id"] in selected})
            plan["live"] = live
            plan["start_at"] = ""   # start now
            run_plan.save(ctx.settings, plan)
            run_plan.apply_to_settings(plan, ctx.settings)
            if live:
                # apply_to_settings only ever *raises* force_dry_run; a live run must
                # clear a guard a previous dry run left set, or it silently stays dry.
                ctx.settings.data.setdefault("checkout", {})["force_dry_run"] = False
                ctx.settings.save()
            tasks = run_plan.materialize_tasks(
                plan, store=store, task_store=ctx.task_store, profile_store=ctx.profile_store)
            orch.reload_managers()
            ok = orch.start()
            if ok:
                ui.notify(
                    f"Monitoring {len(tasks)} product{'s' if len(tasks) != 1 else ''} — "
                    f"{'LIVE' if live else 'dry run'}",
                    type="warning" if live else "positive")
            else:
                ui.notify("Could not start — see the live log", type="warning")

        # ================================================================== #
        # results + progress, on one timer
        # ================================================================== #
        def _rows() -> list[tuple[dict, dict | None]]:
            """(product, task) pairs to display, joined by URL."""
            tasks_by_url: dict[str, dict] = {}
            for t in ctx.task_store.list():
                tasks_by_url.setdefault((t.get("url") or "").strip(), t)
            chosen = [p for p in store.items if p["id"] in selected]
            if not chosen and orch.is_running:
                # everything deselected mid-run: still show what's actually running
                seen = set()
                out = []
                for t in ctx.task_store.list(enabled_only=True):
                    key = (t.get("url") or "").strip()
                    if key in seen:
                        continue
                    seen.add(key)
                    prod = next((p for p in store.items if p["url"].strip() == key), None)
                    out.append((prod or {"name": t.get("name"), "url": t.get("url"),
                                         "site": "—"}, t))
                return out
            return [(p, tasks_by_url.get(p["url"].strip())) for p in chosen]

        def _render_results(rows) -> None:
            results_box.clear()
            with results_box:
                if not rows:
                    theme.note("No products selected. Tick one on the left, then press "
                               "<b>Start monitoring</b>.")
                    return
                for p, t in rows:
                    _result_row(p, t)

        def _result_row(p: dict, t: dict | None) -> None:
            status = (t or {}).get("status") or p.get("status") or "pending"
            message = (t or {}).get("message") or ""
            updated = (t or {}).get("updated") or p.get("last_checked") or ""
            with ui.element("div").classes("rd-resrow"):
                ph = placeholder_image(p.get("name") or p.get("url") or "?")
                src = p.get("image") or ph
                ui.html(f'<img src="{src}" referrerpolicy="no-referrer" loading="lazy" '
                        f'onerror="this.onerror=null;this.src=\'{ph}\'" class="rd-thumb">',
                        sanitize=False)
                with ui.column().classes("gap-1 grow min-w-0"):
                    with ui.row().classes("items-center gap-2 no-wrap"):
                        ui.label(p.get("name") or p.get("url")).classes("rd-pname")
                        ui.label(p.get("site") or "—").classes("rd-lab") \
                            .style("color:var(--rd-ink-faint)")
                    if status in _PIPELINE and status not in _TERMINAL:
                        _phase_bar(status)
                        if message:
                            ui.label(message).classes("rd-pmeta")
                    elif status == "success":
                        oid = (t or {}).get("order_id")
                        dry = (t or {}).get("dry_run", True)
                        txt = ("Dry run reached checkout — stopped before paying"
                               if dry else f"Order placed{' · ' + oid if oid else ''}")
                        ui.label(txt).classes("rd-pmeta").style("color:var(--rd-ok)")
                    elif status == "failed":
                        ui.label((t or {}).get("last_error") or "Failed").classes("rd-pmeta") \
                            .style("color:var(--rd-live)")
                    elif message:
                        ui.label(message).classes("rd-pmeta")
                with ui.column().classes("items-end gap-1 shrink-0"):
                    theme.chip(status)
                    with ui.row().classes("items-center gap-2 no-wrap"):
                        ui.label(_time_of(updated)).classes("rd-mono rd-num") \
                            .style("font-size:11px;color:var(--rd-ink-faint)")
                        if p.get("url"):
                            ui.html(f'<a class="rd-openlink" href="{theme.esc(p["url"])}" '
                                    'target="_blank" rel="noopener">Open ↗</a>', sanitize=False)
                        if t and status in ("failed", "stopped"):
                            ui.button("Retry", on_click=lambda tid=t["id"]: (
                                orch.run_task_now(tid), ui.notify("Retrying…"))) \
                                .props("flat dense no-caps size=sm").classes("rd-watch")

        def _phase_bar(status: str) -> None:
            idx = _phase_index(status)
            with ui.element("div").classes("rd-phbar rd-reveal"):
                for i, (_key, label) in enumerate(_PHASES, start=1):
                    cls = "rd-ph"
                    if idx > i:
                        cls += " rd-done"
                    elif idx == i:
                        cls += " rd-now"
                    with ui.element("div").classes(cls):
                        ui.label(("✓ " if idx > i else "") + label).classes("rd-plab")
                        ui.element("div").classes("rd-track")

        def _tick() -> None:
            snap = orch.snapshot()
            running = bool(snap.get("running"))

            # disc identity — only repaint on an actual running-state transition
            if running != cache.get("running"):
                cache["running"] = running
                if running:
                    disc.classes(add="rd-stop")
                    disc_lbl.set_content('<div class="rd-disclab">Stop<br>monitoring</div>')
                else:
                    disc.classes(remove="rd-stop")
                    disc_lbl.set_content('<div class="rd-disclab">Start<br>monitoring</div>')
                live_sw.set_enabled(not running)
            disc.set_enabled(running or len(selected) > 0)

            # progress line
            if running:
                phase = snap.get("phase") or "…"
                active = snap.get("active_tasks", 0)
                mon = "on" if snap.get("monitor_alive") else "off"
                bit = (f'checking out {active} task(s)' if active
                       else f'watching {snap.get("enabled_tasks", 0)} product(s)')
                prog_lbl.set_content(
                    f'<span class="rd-progline"><span class="rd-livedot"></span>'
                    f'{theme.esc(bit)} · phase {theme.esc(phase)} · monitor {mon}</span>')
                updated_lbl.set_text("live · updates every second")
            else:
                prog_lbl.set_content('<span class="rd-progline">Not monitoring</span>')
                updated_lbl.set_text("idle")

            # results — rebuild only when something changed (flicker-free)
            rows = _rows()
            sig = tuple((
                (p.get("id") or p.get("url")),
                (t or {}).get("status") or p.get("status"),
                (t or {}).get("message"),
                (t or {}).get("updated") or p.get("last_checked"),
                (t or {}).get("order_id"),
            ) for p, t in rows)
            if sig != cache["sig"]:
                cache["sig"] = sig
                _render_results(rows)

            # log tail — append-only mirror of history (never drains the bus)
            hist = ctx.bus.history
            if cache["logpos"] > len(hist):
                cache["logpos"] = 0
            for line in hist[cache["logpos"]:]:
                logbox.push(f"[{line.ts}] {line.text}")
            cache["logpos"] = len(hist)

        _paint_mode()
        _sync()
        _tick()
        ui.timer(1.0, _tick)
