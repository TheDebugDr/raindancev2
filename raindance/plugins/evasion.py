"""Evasion settings tool — toggle stealth + proxies used during checkout/login.

When enabled, BrowserFactory applies playwright-stealth, sticky proxies (if any
are configured), and a realistic fingerprint on every browser launch used by
the checkout engine and login session.
"""
from __future__ import annotations

from pathlib import Path

from nicegui import run, ui

from raindance.core.registry import ToolPlugin, register_tool
from raindance.evasion.proxy_manager import ProxyManager, mask as proxy_mask


@register_tool
class EvasionTool(ToolPlugin):
    id = "evasion"
    name = "Evasion"
    icon = "visibility_off"
    order = 35
    description = "Stealth + proxy layer for browser checkout sessions."

    @staticmethod
    def bus_log(ctx, msg: str, level: str = "info") -> None:
        bus = getattr(ctx, "bus", None)
        if bus is not None:
            try:
                bus.log(msg, level)
            except Exception:
                pass

    def render(self, ctx) -> None:
        d = ctx.settings.data
        evasion = d.setdefault("evasion", {
            "enabled": False,
            "sticky": True,
            "account_id": "default",
            "proxies_file": "proxies.txt",
            "proxies": [],
        })

        ui.label("Evasion").classes("text-2xl font-bold")
        ui.markdown(
            "**Phase 0/3** — stealth + proxies for every task browser session. "
            "When enabled, TaskRunner launches Playwright with playwright-stealth, "
            "a sticky proxy from the task's proxy group, and a profile fingerprint. "
            "HTTP monitoring (Phase 2) uses proxies lightly when available."
        ).classes("opacity-70")

        status = ui.label("").classes("text-sm")
        # "healthy" only means "not known-bad", so don't present it as a measured
        # result until Test reachability has actually run.
        _probed = {"done": False}

        def _refresh_status():
            pm = ctx.proxy_manager
            n = pm.count if pm else 0
            probed = bool(getattr(pm, "_bad", None)) or _probed["done"]
            healthy = pm.healthy_count if pm and hasattr(pm, "healthy_count") else n
            on = bool(evasion.get("enabled"))
            bits = [
                "● ENABLED" if on else "○ disabled",
                (f"{healthy}/{n} proxy(ies) reachable" if probed
                 else f"{n} proxy(ies) loaded (not probed)") if n
                else "0 proxies loaded",
                f"account: {evasion.get('account_id') or 'default'}",
            ]
            bf = ctx.browser_factory
            last = getattr(bf, "last_stealth", "") or ""
            if last:
                bits.append("stealth: " + ("NOT APPLIED" if "NOT APPLIED" in last
                                           else "applied"))
            # Both of these describe a launch that has ALREADY happened - the
            # factory only fills them in after one - so they are self-gating and
            # cannot claim a result nothing measured. The _probed["done"] gate
            # above stays exactly as it was: it guards the PROXY reachability
            # claim, which is a different measurement.
            engine_label = getattr(bf, "last_engine", "") or ""
            if engine_label:
                bits.append(f"engine: {engine_label}")
            # verify_summary is its own field on the newer factory's session
            # record; on the older one the same verdict is only ever appended to
            # last_stealth, so read it from there rather than losing it.
            verdict = getattr(getattr(bf, "last_session", None),
                              "verify_summary", "") or ""
            if not verdict and " · verify " in last:
                verdict = last.split(" · verify ", 1)[1].strip()
            if verdict:
                bits.append("verify: " + ("passed" if "FAILED" not in verdict
                                          else verdict))
            if on and n == 0:
                bits.append("(stealth only — no proxies file)")
            status.set_text(" · ".join(bits))
            status.classes(
                replace="text-sm text-green-400" if on else "text-sm opacity-70"
            )

        with ui.card().classes("w-full"):
            ui.label("Master switch").classes("font-semibold")
            sw = ui.switch(
                "Enable evasion — master switch. Off disables stealth + proxies "
                "for every run; individual rows in the wizard can only turn it "
                "off, not on.",
                value=bool(evasion.get("enabled", False)),
            )

            def _toggle():
                evasion["enabled"] = bool(sw.value)
                ctx.settings.save()
                _apply_to_factory()
                _probed["done"] = False
                _render_pool()
                ui.notify(
                    f"Evasion {'enabled' if sw.value else 'disabled'}",
                    type="positive" if sw.value else "info",
                )
                _refresh_status()

            sw.on_value_change(lambda e: _toggle())

            strict_sw = ui.switch(
                "Strict — fail the launch if stealth cannot be applied",
                value=bool(getattr(ctx.browser_factory, "strict_evasion", False)),
            )
            ui.label(
                "Off, a launch that cannot be patched still runs — the session "
                "looks protected but is not. On, it raises instead."
            ).classes("text-xs opacity-60")

            def _toggle_strict():
                if ctx.browser_factory is not None:
                    ctx.browser_factory.configure(strict_evasion=bool(strict_sw.value))
                evasion["strict"] = bool(strict_sw.value)
                ctx.settings.save()
                _refresh_status()

            strict_sw.on_value_change(lambda e: _toggle_strict())

        with ui.card().classes("w-full"):
            ui.label("Identity / sticky proxy").classes("font-semibold")
            ui.label(
                "Sticky mode pins the same proxy to an account id across runs "
                "(useful so a retailer sees a consistent IP)."
            ).classes("text-xs opacity-60 mb-2")
            account_i = ui.input(
                "Account ID",
                value=evasion.get("account_id") or "default",
            ).classes("w-full")
            sticky_sw = ui.switch(
                "Sticky proxy per account",
                value=bool(evasion.get("sticky", True)),
            )
            file_i = ui.input(
                "Proxies file",
                value=evasion.get("proxies_file") or "proxies.txt",
            ).classes("w-full")
            ui.label(
                "One proxy per line. Formats: host:port · host:port:user:pass · "
                "http://user:pass@host:port · socks5://host:port"
            ).classes("text-xs opacity-50")

            proxies_path = Path(evasion.get("proxies_file") or "proxies.txt")
            sample = ""
            if ctx.proxy_manager is not None and ctx.proxy_manager.count:
                sample = ctx.proxy_manager.as_lines()
            elif proxies_path.exists():
                sample = proxies_path.read_text()[:20000]
            proxies_ta = ui.textarea(
                "Proxy list (saved to the file above)",
                value=sample,
            ).classes("w-full").props("rows=7 spellcheck=false")

            feedback = ui.column().classes("w-full gap-1")
            pool_list = ui.column().classes("w-full gap-1")
            http_results = ui.column().classes("w-full gap-1")

            def _render_pool():
                pool_list.clear()
                pm = ctx.proxy_manager
                if pm is None or not pm.proxies:
                    return
                with pool_list:
                    for pr in pm.proxies:
                        bad = pm.is_bad(pr) if hasattr(pm, "is_bad") else False
                        holder = ""
                        if hasattr(pm, "sticky_for"):
                            for aid in list(getattr(pm, "_sticky_map", {})):
                                if pm.sticky_for(aid) is pr:
                                    holder = aid
                                    break
                        with ui.row().classes("items-center gap-3 w-full"):
                            ui.label(proxy_mask(pr)).classes("rd-mono text-xs") \
                                .style("flex-grow:1;color:var(--rd-ink)")
                            if holder:
                                ui.label(f"sticky → {holder}").classes(
                                    "rd-mono text-xs opacity-50")
                            ui.label("Unreachable" if bad else "Ready").classes(
                                "rd-chip " + ("rd-error" if bad else "rd-in_stock"))

            def _report(kept, rejected):
                feedback.clear()
                with feedback:
                    if rejected:
                        def _redact(line: str) -> str:
                            # host:port:user:pass and URL forms both carry secrets.
                            # The URL form (scheme://user:pass@host:port) must be
                            # handled first: splitting it on ":" masks the PORT and
                            # leaves the password on screen.
                            if "@" in line:
                                head, tail = line.split("@", 1)
                                scheme, sep, cred = head.rpartition("//")
                                if ":" in cred:
                                    cred = cred.split(":", 1)[0] + ":****"
                                line = scheme + sep + cred + "@" + tail
                            else:
                                parts = line.split(":")
                                if len(parts) >= 4:
                                    parts[3] = "****"
                                    line = ":".join(parts[:4])
                            return line[:40]
                        shown = ", ".join(f"`{_redact(r)}`" for r in rejected[:4])
                        more = "…" if len(rejected) > 4 else ""
                        ui.markdown(
                            f"**Ignored {len(rejected)} unparseable "
                            f"line{'' if len(rejected) == 1 else 's'}:** {shown}{more}"
                        ).classes("text-xs").style("color:var(--rd-accent)")
                    ui.label(f"{kept} proxy(ies) loaded and live.").classes(
                        "text-xs opacity-60")

            def _save_proxies_file() -> bool:
                """Returns True only if the pool was saved. Callers must check."""
                path = Path((file_i.value or "proxies.txt").strip() or "proxies.txt")
                file_i.set_value(str(path))
                evasion["proxies_file"] = str(path)
                pm = ctx.proxy_manager
                if pm is None:
                    path.write_text(proxies_ta.value or "")
                    ui.notify(f"Wrote {path}", type="positive")
                    return True
                before = pm.as_lines()
                kept, rejected = pm.replace_lines(proxies_ta.value or "")
                try:
                    pm.save_to(path)
                except (OSError, ValueError) as exc:
                    # Put the pool back: a half-applied edit that the UI does not
                    # show is worse than no edit at all.
                    pm.replace_lines(before)
                    _report(pm.count, rejected)
                    _render_pool()
                    _refresh_status()
                    ui.notify(str(exc) if isinstance(exc, ValueError)
                              else f"Could not write {path}: {exc}",
                              type="negative", multi_line=True)
                    return False
                proxies_ta.set_value(pm.as_lines())
                _probed["done"] = False
                ctx.settings.save()
                _report(kept, rejected)
                _render_pool()
                _refresh_status()
                ui.notify(f"{kept} proxies live" +
                          (f", {len(rejected)} ignored" if rejected else ""),
                          type="positive")
                return True

            async def _test_proxies():
                pm = ctx.proxy_manager
                if pm is None or not pm.proxies:
                    ui.notify("No proxies to test", type="warning")
                    return
                total = pm.count
                ui.notify(f"Testing {total} proxies…", type="info")
                ok = await run.io_bound(pm.check_all)
                _probed["done"] = True

                # Also probe the pools TASK RUNS actually pull from - the
                # orchestrator's proxy groups, which are separate objects from
                # this editable standalone pool.
                groups = {}
                pg = getattr(getattr(ctx, "orchestrator", None), "proxy_groups", None)
                if pg is not None and hasattr(pg, "check_all"):
                    groups = await run.io_bound(pg.check_all)

                _render_pool()
                _refresh_status()
                lines = [f"editable pool: {ok}/{total} reachable"]
                for gname, (g_ok, g_total) in groups.items():
                    lines.append(f"group '{gname}': {g_ok}/{g_total}")
                    self.bus_log(ctx, f"[evasion] group '{gname}': "
                                      f"{g_ok}/{g_total} reachable")
                all_ok = ok == total and all(g == t for g, t in groups.values())
                ui.notify(" · ".join(lines),
                          type="positive" if all_ok else "warning",
                          multi_line=True)

            # ---- HTTP exit probe (newer ProxyManager only) --------------- #
            # check_http() drives the whole path a launch depends on - CONNECT
            # tunnel, TLS, a response body - and reports the exit IP/geo.
            # OPTIONAL: it arrived with a newer evasion layer, so it is detected
            # on the live manager when there is one and on the imported class
            # otherwise, and the button is disabled (not hidden) with a tooltip
            # saying why when the installed manager has no such method.
            _probe_target = (ctx.proxy_manager if ctx.proxy_manager is not None
                             else ProxyManager)
            _has_http_probe = hasattr(_probe_target, "check_http")

            def _scrub(text: str, proxy: dict) -> str:
                """Never let a proxy's own credentials reach the screen.

                The manager's summary is clean, but its `error` half is raw
                exception text and a urllib failure can carry the whole proxy
                URL, password included.
                """
                out = str(text or "")
                for secret in (proxy.get("password"), proxy.get("username")):
                    if secret and len(str(secret)) > 1:
                        out = out.replace(str(secret), "***")
                return out

            def _http_pools():
                """(label, manager) for the editable pool and every named group."""
                pools = [("editable pool", ctx.proxy_manager)]
                pg = (getattr(ctx, "proxy_groups", None)
                      or getattr(getattr(ctx, "orchestrator", None),
                                 "proxy_groups", None))
                if pg is not None and hasattr(pg, "group_names"):
                    for gname in pg.group_names():
                        pools.append((f"group '{gname}'", pg.get_manager(gname)))
                return pools

            def _http_targets():
                """Every distinct proxy to probe; the first pool carrying it wins.

                Deduped by masked identity: the default group is usually the same
                file as the editable pool, and every probe is a real network
                round-trip - probing one exit twice buys nothing.
                """
                out, seen = [], set()
                for label, mgr in _http_pools():
                    if mgr is None or not hasattr(mgr, "check_http"):
                        continue
                    for pr in list(getattr(mgr, "proxies", None) or []):
                        key = proxy_mask(pr)
                        if key in seen:
                            continue
                        seen.add(key)
                        out.append((label, key, mgr, pr))
                return out

            def _probe_http_blocking(targets):
                """One real HTTPS round-trip per proxy. Worker thread ONLY."""
                rows = []
                for label, key, mgr, pr in targets:
                    try:
                        res = mgr.check_http(pr)
                    except Exception as exc:  # noqa: BLE001 - one bad proxy
                        res = {"ok": False, "stage": "error",
                               "summary": f"{type(exc).__name__}: {exc}"}
                    if not isinstance(res, dict):
                        res = {"ok": False, "stage": "error",
                               "summary": "check_http returned "
                                          f"{type(res).__name__}, not an object"}
                    rows.append((label, key, {
                        "ok": bool(res.get("ok")),
                        "stage": str(res.get("stage") or "?"),
                        "status": res.get("status"),
                        "latency_ms": res.get("latency_ms"),
                        "exit_ip": str(res.get("exit_ip") or ""),
                        "geo": str(res.get("geo") or ""),
                        "summary": _scrub(res.get("summary"), pr),
                    }))
                return rows

            async def _test_exit_http():
                if not _has_http_probe:
                    ui.notify("The installed ProxyManager has no check_http()",
                              type="warning")
                    return
                targets = _http_targets()
                if not targets:
                    ui.notify("No proxies to probe", type="warning")
                    return
                ui.notify(f"Probing {len(targets)} exit(s) over HTTP…",
                          type="info")
                # Blocking network calls - off the UI thread, exactly the way
                # Test reachability runs check_all.
                rows = await run.io_bound(_probe_http_blocking, targets)
                ok_n = sum(1 for _, _, r in rows if r["ok"])
                http_results.clear()
                with http_results:
                    ui.label(f"HTTP exit probe — {ok_n}/{len(rows)} exit(s) "
                             "carried traffic").classes("text-xs font-semibold")
                    for label, key, r in rows:
                        detail = [f"stage={r['stage']}"]
                        if r["latency_ms"] not in (None, ""):
                            detail.append(f"{r['latency_ms']}ms")
                        if r["status"]:
                            detail.append(f"HTTP {r['status']}")
                        if r["exit_ip"] or r["geo"]:
                            detail.append(
                                "exit " + (r["exit_ip"] or "?")
                                + (f" [{r['geo']}]" if r["geo"] else ""))
                        with ui.row().classes("items-center gap-3 w-full"):
                            ui.label(key).classes("rd-mono text-xs") \
                                .style("flex-grow:1;color:var(--rd-ink)")
                            ui.label(label).classes("rd-mono text-xs opacity-50")
                            ui.label(" · ".join(detail)).classes(
                                "rd-mono text-xs opacity-70")
                            ui.label("OK" if r["ok"] else "FAIL").classes(
                                "rd-chip " + ("rd-in_stock" if r["ok"]
                                              else "rd-error"))
                        if r["summary"]:
                            ui.label(r["summary"]).classes(
                                "text-xs opacity-60 pl-2")
                        self.bus_log(
                            ctx,
                            f"[evasion] exit probe {key} ({label}): "
                            f"{r['summary'] or r['stage']}",
                            "info" if r["ok"] else "warn",
                        )
                # Deliberately NOT touching _probed["done"]: that gates the
                # "N/M reachable" claim, which counts proxies check_all marked
                # bad. check_http never marks one bad, so letting it flip that
                # gate would show an unmeasured healthy count as measured.
                _refresh_status()
                ui.notify(f"HTTP exit probe: {ok_n}/{len(rows)} ok",
                          type="positive" if ok_n == len(rows) else "warning")

            with ui.row().classes("gap-2"):
                ui.button("Save & apply", icon="save",
                          on_click=_save_proxies_file).props("outline")
                ui.button("Test reachability", icon="network_check",
                          on_click=_test_proxies).props("flat")
                _http_btn = ui.button("Test exit (HTTP)", icon="travel_explore",
                                      on_click=_test_exit_http).props("flat")
                if not _has_http_probe:
                    _http_btn.disable()
                    _http_btn.tooltip(
                        "The installed ProxyManager has no check_http() — "
                        "update the evasion layer to enable this")
                else:
                    _http_btn.tooltip(
                        "Sends a real request through each proxy: CONNECT "
                        "tunnel, TLS, response body, and the exit IP it came "
                        "out of. Slower than reachability.")

            _render_pool()

        with ui.card().classes("w-full"):
            ui.label("Test launch").classes("font-semibold")
            ui.label(
                "Opens a browser via BrowserFactory (respects the toggle) and "
                "navigates to amiunique.org so you can inspect the fingerprint."
            ).classes("text-xs opacity-60 mb-2")

            def _test_launch():
                _persist_fields()
                _apply_to_factory()
                profile = d.get("browser", {}).get("user_data_dir") or "profile"
                try:
                    ctx.engine.open_login(
                        "https://amiunique.org",
                        profile,
                        headless=False,
                    )
                    ui.notify("Browser launching…", type="info")
                except Exception as e:  # noqa: BLE001
                    ui.notify(f"Launch failed: {e}", type="negative")

            ui.button(
                "Launch test browser",
                icon="travel_explore",
                color="primary",
                on_click=_test_launch,
            )

        def _persist_fields():
            evasion["enabled"] = bool(sw.value)
            evasion["sticky"] = bool(sticky_sw.value)
            evasion["account_id"] = (account_i.value or "default").strip() or "default"
            evasion["proxies_file"] = (file_i.value or "proxies.txt").strip() or "proxies.txt"
            ctx.settings.save()

        def _apply_to_factory():
            """Push current settings into the live factory + proxy manager."""
            if ctx.proxy_manager is not None:
                ctx.proxy_manager.reload_from_settings(evasion)
            if ctx.browser_factory is not None:
                ctx.browser_factory.configure(
                    evasion_enabled=bool(evasion.get("enabled")),
                    proxy_manager=ctx.proxy_manager,
                )
            # Engine keeps a reference to the same factory instance.
            if getattr(ctx, "engine", None) is not None:
                ctx.engine.browser_factory = ctx.browser_factory
                ctx.engine.account_id = evasion.get("account_id") or "default"

        def _save_all():
            # Route the textarea through the same validating path as Save & apply.
            if proxies_ta.value is not None and not _save_proxies_file():
                return          # the proxy save failed - do not claim success
            _persist_fields()
            _apply_to_factory()
            _refresh_status()
            ui.notify("Evasion settings saved", type="positive")

        ui.button("Save evasion settings", icon="save", color="primary", on_click=_save_all)
        _refresh_status()
