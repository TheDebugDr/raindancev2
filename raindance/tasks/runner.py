"""Phase 3–8 — single-task browser session: launch → ATC → checkout → cleanup."""
from __future__ import annotations

import traceback
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from raindance.core import retailer_config as RC
from raindance.evasion.browser_factory import BrowserFactory
from raindance.evasion.fingerprint_manager import FingerprintManager
from raindance.sites import get_handler
from raindance.tasks import models as M
from raindance.core.leases import LEASES, CrossProcessGuard


class TaskRunner:
    """Runs one task end-to-end on its own thread (own Playwright instance)."""

    def __init__(
        self,
        *,
        bus,
        hub,
        task_store,
        profile_store,
        proxy_groups,
        captcha,
        settings,
        should_stop: Callable[[], bool],
        on_status: Optional[Callable[[str, str], None]] = None,
    ):
        self.bus = bus
        self.hub = hub
        self.task_store = task_store
        self.profile_store = profile_store
        self.proxy_groups = proxy_groups
        self.captcha = captcha
        self.settings = settings
        self.should_stop = should_stop
        self.on_status = on_status  # (task_id, status)

    def _set(self, tid: str, status: str, **extra) -> None:
        self.task_store.set_runtime(tid, status=status, **extra)
        if self.on_status:
            try:
                self.on_status(tid, status)
            except Exception:
                pass

    def run(self, task: dict) -> dict:
        """Execute Phases 3–8. Returns result dict."""
        tid = task["id"]
        attempt = int(task.get("attempt") or 0) + 1
        self.task_store.set_runtime(tid, attempt=attempt)
        profile_id = task.get("profile_id") or ""
        profile = self.profile_store.get(profile_id) if profile_id else None
        if not profile:
            profile = self.profile_store.ensure_default()
            self.bus.log(
                f"[{tid}] no profile_id — using {profile.get('id')}", "warn"
            )

        # One running task per profile. The profile owns a persistent Chromium
        # user_data_dir — cookies, session AND cart — plus one card and one
        # address. Two tasks sharing it are not two independent attempts; they
        # are two drivers of one cart, and whichever reaches place-order buys
        # what both of them added. Tasks on DIFFERENT profiles are unaffected
        # and still run fully in parallel.
        lease_wait = float((self.settings.data.get("checkout") or {})
                           .get("profile_lease_wait_seconds", 90))
        lease_held = profile.get("id") or ""
        if not LEASES.acquire(lease_held, tid, timeout=lease_wait):
            holder = LEASES.holder(lease_held)
            err = (f"profile {lease_held} is in use by task {holder or '(unknown)'} "
                   f"after waiting {lease_wait:.0f}s — refusing to share a cart")
            self.bus.log(f"[{tid}] {err}", "warn")
            self._set(tid, M.STATUS_FAILED, last_error=err, message="profile busy")
            return {"ok": False, "order_id": "", "error": err, "task_id": tid,
                    "profile_busy": True}

        site = task.get("site") or "generic"
        # Retailer config resolves the optimal setup for this site — the task
        # already carries the arm-time proxy_group, and evasion/headless/captcha
        # come from here so a retailer can enable evasion without the global switch.
        group = task.get("proxy_group") or RC.proxy_group_for(self.settings, site)
        session_key = f"{profile.get('id')}:{tid}"
        dry_run = bool(task.get("dry_run", True))
        if (self.settings.data.get("checkout") or {}).get("force_dry_run"):
            dry_run = True

        browser_cfg = self.settings.data.get("browser") or {}
        headless = RC.headless_for(self.settings, site, bool(browser_cfg.get("headless", False)))
        # CAPTCHA manual solve needs a visible window
        cap_mode = RC.captcha_mode_for(self.settings, site, self.captcha.mode)
        # Master switch rule: the Evasion page's master switch off means off
        # everywhere — no retailer default and no per-row setting can turn it
        # back on. RC.evasion_enabled_for() lets a retailer's own `enabled`
        # take precedence over the global switch, so the master is ANDed in here.
        master = bool((self.settings.data.get("evasion") or {}).get("enabled"))
        evasion_on = master and RC.evasion_enabled_for(self.settings, site)
        # The wizard's per-listing controls (execute_queue.materialize writes
        # site_params.evasion only when False / site_params.captcha_mode) can
        # only turn evasion OFF for a row; an absent key inherits the above.
        sp = task.get("site_params") or {}
        if "evasion" in sp and sp["evasion"] is not None:
            row_on = bool(sp["evasion"])
            if not row_on:
                evasion_on = False
                self.bus.log(
                    f"[{tid}] per-task override — evasion=off",
                    "info",
                )
            elif not master:
                self.bus.log(
                    f"[{tid}] evasion master switch is off — row setting ignored",
                    "info",
                )
        if sp.get("captcha_mode"):
            cap_mode = str(sp["captcha_mode"])
            self.bus.log(
                f"[{tid}] per-task override — captcha_mode={cap_mode}", "info"
            )
        if cap_mode == "manual" and headless:
            self.bus.log(
                f"[{tid}] forcing headed browser for manual CAPTCHA", "warn"
            )
            headless = False

        proxy_mgr = self.proxy_groups.get_manager(group)
        seed = profile.get("fingerprint_seed") or None
        fp = FingerprintManager(seed=int(seed) if seed else None)
        _ev = (self.settings.data.get("evasion") or {})
        factory = BrowserFactory(
            proxy_manager=proxy_mgr,
            fingerprint_manager=fp,
            evasion_enabled=evasion_on,
            # Without these two the task path could not report a stealth failure:
            # no bus meant the warning went to a logger nobody configured, and no
            # strict flag meant the Evasion page's Strict switch never applied here.
            strict_evasion=bool(_ev.get("strict", False)),
            bus=self.bus,
        )

        user_data_dir = profile.get("user_data_dir") or None
        if user_data_dir == "":
            user_data_dir = None

        # Forensics snapshot — the evasion layer's actual launch state, filled
        # in from the per-context session record below. Rides along to every
        # denial/failure record. Observation only; it changes no behavior.
        from raindance.core import forensics as _F
        ev_snapshot = _F.evasion_snapshot(
            evasion_on=evasion_on, proxy=None, fingerprint="n/a",
            headless=headless)
        self._set(tid, M.STATUS_LAUNCHING, message="creating browser session")
        self.bus.log(
            f"[{tid}] Phase 3 — launch site={site} "
            f"(evasion={'ON' if evasion_on else 'off'}, proxy_group={group}, "
            f"proxies={proxy_mgr.count if proxy_mgr else 0}, profile={profile.get('name')})",
            "info",
        )
        self.bus.log(f"[{tid}] retailer config — {RC.describe(self.settings, site)}", "info")

        result = {"ok": False, "order_id": "", "error": "", "task_id": tid}
        browser = context = page = None

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            err = "Playwright not installed"
            self._set(tid, M.STATUS_FAILED, last_error=err, message=err)
            return {**result, "error": err}

        try:
            with sync_playwright() as p:
                harvest = bool((task.get("site_params") or {}).get("harvester_proxies"))
                browser, context, page = factory.create_context(
                    p,
                    account_id=session_key,
                    headless=headless,
                    user_data_dir=user_data_dir,
                    slow_mo=int(browser_cfg.get("slow_mo_ms") or 0),
                    viewport=browser_cfg.get("viewport"),
                    for_harvester=harvest,
                    # Labels for the per-launch JSONL verdict (launch_log.py).
                    task_id=tid,
                    site=site,
                    proxy_group=group,
                )
                if evasion_on:
                    # Prefer the per-CONTEXT session record over the factory's
                    # last_* attributes. last_* belong to the factory, not to a
                    # launch: with several tasks in flight they race, and this
                    # task can be handed another task's proxy/fingerprint. The
                    # session object is attached to the context this task owns,
                    # so a concurrent launch cannot overwrite it.
                    # A factory without session_info() (the older evasion layer)
                    # leaves sess None and every read below falls back to the
                    # exact last_* attribute it used before.
                    sess = (factory.session_info(context)
                            if hasattr(factory, "session_info") else None)
                    used = sess.proxy if sess is not None else factory.last_proxy
                    applied = (sess.stealth if sess is not None
                               else factory.last_stealth) or ""
                    fingerprint = (sess.fingerprint if sess is not None
                                   else factory.last_fingerprint) or "n/a"
                    # Kept verbatim: the stealth-failure warning keys off this
                    # substring, and last_stealth / session.stealth both carry
                    # "NOT APPLIED" the same way.
                    unpatched = "NOT APPLIED" in applied
                    # Forensics: what this launch actually exited on and which
                    # fingerprint it wore. (The two bus.log lines above keep
                    # their exact text — nothing about the launch changes.)
                    ev_snapshot["proxy"] = _F.mask_proxy(used)
                    ev_snapshot["fingerprint"] = fingerprint
                    self.bus.log(
                        f"[{tid}] "
                        + ("stealth NOT APPLIED — this session is not patched"
                           if unpatched else "stealth on")
                        + f" — fingerprint={fingerprint}, "
                        f"exit={(used or {}).get('server', 'direct (no proxy in pool)')}",
                        "warn" if unpatched else "info",
                    )
                    # Detail the older factory does not record at all. Sourced
                    # from the session ONLY, so on the older layer this block is
                    # skipped whole and the log stays exactly what it always was.
                    if sess is not None:
                        engine_label = getattr(sess, "engine", "") or ""
                        if engine_label:
                            self.bus.log(
                                f"[{tid}] engine — {engine_label}", "info")
                        verify_summary = getattr(sess, "verify_summary", "") or ""
                        if verify_summary:
                            self.bus.log(
                                f"[{tid}] stealth verify — {verify_summary}",
                                "warn" if "FAILED" in verify_summary else "info",
                            )
                        health = getattr(sess, "proxy_health", None)
                        if isinstance(health, dict) and health:
                            h_summary = str(health.get("summary") or "").strip()
                            if h_summary:
                                self.bus.log(
                                    f"[{tid}] proxy health — {h_summary}",
                                    "info" if health.get("ok") else "warn",
                                )
                        for warning in (getattr(sess, "warnings", None) or []):
                            self.bus.log(
                                f"[{tid}] evasion warning — {warning}", "warn")
                handler = get_handler(
                    task.get("site") or "generic",
                    bus=self.bus,
                    captcha=self.captcha,
                )

                def status_cb(st: str):
                    self._set(tid, st, message=st)

                # Phase 4. For Target specifically, listen for the product-data
                # response this SAME navigation already triggers — target.com's
                # own frontend fetches price/stock same-origin, and riding on
                # that beats a second, independent request: no key, no extra
                # round trip, and no separate origin for Target's bot
                # protection to challenge. See raindance/sites/target_api.py
                # for why the old direct redsky.target.com call stopped working.
                target_captures: list = []
                if (site or "").lower() == "target":
                    from raindance.sites import target_api
                    with target_api.capture(page) as target_captures:
                        handler.navigate(page, task)
                        # The task URL can be a retailer SEARCH page (the
                        # catalog had no exact listing for this set + line +
                        # store), and a retired PDP redirects to search
                        # results. Either way the gate below would refuse a
                        # results page — correctly, it has no single price,
                        # seller or buy box. Resolve the exact product page
                        # now, while the listener is still attached so the
                        # final PDP's product-data responses are captured too.
                        _terminal = self._resolve_search_landing(
                            page, task, site, tid, attempt, ev_snapshot)
                        if _terminal is not None:
                            return _terminal
                        # navigate() only waits for domcontentloaded; the
                        # product-data requests this is listening for fire
                        # later, via client-side JS after hydration. Verified
                        # live: they land within ~2-4s. The listener has to
                        # still be attached when they do.
                        page.wait_for_timeout(2500)
                else:
                    handler.navigate(page, task)
                    _terminal = self._resolve_search_landing(
                        page, task, site, tid, attempt, ev_snapshot)
                    if _terminal is not None:
                        return _terminal
                if self.should_stop():
                    self._set(tid, M.STATUS_STOPPED, message="stopped")
                    return {**result, "error": "stopped"}

                if not handler.handle_queue(
                    page, task, should_stop=self.should_stop, on_status=status_cb
                ):
                    err = "queue failed or timed out"
                    self._fail(
                        tid, page, err,
                        forensics=self._forensics_bundle(
                            tid=tid, task=task, page=page, site=site,
                            attempt=attempt, kind="failed", classification="",
                            detector="", action="failed_at_queue",
                            outcome="failed:queue", ev_snapshot=ev_snapshot,
                        ),
                    )
                    return {**result, "error": err}

                if self.should_stop():
                    self._set(tid, M.STATUS_STOPPED, message="stopped")
                    return {**result, "error": "stopped"}

                # -- Seller + MSRP gate --------------------------------------- #
                # Fail safe: the ONLY thing that may proceed to a purchase is a
                # listing sold by the retailer at/near MSRP, confirmed on the
                # browser-rendered page (past whatever the site returned). A
                # third-party, over-MSRP, blocked, or unverifiable page aborts
                # here, before anything is added to the cart.
                from raindance.core import classify as C
                try:
                    body = page.content()
                except Exception:
                    body = ""

                # Retailer API signals, where the retailer has one. This is not
                # an optimisation — on a live Target PDP the price is NOT in the
                # HTML at all (it is fetched client-side from RedSky), so the
                # browser body alone yields no anchored price and the gate can
                # only ever return "unverified". The monitor already read these
                # signals; the gate never did, which left the purchase decision
                # running on weaker evidence than the alert that triggered it.
                signals = None
                if (site or "").lower() == "target":
                    from raindance.sites import target_api
                    try:
                        signals = target_api.signals_from_captures(target_captures)
                    except Exception as e:
                        self.bus.log(f"[{tid}] target signal parse failed: {e}", "warn")
                        signals = None
                    if signals:
                        self.bus.log(
                            f"[{tid}] target session data → price={signals.get('price')} "
                            f"stock={signals.get('stock')} "
                            f"(seller left to HTML detection)", "info")
                    else:
                        self.bus.log(
                            f"[{tid}] no target product-data response captured "
                            f"during navigation — falling back to the HTML path",
                            "warn")

                verdict = C.classify(
                    body, task.get("url") or "",
                    {"name": task.get("name"), "url": task.get("url")},
                    source="browser", handler=handler, settings=self.settings,
                    signals=signals,
                )
                gate_ok = verdict["status"] == C.IN_STOCK_RETAIL
                self.bus.log(
                    f"[{tid}] retail/MSRP gate → {verdict['status']}: {verdict['reason']}",
                    "hit" if gate_ok else "warn",
                )
                self.bus.log(
                    f"[{tid}] gate evidence — price={verdict.get('price')} "
                    f"via {verdict.get('price_source')} "
                    f"(anchored={verdict.get('price_scoped')}) · "
                    f"tier={verdict.get('sku_type')}/{verdict.get('era')} "
                    f"msrp={verdict.get('msrp')} ceiling={verdict.get('threshold')} "
                    f"[{verdict.get('tier_confidence')}] · "
                    f"seller={verdict.get('seller') or '-'} "
                    f"official={verdict.get('is_official')}", "info",
                )
                if not gate_ok:
                    err = f"aborted before add-to-cart — {verdict['reason']}"
                    # A deliberate skip, not a transient failure — do not retry.
                    # It still ends the run and closes the browser, so it has to
                    # announce itself as loudly as any other terminal outcome.
                    shot = self._deny(
                        tid, page, err, verdict,
                        forensics=self._forensics_bundle(
                            tid=tid, task=task, page=page, site=site,
                            attempt=attempt,
                            kind=("bot_wall" if verdict["status"] == C.BLOCKED
                                  else "gate_denied"),
                            classification=verdict["status"],
                            detector=(C.wall_match(body)
                                      if verdict["status"] == C.BLOCKED else ""),
                            action="aborted_before_add_to_cart",
                            outcome=f"denied:{verdict['status']}",
                            ev_snapshot=ev_snapshot,
                        ),
                    )
                    return {**result, "error": err, "aborted_gate": True,
                            "denied": True, "screenshot": shot}

                # How far a dry run rehearses. The default stops HERE, before
                # anything is added to a cart.
                #
                # This used to run add-to-cart, enter checkout and type the card
                # in before returning "DRY-RUN OK". The browser profile is
                # persistent, so every rehearsal left another unit in a cart
                # that nothing ever clears, and each retry added another. The
                # gate authorises ONE unit at one price; place-order buys
                # whatever the cart accumulated.
                #
                #   gate     (default) stop after the buy gate — touches nothing
                #   cart     add to cart, then stop (for checking ATC selectors)
                #   checkout fill the checkout form, then stop (the old behaviour)
                depth = str((self.settings.data.get("checkout") or {})
                            .get("dry_run_depth", "gate")).lower()
                if dry_run and depth not in ("cart", "checkout"):
                    self.bus.log(
                        f"[{tid}] DRY-RUN complete at the gate — verdict was "
                        f"{verdict['status']}; nothing added to the cart. "
                        f"Set checkout.dry_run_depth to 'cart' or 'checkout' to "
                        f"rehearse further.", "ok")
                    self._set(tid, M.STATUS_SUCCESS, message="dry-run OK (gate)")
                    return {**result, "ok": True, "dry_run": True,
                            "dry_run_depth": "gate", "gate_status": verdict["status"]}

                # Start from an empty cart. Without this a rehearsal, a retry or
                # any manual browsing in this profile leaves units behind that a
                # later live run would pay for.
                try:
                    cleared = handler.clear_cart(page)
                    self.bus.log(
                        f"[{tid}] cart cleared ({cleared} item(s) removed)"
                        if cleared else
                        f"[{tid}] cart already empty", "info")
                except NotImplementedError:
                    self.bus.log(
                        f"[{tid}] this handler cannot clear the cart — a live run "
                        f"may pay for items left by an earlier attempt", "warn")
                except Exception as e:
                    self.bus.log(f"[{tid}] cart clear failed: {e}", "warn")

                # Phase 5
                if not handler.add_to_cart(
                    page, task, should_stop=self.should_stop, on_status=status_cb
                ):
                    err = "add to cart failed"
                    self._fail(
                        tid, page, err,
                        forensics=self._forensics_bundle(
                            tid=tid, task=task, page=page, site=site,
                            attempt=attempt, kind="failed", classification="",
                            detector="", action="failed_at_add_to_cart",
                            outcome="failed:add_to_cart",
                            ev_snapshot=ev_snapshot,
                        ),
                    )
                    return {**result, "error": err}

                if self.should_stop():
                    self._set(tid, M.STATUS_STOPPED, message="stopped")
                    return {**result, "error": "stopped"}

                # Phase 6-7. Before a live submit, the cart total has to agree
                # with what the gate approved. The gate judged ONE listing at one
                # unit price; nothing downstream has ever looked at what is
                # actually in the basket.
                if not dry_run:
                    approved = verdict.get("price")
                    qty = int(task.get("quantity") or 1)
                    require = bool((self.settings.data.get("checkout") or {})
                                   .get("require_cart_check", True))
                    try:
                        total = handler.read_cart_total(page)
                    except Exception as e:
                        total = None
                        self.bus.log(f"[{tid}] cart total unreadable: {e}", "warn")
                    if total is None:
                        msg = ("cart total could not be read, so it cannot be "
                               "checked against the approved price")
                        if require:
                            err = f"aborted before place-order — {msg}"
                            self.bus.log(f"[{tid}] {err}", "warn")
                            shot = self._deny(
                                tid, page, err, verdict,
                                forensics=self._forensics_bundle(
                                    tid=tid, task=task, page=page, site=site,
                                    attempt=attempt, kind="gate_denied",
                                    classification=verdict["status"],
                                    detector="",
                                    action="aborted_before_place_order",
                                    outcome=f"denied:{verdict['status']}",
                                    ev_snapshot=ev_snapshot,
                                ),
                            )
                            return {**result, "error": err, "aborted_gate": True,
                                    "denied": True, "screenshot": shot}
                        self.bus.log(f"[{tid}] {msg} (require_cart_check off)", "warn")
                    elif approved:
                        # 12% covers tax and shipping added at checkout; beyond
                        # that the basket is not what the gate priced.
                        ceiling = approved * qty * 1.12 + 15.0
                        if total > ceiling:
                            err = (f"aborted before place-order — cart total "
                                   f"${total:.2f} exceeds ${ceiling:.2f}, the most "
                                   f"the approved ${approved:.2f} x{qty} can come "
                                   f"to with tax and shipping. The basket holds "
                                   f"more than this task authorised.")
                            self.bus.log(f"[{tid}] {err}", "err")
                            shot = self._deny(
                                tid, page, err, verdict,
                                forensics=self._forensics_bundle(
                                    tid=tid, task=task, page=page, site=site,
                                    attempt=attempt, kind="gate_denied",
                                    classification=verdict["status"],
                                    detector="",
                                    action="aborted_before_place_order",
                                    outcome=f"denied:{verdict['status']}",
                                    ev_snapshot=ev_snapshot,
                                ),
                            )
                            return {**result, "error": err, "aborted_gate": True,
                                    "denied": True, "screenshot": shot}
                        self.bus.log(
                            f"[{tid}] cart check OK — ${total:.2f} against a "
                            f"${ceiling:.2f} ceiling for {qty} x ${approved:.2f}",
                            "ok")

                out = handler.checkout(
                    page,
                    task,
                    profile,
                    dry_run=dry_run,
                    should_stop=self.should_stop,
                    on_status=status_cb,
                )
                result.update(out or {})
                shot = self._screenshot(page, tid, "result")
                result.setdefault("screenshot", shot)

                if result.get("ok"):
                    msg = (
                        f"DRY-RUN OK" if result.get("dry_run")
                        else f"ORDER {result.get('order_id') or 'placed'}"
                    )
                    self._set(
                        tid, M.STATUS_SUCCESS,
                        order_id=result.get("order_id") or "",
                        message=msg,
                        last_error="",
                        denied=False,
                    )
                    self._notify_success(task, result)
                    self.bus.log(f"[{tid}] Phase 7 success — {msg}", "hit")
                else:
                    err = result.get("error") or "checkout failed"
                    self._fail(
                        tid, page, err,
                        forensics=self._forensics_bundle(
                            tid=tid, task=task, page=page, site=site,
                            attempt=attempt, kind="failed", classification="",
                            detector="", action="failed_at_checkout",
                            outcome="failed:checkout", ev_snapshot=ev_snapshot,
                        ),
                    )
                    result["error"] = err

        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            self.bus.log(f"[{tid}] error: {err}", "err")
            self.bus.log(traceback.format_exc()[-500:], "warn")
            shot = ""
            try:
                if page:
                    shot = self._screenshot(page, tid, "error")
            except Exception:
                pass
            # Failure forensics — observation only, never raises into the run.
            try:
                _F.log_failure(
                    settings=self.settings,
                    screenshot=shot,
                    **self._forensics_bundle(
                        tid=tid, task=task, page=page, site=site,
                        attempt=attempt, kind="error", classification="",
                        detector="", action="exception",
                        outcome=f"error:{type(e).__name__}",
                        ev_snapshot=ev_snapshot,
                    ),
                )
            except Exception:
                pass
            self._set(tid, M.STATUS_FAILED, last_error=err, message=err)
            self._notify_fail(task, err)
            result["error"] = err
        finally:
            # Phase 8 cleanup
            try:
                if context is not None:
                    context.close()
            except Exception:
                pass
            try:
                if browser is not None:
                    browser.close()
            except Exception:
                pass
            # Release the profile only after the browser that was using its
            # user_data_dir is closed, or the next task would attach to a
            # directory Chromium has not finished writing.
            try:
                LEASES.release(lease_held, tid)
            except Exception:
                pass
            # The cross-process guard (see leases.CrossProcessGuard) is a
            # separate lock file inside the persistent profile directory
            # itself — release() is a safe no-op if we never held it (a
            # non-persistent task, or one that never reached the launch).
            if user_data_dir:
                try:
                    CrossProcessGuard.release(user_data_dir)
                except Exception:
                    pass
            self.bus.log(f"[{tid}] Phase 8 cleanup complete", "info")
        return result

    def _deny(self, tid: str, page, err: str, verdict: dict | None = None,
               *, forensics: dict | None = None) -> str:
        """End a run because the gate refused the page.

        Previously this was the one terminal path that called _set directly:
        no screenshot, no notification. The browser closed and nothing said why,
        which reads as the window randomly vanishing. It now does what every
        other ending does, and says "denied" rather than "failed".

        When `forensics` is supplied it is written to the append-only failure
        log (raindance/core/forensics.py) AFTER the screenshot, so the shot
        path is part of the record. Pure observation — the denial itself is
        unchanged.
        """
        verdict = verdict or {}
        self._set(
            tid, M.STATUS_FAILED,
            last_error=err,
            message=f"DENIED — {verdict.get('reason') or err}",
            denied=True,
        )
        self.bus.log(f"[{tid}] DENIED — {err}", "warn")
        shot = ""
        try:
            shot = self._screenshot(page, tid, "denied")
            if shot:
                self.bus.log(f"[{tid}] page captured → {shot}", "info")
        except Exception as exc:  # noqa: BLE001 - never mask the denial itself
            self.bus.log(f"[{tid}] could not capture the denied page: {exc}", "warn")
        if forensics:
            try:
                from raindance.core import forensics as _F
                _F.log_failure(settings=self.settings, screenshot=shot,
                               **forensics)
            except Exception:
                pass  # logging must never mask or break the denial
        task = self.task_store.get(tid) or {"id": tid}
        self._notify_denied(task, err)
        return shot

    def _forensics_bundle(self, *, tid: str, task: dict, page, site: str,
                          attempt: int, kind: str, classification: str,
                          detector: str, action: str, outcome: str,
                          ev_snapshot: dict | None) -> dict:
        """Assemble one failure-forensics record (raindance/core/forensics.py).

        Pure observation: every page probe is guarded, and log_failure()
        itself never raises, so building the bundle can never break a run.
        """
        url = ""
        title = ""
        try:
            if page is not None:
                url = page.url or ""
        except Exception:
            pass
        try:
            if page is not None:
                title = (page.title() or "")[:200]
        except Exception:
            pass
        return {
            "kind": kind,  # bot_wall | gate_denied | failed | error
            "task_id": tid,
            "site": site or "",
            "url": url or (task.get("url") or ""),
            "title": title,
            "classification": classification or "",
            "detector": detector or "",
            "attempt": int(attempt or 0),
            "action": action,
            "outcome": outcome,
            "evasion": dict(ev_snapshot or {}),
        }

    def _resolve_search_landing(self, page, task: dict, site: str,
                                tid: str, attempt: int = 0,
                                ev_snapshot: dict | None = None):
        """Search results → the exact product page, or a terminal denial.

        Called right after navigation. Two ways to be on a results page: the
        task URL itself is a retailer search URL (the catalog had no exact
        listing for this set + line + store), or the PDP redirected there (a
        retired listing). A results page has no single price, seller or buy
        box, so the MSRP gate below would refuse it — correctly. Resolve the
        ONE result matching this set + product line and land on its
        validated product page instead. A refusal here is a deliberate skip,
        never a silent retry — and a search page must never become
        checkout-ready by relabelling.

        Returns None to continue the run (task["url"] updated in place to
        the final product page), or the terminal result dict when the run
        must end here.
        """
        from raindance.core import resolve as _resolve
        try:
            landed = page.url or ""
        except Exception:
            landed = ""
        if not _resolve.is_search_url(landed):
            return None
        try:
            from raindance.core.catalog import CatalogStore as _CS
            _cat = _CS(self.settings)
            _set_name = ((_cat.get_set(task.get("set_id") or "")
                          or {}).get("name") or "")
            _line_name = ((_cat.get_line(task.get("line_id") or "")
                           or {}).get("name") or "")
        except Exception:
            _set_name, _line_name = "", ""
        _want = f"{_set_name} {_line_name}".strip()
        self.bus.log(
            f"[{tid}] landed on search results — resolving the product page "
            f"for '{_want}'", "info")
        _res = _resolve.resolve_search_to_pdp(
            page, landed, site=(site or "generic"),
            set_name=_set_name, line_name=_line_name)
        if _res["ok"]:
            task["url"] = _res["url"]
            self.bus.log(
                f"[{tid}] product page resolved → {_res['url']} "
                f"({_res.get('candidates', 0)} results seen)", "hit")
            return None
        err = f"aborted before the product page — {_res['reason']}"
        shot = self._deny(
            tid, page, err,
            {"reason": _res["reason"], "status": "unverified"},
            forensics=self._forensics_bundle(
                tid=tid, task=task, page=page, site=site, attempt=attempt,
                kind="gate_denied", classification="unverified", detector="",
                action="aborted_before_product_page",
                outcome="denied:unverified", ev_snapshot=ev_snapshot,
            ),
        )
        return {"ok": False, "order_id": "", "error": err, "task_id": tid,
                "aborted_gate": True, "denied": True, "screenshot": shot}

    def _notify_denied(self, task, err: str) -> None:
        if not self.hub:
            return
        from raindance.core.notifications import NotificationEvent
        self.hub.notify(NotificationEvent(
            kind="error",
            title="\U0001F6AB Task denied",
            message=f"{task.get('name') or task.get('id')}: {err}",
            url=task.get("url"),
        ))

    def _fail(self, tid: str, page, err: str, *, forensics: dict | None = None) -> None:
        self._set(tid, M.STATUS_FAILED, last_error=err, message=err, denied=False)
        shot = ""
        try:
            shot = self._screenshot(page, tid, "fail")
        except Exception:
            pass
        if forensics:
            try:
                from raindance.core import forensics as _F
                _F.log_failure(settings=self.settings, screenshot=shot,
                               **forensics)
            except Exception:
                pass  # logging must never mask or break the failure
        task = self.task_store.get(tid) or {"id": tid}
        self._notify_fail(task, err)

    def _screenshot(self, page, tid: str, tag: str) -> str:
        shots = Path(self.settings.data.get("screenshots_dir") or "data/screenshots")
        shots.mkdir(parents=True, exist_ok=True)
        path = shots / f"{datetime.now():%Y%m%d-%H%M%S}-{tid}-{tag}.png"
        try:
            page.screenshot(path=str(path), full_page=True)
            self.bus.log(f"[{tid}] screenshot → {path}", "info")
            return str(path)
        except Exception as e:
            self.bus.log(f"[{tid}] screenshot failed: {e}", "warn")
            return ""

    def _notify_success(self, task, result) -> None:
        if not self.hub:
            return
        from raindance.core.notifications import NotificationEvent
        title = "✅ Checkout success" if not result.get("dry_run") else "🧪 Dry-run OK"
        msg = f"{task.get('name')} — order {result.get('order_id') or 'n/a'}"
        self.hub.notify(NotificationEvent(
            kind="checkout_success", title=title, message=msg, url=task.get("url"),
        ))

    def _notify_fail(self, task, err: str) -> None:
        if not self.hub:
            return
        from raindance.core.notifications import NotificationEvent
        self.hub.notify(NotificationEvent(
            kind="error",
            title="⚠️ Task failed",
            message=f"{task.get('name') or task.get('id')}: {err}",
            url=task.get("url"),
        ))
