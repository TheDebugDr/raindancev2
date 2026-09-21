"""Arm — the drop ritual: one button, four questions, then the bot is live.

With a saved plan, START arms it. With none, START opens the wizard. The last
question is always "does this spend money", and the answer is never a checkbox
buried among other checkboxes.
"""
from __future__ import annotations

from nicegui import ui

from raindance.core import run_plan
from raindance.core.registry import ToolPlugin, register_tool
from raindance.ui import theme

_STEPS = ["Retailers", "Items", "Drop time", "Confirm"]


@register_tool
class ArmTool(ToolPlugin):
    id = "arm"
    name = "Arm"
    icon = "play_circle"
    order = 2
    stage = True
    description = "Pick retailers and items, set the drop time, then arm."

    def render(self, ctx) -> None:
        theme.inject()

        plan = run_plan.prune(run_plan.load(ctx.settings), ctx.store)
        blockers = run_plan.live_blockers(ctx.profile_store)
        wiz = {"step": 0, "mode": "live" if plan["live"] and not blockers else "dry", "typed": ""}

        # ------------------------------------------------------------------ #
        # arming
        # ------------------------------------------------------------------ #
        def _arm() -> None:
            live = wiz["mode"] == "live"
            if live and blockers:
                ui.notify(blockers[0], type="warning")
                return
            if run_plan.is_empty(plan):
                ui.notify("Pick at least one item first.", type="warning")
                return

            plan["live"] = live
            run_plan.save(ctx.settings, plan)
            run_plan.apply_to_settings(plan, ctx.settings)
            tasks = run_plan.materialize_tasks(
                plan, store=ctx.store, task_store=ctx.task_store,
                profile_store=ctx.profile_store,
            )
            ctx.bus.log(
                f"armed {len(tasks)} tasks — {'LIVE' if live else 'dry run'} — "
                f"{run_plan.summary(plan, ctx.store)}",
                "warn" if live else "info",
            )
            ctx.orchestrator.reload_managers()
            ctx.orchestrator.start()
            ui.notify(f"Armed {len(tasks)} task{'s' if len(tasks) != 1 else ''}"
                      f" — {'LIVE' if live else 'dry run'}",
                      type="warning" if live else "positive")
            body.refresh()

        # ------------------------------------------------------------------ #
        # home
        # ------------------------------------------------------------------ #
        def _home() -> None:
            with ui.column().classes("w-full items-center gap-6 py-10"):
                with ui.element("div").classes("rd-ritual"):
                    ui.element("i")
                    ui.element("i")
                    ui.element("i")
                    # color=None keeps NiceGUI from adding Quasar's bg-primary,
                    # whose utility classes are !important and would win.
                    disc = ui.button(on_click=_start_click, color=None) \
                        .classes("rd-disc").props("round")
                    with disc:
                        ui.html('<div style="text-align:center;line-height:1.35">'
                                '<div style="font-size:19px">&#9733;</div>START</div>',
                                sanitize=False)

                if run_plan.is_empty(plan):
                    ui.label("No saved plan yet.").classes("text-lg")
                    ui.label("Pick your retailers, then the items you want from each.") \
                        .style("color:var(--rd-ink-dim)")
                else:
                    ui.label(f"Arming {run_plan.summary(plan, ctx.store)}").classes("text-lg")
                    ui.label(f"Saved plan · {'live' if plan['live'] else 'dry run'} · "
                             f"{plan['workers']} parallel workers") \
                        .style("color:var(--rd-ink-dim)").classes("text-sm")
                    with ui.row().classes("gap-2"):
                        ui.button("Arm this plan", on_click=_arm).props("unelevated no-caps")
                        ui.button("Change setup", on_click=lambda: _goto(1)) \
                            .props("outline no-caps")

        def _start_click() -> None:
            if run_plan.is_empty(plan):
                _goto(1)
            else:
                _arm()

        def _goto(step: int) -> None:
            wiz["step"] = step
            body.refresh()

        # ------------------------------------------------------------------ #
        # wizard steps
        # ------------------------------------------------------------------ #
        def _step_retailers() -> None:
            grouped = ctx.store.by_site()
            with theme.card():
                with ui.element("div").classes("p-4"):
                    theme.lab("Which retailers are you going through?")
                for site in sorted(grouped):
                    items = grouped[site]

                    def _toggle(e, s=site) -> None:
                        if e.value:
                            if s not in plan["retailers"]:
                                plan["retailers"].append(s)
                        else:
                            if s in plan["retailers"]:
                                plan["retailers"].remove(s)
                            keep = {p["id"] for p in ctx.store.for_site(s)}
                            plan["product_ids"] = [i for i in plan["product_ids"] if i not in keep]
                        panel.refresh()   # never rebuild the list being clicked

                    with ui.element("div").classes("rd-row"):
                        ui.checkbox(value=site in plan["retailers"], on_change=_toggle)
                        with ui.column().classes("gap-0 grow min-w-0"):
                            ui.label(site).classes("rd-pname")
                            ui.html(f"handler <code>{run_plan.handler_for(site)}</code>",
                                    sanitize=False).classes("rd-pmeta")
                        ui.label(f"{len(items)} item{'s' if len(items) != 1 else ''}") \
                            .classes("rd-lab rd-num")

        def _step_items() -> None:
            if not plan["retailers"]:
                with theme.card():
                    with ui.element("div").classes("p-4"):
                        theme.lab("Pick a retailer first")
                        ui.label("Go back to step 01.").style("color:var(--rd-ink-dim)")
                return

            with theme.card():
                for site in sorted(plan["retailers"]):
                    with ui.element("div").classes("p-4 pb-2"):
                        theme.lab(site)
                    for p in ctx.store.for_site(site):

                        def _toggle(e, pid=p["id"]) -> None:
                            if e.value:
                                if pid not in plan["product_ids"]:
                                    plan["product_ids"].append(pid)
                            elif pid in plan["product_ids"]:
                                plan["product_ids"].remove(pid)
                            panel.refresh()   # never rebuild the list being clicked

                        with ui.element("div").classes("rd-row"):
                            ui.checkbox(value=p["id"] in plan["product_ids"], on_change=_toggle)
                            with ui.column().classes("gap-0 grow min-w-0"):
                                ui.label(p.get("name") or p["url"]).classes("rd-pname")
                                ui.label(f"every {p.get('frequency') or '—'}").classes("rd-pmeta")
                            theme.chip(p.get("status", "unknown"))

        def _step_time() -> None:
            with theme.card():
                with ui.element("div").classes("p-4 flex flex-col gap-2"):
                    theme.lab("Drop time")
                    at = ui.input(placeholder="HH:MM[:SS] or ISO", value=plan["start_at"]) \
                        .props("outlined dense").style("width:11rem")
                    at.on_value_change(lambda e: plan.update(start_at=e.value or ""))
                    ui.label("The bot idles until this moment, then begins watching. "
                             "Leave empty to start now — the wait is interruptible.") \
                        .classes("rd-lede text-sm")

                    theme.lab("Parallel workers")
                    w = ui.number(value=plan["workers"], min=1, max=64) \
                        .props("outlined dense").style("width:6rem")
                    w.on_value_change(lambda e: plan.update(workers=int(e.value or 8)))
                    ui.label("How many checkout sessions may run at once. "
                             "The rest queue.").classes("rd-lede text-sm")

        def _step_confirm() -> None:
            with theme.card():
                with ui.element("div").classes("p-4 flex flex-col gap-3"):
                    theme.lab("How should this run end?")

                    def _pick(mode: str) -> None:
                        wiz["mode"] = mode
                        wiz["typed"] = ""
                        body.refresh()

                    dry_on = wiz["mode"] == "dry"
                    with ui.element("div").classes("rd-card p-4 cursor-pointer") \
                            .style(f"border-color:{'var(--rd-ink-dim)' if dry_on else 'var(--rd-line)'}") \
                            .on("click", lambda: _pick("dry")):
                        ui.label("Dry run").classes("font-semibold")
                        ui.label("Walks the entire flow — add to cart, fill checkout, reach the "
                                 "final button — and stops before paying. Nothing is charged.") \
                            .classes("rd-lede text-sm")

                    live_on = wiz["mode"] == "live"
                    style = f"border-color:{'var(--rd-live)' if live_on else 'var(--rd-line)'}"
                    if blockers:
                        style += ";opacity:.5;cursor:not-allowed"
                    el = ui.element("div").classes("rd-card p-4").style(style)
                    if not blockers:
                        el.classes("cursor-pointer").on("click", lambda: _pick("live"))
                    with el:
                        ui.label("Live").classes("font-semibold")
                        ui.label(blockers[0] if blockers
                                 else "Places a real order and charges the card on the profile.") \
                            .classes("rd-lede text-sm")

                    if live_on and not blockers:
                        with ui.row().classes("items-center gap-2"):
                            ui.label("Type LIVE to confirm").classes("rd-lede text-sm")
                            t = ui.input(placeholder="LIVE").props("outlined dense") \
                                .style("width:8rem;font-family:var(--rd-mono)")
                            # panel only — refreshing the body would rebuild this input
                            # and drop focus on every keystroke.
                            t.on_value_change(lambda e: (wiz.update(typed=e.value or ""),
                                                         panel.refresh()))

                    if blockers:
                        theme.block("<b>Live is blocked.</b> " + blockers[0] +
                                    " Add one in <b>Set up</b>. Dry runs still work.")

        # ------------------------------------------------------------------ #
        # the run-plan panel — the products→tasks bridge, made visible
        # ------------------------------------------------------------------ #
        @ui.refreshable
        def panel() -> None:
            chosen = run_plan.products(plan, ctx.store)
            with theme.card("p-4"):
                theme.lab("Run plan")
                for k, v in (("Retailers", len(plan["retailers"]) or "—"),
                             ("Items", len(chosen) or "—"),
                             ("Drop time", plan["start_at"] or "now"),
                             ("Workers", plan["workers"])):
                    with ui.row().classes("justify-between w-full items-baseline"):
                        ui.label(k).style("color:var(--rd-ink-faint);font-size:13px")
                        ui.label(str(v)).classes("rd-mono rd-num").style("font-size:12px")

                colour = ("var(--rd-live)" if wiz["mode"] == "live" else "var(--rd-ok)")
                with ui.row().classes("justify-between w-full items-baseline"):
                    ui.label("Mode").style("color:var(--rd-ink-faint);font-size:13px")
                    ui.label(wiz["mode"].upper()).classes("rd-mono").style(
                        f"font-size:12px;color:{colour}")

                if chosen:
                    ui.separator().classes("my-2")
                    theme.lab(f"Will create {len(chosen)} task{'s' if len(chosen) != 1 else ''}")
                    for p in chosen:
                        with ui.column().classes("gap-0"):
                            ui.label("→ " + (p.get("name") or p["url"])[:32]) \
                                .style("color:var(--rd-ink-dim);font-size:12px")
                            ui.label(run_plan.handler_for(p.get("site", ""))) \
                                .classes("rd-mono").style(
                                    "font-size:10.5px;color:var(--rd-ink-faint);padding-left:14px")

                step = wiz["step"]
                can_next = (
                    (step == 1 and bool(plan["retailers"]))
                    or (step == 2 and bool(plan["product_ids"]))
                    or step == 3
                    or (step == 4 and (wiz["mode"] == "dry"
                                       or (wiz["mode"] == "live" and not blockers
                                           and wiz["typed"].upper() == "LIVE")))
                )
                with ui.row().classes("gap-2 w-full mt-3 no-wrap"):
                    if step > 1:
                        ui.button("Back", on_click=lambda: _goto(step - 1)) \
                            .props("outline no-caps").classes("grow")
                    if step == 4:
                        label = "Arm live" if wiz["mode"] == "live" else "Arm dry run"
                        b = ui.button(label, on_click=_arm).props("unelevated no-caps").classes("grow")
                        if wiz["mode"] == "live":
                            b.props("color=red")
                    else:
                        b = ui.button("Next", on_click=lambda: _goto(step + 1)) \
                            .props("unelevated no-caps").classes("grow")
                    b.set_enabled(can_next)

        # ------------------------------------------------------------------ #
        @ui.refreshable
        def body() -> None:
            theme.title("Arm")
            step = wiz["step"]
            if step == 0:
                _home()
                return

            with ui.row().classes("gap-1 flex-wrap"):
                for i, name in enumerate(_STEPS, 1):
                    lbl = ui.label(f"0{i} {name}").classes("rd-lab px-2 py-1 rounded")
                    if i == step:
                        lbl.style("color:var(--rd-ink);border:1px solid var(--rd-ink-dim)")
                    elif i < step:
                        lbl.style("color:var(--rd-ok);border:1px solid var(--rd-ok)")
                    else:
                        lbl.style("border:1px solid var(--rd-line-soft)")

            with ui.row().classes("w-full no-wrap items-start gap-5"):
                with ui.column().classes("grow min-w-0"):
                    (_step_retailers, _step_items, _step_time, _step_confirm)[step - 1]()
                with ui.column().classes("shrink-0").style("width:18rem"):
                    panel()

        body()
