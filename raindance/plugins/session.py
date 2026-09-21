"""Login Session tool — persistent profile management.

Opens a real browser on a PROFILE's browser folder so you sign into YOUR OWN
account once; the session is reused on every run of that profile. The app never
sees or stores your password — that's the whole point of using a browser profile.

The window is launched through the very same ``BrowserFactory.create_context``
call, on the very same ``user_data_dir``, that ``tasks/runner.py`` uses when the
wizard's EXECUTE starts a task for that profile. That is what makes the login
carry into a run: a session saved in one Chrome profile folder is invisible to a
task launched in another. (The previous version opened the legacy engine's
``browser.user_data_dir`` — ``profile/`` — while tasks run in the profile's
``data/browser_profiles/<id>``; a login there never reached a run.)
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from nicegui import ui

from raindance.core.registry import ToolPlugin, register_tool

# One login window per process, kept at module level so leaving and re-opening
# the page can still close it. Only the worker thread touches the browser.
_LOGIN: dict = {"thread": None, "stop": threading.Event(), "dir": "", "state": "idle"}


def profile_launch_dir(profile: dict | None) -> str | None:
    """The directory a TASK launches in for this profile — same rule as
    tasks/runner.py: ``profile.get("user_data_dir") or None`` (None = ephemeral)."""
    d = (profile or {}).get("user_data_dir") or None
    return d or None


def resolve_launch(ctx, profile_id: str, site: str) -> dict:
    """Everything the login window shares with a task launch, resolved the way
    the runner resolves it: profile dir, evasion on/off, strict flag, proxy pool."""
    from raindance.core import retailer_config as RC

    profile = ctx.profile_store.get(profile_id) if (ctx.profile_store and profile_id) else None
    if not profile and ctx.profile_store is not None:
        profile = ctx.profile_store.ensure_default()
    profile = profile or {}
    settings = ctx.settings
    ev = settings.data.get("evasion") or {}
    master = bool(ev.get("enabled"))
    site = site or "generic"
    group = RC.proxy_group_for(settings, site)
    pg = getattr(ctx, "proxy_groups", None) or getattr(getattr(ctx, "orchestrator", None), "proxy_groups", None)
    proxy_mgr = pg.get_manager(group) if pg is not None else getattr(ctx, "proxy_manager", None)
    browser_cfg = settings.data.get("browser") or {}
    return {
        "profile": profile,
        "profile_id": profile.get("id") or profile_id,
        "user_data_dir": profile_launch_dir(profile),
        "evasion_on": master and RC.evasion_enabled_for(settings, site),
        "strict": bool(ev.get("strict", False)),
        "proxy_group": group,
        "proxy_manager": proxy_mgr,
        "slow_mo": int(browser_cfg.get("slow_mo_ms") or 0),
        "viewport": browser_cfg.get("viewport"),
        # Same key shape the runner uses ("<profile>:<task>"), so a sticky proxy
        # pinned here is keyed per profile like a task's.
        "account_id": f"{profile.get('id') or 'default'}:login",
    }


def open_login_window(ctx, launch: dict, url: str) -> bool:
    """Launch the headed browser on a worker thread (sync Playwright cannot run
    on NiceGUI's event loop — same pattern as tasks/runner.py) and keep it open
    until close_login_window(). Returns False if one is already open."""
    t = _LOGIN.get("thread")
    if t is not None and t.is_alive():
        return False
    factory = ctx.browser_factory
    bus = ctx.bus
    stop = threading.Event()
    _LOGIN.update(stop=stop, dir=launch.get("user_data_dir") or "", state="opening", error="")

    # Bring the shared factory in line with what the runner would build for
    # this profile + store (the Evasion page does the same on save).
    if factory is not None and hasattr(factory, "configure"):
        try:
            factory.configure(
                evasion_enabled=bool(launch["evasion_on"]),
                strict_evasion=bool(launch["strict"]),
                proxy_manager=launch.get("proxy_manager"),
            )
        except Exception as exc:  # noqa: BLE001 - a stub factory may lack flags
            if bus:
                bus.log(f"[login] factory.configure skipped: {exc}", "warn")

    def worker():
        browser = context = None
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            _LOGIN["state"] = "error"; _LOGIN["error"] = "Playwright not installed"
            if bus:
                bus.log("[login] Playwright not installed — pip install -r requirements.txt", "err")
            return
        try:
            with sync_playwright() as p:
                if bus:
                    bus.log(
                        f"[login] opening profile={launch['profile_id']} "
                        f"dir={launch['user_data_dir'] or '(ephemeral!)'} "
                        f"(evasion={'ON' if launch['evasion_on'] else 'off'}, "
                        f"proxy_group={launch['proxy_group']})", "info")
                browser, context, page = factory.create_context(
                    p,
                    account_id=launch["account_id"],
                    headless=False,
                    user_data_dir=launch["user_data_dir"],
                    slow_mo=launch["slow_mo"],
                    viewport=launch["viewport"],
                )
                sess = factory.session_info(context) if hasattr(factory, "session_info") else None
                applied = (sess.stealth if sess is not None else getattr(factory, "last_stealth", "")) or ""
                if launch["evasion_on"] and "NOT APPLIED" in applied and bus:
                    bus.log("[login] this window is NOT actually patched — see the Evasion page", "warn")
                if url:
                    page.goto(url, wait_until="domcontentloaded")
                _LOGIN["state"] = "open"
                if bus:
                    bus.log("[login] window open — sign in, then click Done / Close", "info")
                while not stop.is_set():
                    time.sleep(0.4)
        except Exception as exc:  # noqa: BLE001
            _LOGIN["state"] = "error"; _LOGIN["error"] = str(exc)
            if bus:
                bus.log(f"[login] window error: {exc}", "err")
        finally:
            for obj in (context, browser):
                try:
                    if obj is not None:
                        obj.close()
                except Exception:  # noqa: BLE001
                    pass
            if _LOGIN.get("state") != "error":
                _LOGIN["state"] = "closed"
            if bus:
                bus.log(f"[login] window closed — session saved to {launch['user_data_dir']}", "info")

    th = threading.Thread(target=worker, daemon=True, name="raindance-login")
    _LOGIN["thread"] = th
    th.start()
    return True


def close_login_window() -> None:
    _LOGIN["stop"].set()


@register_tool
class SessionTool(ToolPlugin):
    id = "session"
    name = "Login Session"
    icon = "login"
    order = 30
    description = "Stay signed into your own account via a persistent profile."

    def render(self, ctx) -> None:
        d = ctx.settings.data
        sess_cfg = d.setdefault("session", {})

        ui.label("Login Session").classes("text-2xl font-bold")
        ui.markdown("Sign into **your own** account once and stay logged in. Your "
                    "password is **never stored or seen by this app** — the browser "
                    "keeps the session in the profile's own folder, the same folder "
                    "a task launches in. If **Evasion** is enabled, this login window "
                    "uses the same stealth + proxy settings as checkout.").classes("opacity-80")

        profiles = []
        try:
            profiles = ctx.profile_store.list() if ctx.profile_store is not None else []
        except Exception:  # noqa: BLE001
            profiles = []
        popts = {p["id"]: f"{p['name']} ({p['id']})" for p in profiles}
        stores = []
        try:
            stores = list(ctx.catalog.stores()) if getattr(ctx, "catalog", None) else []
        except Exception:  # noqa: BLE001
            stores = []
        sopts = {s["id"]: s.get("name") or s["id"] for s in stores}

        with ui.card().classes("w-full"):
            if not popts:
                ui.label("No profiles yet — create one on the Profiles page first.") \
                    .classes("text-sm text-amber-400")
            default_pid = sess_cfg.get("profile_id") if sess_cfg.get("profile_id") in popts else \
                (next(iter(popts)) if popts else None)
            prof_sel = ui.select(popts, value=default_pid, label="Profile (the identity a task runs as)") \
                .classes("w-full")
            dir_lbl = ui.label("").classes("text-xs opacity-60")
            default_sid = sess_cfg.get("store_id") if sess_cfg.get("store_id") in sopts else \
                (next(iter(sopts)) if sopts else None)
            store_sel = ui.select(sopts, value=default_sid, label="Store (opens its base URL)") \
                .classes("w-full")
            login_url = ui.input(
                "Login URL (optional — overrides the store URL)",
                value=d.get("site", {}).get("login_url", ""),
            ).classes("w-full")
            status = ui.label("").classes("text-sm opacity-70")
            ev = d.get("evasion") or {}
            ui.label(
                f"Evasion: {'ON' if ev.get('enabled') else 'off'} · "
                f"account: {ev.get('account_id') or 'default'}"
            ).classes("text-xs opacity-60")

            def _launch() -> dict:
                return resolve_launch(ctx, prof_sel.value or "", store_sel.value or "generic")

            def _paint_dir():
                try:
                    L = _launch()
                except Exception as exc:  # noqa: BLE001
                    dir_lbl.set_text(f"profile dir: ? ({exc})")
                    return
                dir_lbl.set_text(
                    "profile dir: " + (L["user_data_dir"] or "(none — ephemeral; set one on the Profiles page)")
                    + " — tasks for this profile launch here too")
            prof_sel.on_value_change(lambda e: _paint_dir())
            _paint_dir()

            def _target_url() -> str:
                if (login_url.value or "").strip():
                    return login_url.value.strip()
                st = next((s for s in stores if s["id"] == store_sel.value), None)
                return (st or {}).get("base_url") or ""

            def open_login():
                if not prof_sel.value:
                    ui.notify("Pick a profile first (create one on the Profiles page).", type="warning")
                    return
                L = _launch()
                if not L["user_data_dir"]:
                    ui.notify("This profile has no browser folder — set one on the Profiles page.",
                              type="warning")
                    return
                if ctx.browser_factory is None:
                    ui.notify("Browser factory unavailable.", type="negative")
                    return
                sess_cfg["profile_id"] = L["profile_id"]
                sess_cfg["store_id"] = store_sel.value or ""
                d.setdefault("site", {})["login_url"] = (login_url.value or "").strip()
                # Keep the legacy single-run engine pointed at the same folder so
                # every path in the app agrees on where the session lives.
                d.setdefault("browser", {})["user_data_dir"] = L["user_data_dir"]
                ctx.settings.save()
                if not open_login_window(ctx, L, _target_url()):
                    ui.notify("A login window is already open — click Done / Close first.", type="warning")
                    return
                ui.notify("Opening browser — sign in, then click Done / Close", type="info")

            def close_login():
                close_login_window()
                ui.notify("Login window closing — session saved to the profile folder", type="positive")

            with ui.row().classes("items-center gap-2"):
                ui.button("Open browser to log in", icon="login",
                          color="primary", on_click=open_login)
                ui.button("Done / Close", icon="check", on_click=close_login).props("outline")

            def check():
                try:
                    L = _launch()
                    p = Path(L["user_data_dir"]) if L["user_data_dir"] else None
                except Exception:  # noqa: BLE001
                    p = None
                ok = bool(p and p.exists() and p.is_dir() and any(p.iterdir()))
                st = _LOGIN.get("state")
                thread = _LOGIN.get("thread")
                live = thread is not None and thread.is_alive()
                if live:
                    txt = "login window open — sign in, then click Done / Close" if st == "open" else "opening browser…"
                elif st == "error":
                    txt = f"login window failed: {_LOGIN.get('error') or 'see log'}"
                else:
                    txt = "profile saved ✓" if ok else "no saved session yet — log in above"
                status.set_text(txt)

            check()
            ui.timer(2.0, check)

        ui.markdown("Pick the **Profile** the wizard will run as; the window opens on "
                    "that profile's browser folder, so the login is there when EXECUTE "
                    "launches it.").classes("opacity-70 text-sm")
