"""RainDance — desktop app entry point.

    python app.py
    python main.py          # web mode on :8213

Boots shared services, the multi-task orchestrator (Phases 0–8), evasion layer,
auto-discovers plugins/providers, and renders the sidebar + live log UI.
"""
from __future__ import annotations

import inspect
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nicegui import ui

from raindance.core.context import AppContext
from raindance.core.engine import CheckoutEngine
from raindance.core.events import EventBus
from raindance.core.notifications import NotificationHub
from raindance.core.registry import TOOL_REGISTRY, discover, tools_sorted
from raindance.core.scanner import Scanner
from raindance.core.search import SearchService
from raindance.core.settings import Settings
from raindance.core.store import ProductStore
from raindance.core.catalog import CatalogStore
from raindance.core.execute_queue import ExecuteQueue
from raindance.core.link_index import LinkIndex
from raindance.sites.user_store import register_user_stores
from raindance.ui import theme
from raindance.evasion.browser_factory import BrowserFactory
from raindance.evasion.fingerprint_manager import FingerprintManager
from raindance.evasion.proxy_manager import ProxyManager
from raindance.tasks.orchestrator import Orchestrator
import raindance.plugins as plugins_pkg
import raindance.providers as providers_pkg

# --- bootstrap ------------------------------------------------------------ #
bus = EventBus()
settings = Settings()
hub = NotificationHub(bus=bus)
store = ProductStore(settings)
catalog = CatalogStore(settings)
queue = ExecuteQueue(settings)
links = LinkIndex(settings)
# Give every user-owned store a handler BEFORE the task store is read, so
# normalize_task stops rewriting their ids to "generic".
register_user_stores(settings, bus=bus)
search = SearchService(settings=settings, bus=bus)

_prov_errors = discover(providers_pkg)
hub.load()
hub.configure(settings.data.get("notifications", {}))
_tool_errors = discover(plugins_pkg)

_evasion = settings.data.get("evasion") or {}
proxy_manager = ProxyManager.from_settings(_evasion)

# The BrowserFactory constructor GROWS flags over time, and this app has to boot
# against whichever copy of the evasion layer is installed. So the tuning flags
# are filtered through the installed __init__'s real signature rather than being
# passed blind: on a factory that predates one, the keyword is simply dropped and
# construction is byte-for-byte what it was before. Everything the older factory
# already accepted (bus=, strict_evasion=) is passed unconditionally, as always.
_bf_kwargs = {
    "proxy_manager": proxy_manager,
    "fingerprint_manager": FingerprintManager(),
    "evasion_enabled": bool(_evasion.get("enabled")),
    "strict_evasion": bool(_evasion.get("strict", False)),
    "bus": bus,
}
# Defaults chosen to match what each flag's own default is on the factory that
# introduced it, so an absent settings key changes nothing.
_bf_optional = {
    "chrome_channel": bool(_evasion.get("chrome_channel", True)),
    "verify_proxy_http": bool(_evasion.get("verify_proxy_http", True)),
    "detect_engine_version": bool(_evasion.get("detect_engine_version", True)),
    "use_stealth_lib": bool(_evasion.get("use_stealth_lib", True)),
}
try:
    _bf_accepts = set(inspect.signature(BrowserFactory.__init__).parameters)
except (TypeError, ValueError):  # a C-implemented or otherwise opaque __init__
    _bf_accepts = set()
_bf_dropped = sorted(k for k in _bf_optional if k not in _bf_accepts)
_bf_kwargs.update({k: v for k, v in _bf_optional.items() if k in _bf_accepts})
browser_factory = BrowserFactory(**_bf_kwargs)
account_id = _evasion.get("account_id") or "default"

engine = CheckoutEngine(
    bus=bus,
    hub=hub,
    browser_factory=browser_factory,
    account_id=account_id,
)
scanner = Scanner(store=store, bus=bus, hub=hub)

orchestrator = Orchestrator(settings=settings, bus=bus, hub=hub)

ctx = AppContext(
    settings=settings,
    engine=engine,
    hub=hub,
    bus=bus,
    store=store,
    search=search,
    scanner=scanner,
    browser_factory=browser_factory,
    proxy_manager=proxy_manager,
    proxy_groups=orchestrator.proxy_groups,
    catalog=catalog,
    queue=queue,
    links=links,
    orchestrator=orchestrator,
    profile_store=orchestrator.profile_store,
    task_store=orchestrator.task_store,
)

if _bf_dropped:
    bus.log(
        "evasion: installed BrowserFactory does not accept "
        + ", ".join(_bf_dropped)
        + " — those settings are inactive until it is updated",
        "warn",
    )
for err in _prov_errors:
    bus.log(f"provider load error: {err}", "warn")
for err in _tool_errors:
    bus.log(f"tool load error: {err}", "warn")

_ev_label = "ON" if browser_factory.evasion_enabled else "off"
if browser_factory.evasion_enabled and browser_factory.strict_evasion:
    _ev_label += " (strict)"
bus.log(
    f"RainDance ready — {len(TOOL_REGISTRY)} tools, {len(hub.providers())} providers, "
    f"evasion {_ev_label}, proxies={orchestrator.proxy_groups.total()}, "
    f"profiles={len(orchestrator.profile_store.list())}, "
    f"tasks={len(orchestrator.task_store.list())}",
    "info",
)

ADD_TOOL_HELP = """**Add a tool** — drop a `.py` file in `raindance/plugins/`
subclassing `ToolPlugin` with `@register_tool`."""

# The drawer is a Settings panel and nothing else. This is an explicit
# allowlist — (registry id, label override) — so a new plugin never appears in
# the nav by accident; the wizard is the app and every other page is legacy.
SETTINGS_NAV: tuple[tuple[str, str | None], ...] = (
    ("monitors_tab", "Catalog"),
    ("profiles", None),
    ("evasion", None),
    ("captcha", "CAPTCHA"),
    ("notifications", None),
    ("session", "Login Session"),
)
_NAV_LABEL = {tid: lab for tid, lab in SETTINGS_NAV if lab}
HOME_ID = "wizard"


def settings_nav() -> list[tuple[str, str]]:
    """(id, label) for every allowlisted settings page that is registered."""
    out = []
    for tid, label in SETTINGS_NAV:
        tool = TOOL_REGISTRY.get(tid)
        if tool is not None:
            out.append((tid, label or tool.name))
    return out


def _mode(dry: bool, running: bool) -> tuple[str, str]:
    """(css-modifier, label) for the persistent dry-run / live badge.

    Reads the same gate the wizard flips (`checkout.force_dry_run`) plus the
    orchestrator's running flag — not run_plan, which the wizard never writes.
    """
    if dry:
        return "dry", "Dry run · running" if running else "Dry run"
    if running:
        return "live", "Live · running"
    return "live", "Live · armed"


def _gate_dry() -> bool:
    """True while `checkout.force_dry_run` holds every task pre-payment."""
    try:
        return bool((settings.data.get("checkout") or {}).get("force_dry_run", True))
    except Exception:  # noqa: BLE001 - a malformed settings blob is not fatal
        return True


@ui.page("/")
def main():
    ui.dark_mode(True)
    ui.page_title("RainDance")
    theme.inject()

    # The wizard IS the app: products -> retailer -> execute. The wrench opens a
    # Settings-only drawer (SETTINGS_NAV); legacy pages stay registered, unlisted.
    selected = {"id": HOME_ID if HOME_ID in TOOL_REGISTRY
                else (tools_sorted()[0].id if TOOL_REGISTRY else None)}

    def _home() -> None:
        """Back to the wizard — the one selection call every return path uses."""
        selected.update(id=HOME_ID)
        render_selected()

    def _stop() -> None:
        orchestrator.stop()
        bus.log("[ui] stop requested from the header", "warn")
        ui.notify("Stop requested", type="info")

    with ui.header().classes("items-center").style(
            "background:var(--rd-surface); border-bottom:1px solid var(--rd-line)"):
        # The mark + wordmark are one click target back to Start.
        with ui.element("div").classes("rd-brand").on("click", _home):
            ui.html(
                '<span style="display:inline-block;width:22px;height:22px;border-radius:50%;'
                'border:1.5px solid var(--rd-accent);position:relative">'
                '<span style="position:absolute;inset:5px;border-radius:50%;'
                'background:var(--rd-accent)"></span></span>',
                sanitize=False,
            )
            ui.label("RainDance").classes("rd-mono").style(
                "font-weight:700;letter-spacing:.22em;text-transform:uppercase;"
                "font-size:13px;color:var(--rd-ink)")
        ui.space()

        # No tab row — the wizard's own stepper is the navigation.
        mode_badge = ui.label("Dry run").classes("rd-badge rd-badge-dry")
        stop_btn = ui.button("STOP", on_click=_stop, color=None) \
            .props("outline dense no-caps").classes("rd-stop")
        stop_btn.set_visibility(False)
        ui.button(icon="build", on_click=lambda: drawer.toggle()).props("flat dense") \
            .tooltip("Settings")

    # The drawer is Settings only. Built from SETTINGS_NAV, never from the
    # registry at large, so legacy pages (execute, arm, control, scheduler, …)
    # stay registered but out of the nav.
    nav_buttons: dict[str, "ui.button"] = {}

    def _nav_button(tid: str, label: str) -> None:
        tool = TOOL_REGISTRY[tid]
        btn = ui.button(
            label, icon=tool.icon,
            on_click=lambda t=tid: (selected.update(id=t), render_selected(),
                                    drawer.toggle()),
        ).props("flat no-caps align=left").classes("rd-nav w-full justify-start")
        nav_buttons[tid] = btn

    with ui.left_drawer(value=False).style(
            "background:var(--rd-surface); border-right:1px solid var(--rd-line)") as drawer:
        ui.label("Settings").classes("rd-lab").style("padding:2px 8px 6px")
        for tid, label in settings_nav():
            _nav_button(tid, label)

        ui.separator().style("background:var(--rd-line); margin:12px 0 8px")
        with ui.expansion("Add a tool", icon="add").classes("w-full rd-mono"):
            ui.markdown(ADD_TOOL_HELP).classes("text-xs")

    def render_selected():
        for tid, btn in nav_buttons.items():
            btn.classes(replace="rd-nav w-full justify-start"
                        + (" rd-nav-on" if tid == selected["id"] else ""))
        content.clear()
        with content:
            # Looked up straight from the registry: the wizard is reachable here
            # even though it is deliberately absent from the drawer.
            tool = TOOL_REGISTRY.get(selected["id"])
            if tool is None:
                ui.label("No stages registered.").classes("opacity-60")
                return
            if tool.id != HOME_ID:
                # The one way back for every settings page — no per-plugin buttons.
                with ui.element("div").classes("rd-backbar"):
                    ui.button("← Back to Start", on_click=_home, color=None) \
                        .props("flat dense no-caps").classes("rd-back")
                    ui.label(_NAV_LABEL.get(tool.id, tool.name)).classes("rd-backbar-name")
            tool.render(ctx)

    # ---- opening splash -------------------------------------------------- #
    # Covers everything on load: rain rings expand, the mark condenses out of
    # them, the wordmark spaces in, then START hands over to the wizard.
    with ui.element("div").classes("rd-splash") as splash:
        with ui.element("div").classes("rd-splash-inner"):
            ui.html(
                '<div class="rd-splash-stage">'
                '<i></i><i></i><i></i><i></i>'
                '<div class="rd-splash-mark"></div>'
                '</div>'
                '<div class="rd-splash-word">RainDance</div>'
                '<div class="rd-splash-sub">drop automation</div>',
                sanitize=False,
            )

            def _enter() -> None:
                splash.classes(add="rd-gone")
                if HOME_ID in TOOL_REGISTRY:
                    _home()
                else:
                    render_selected()

            with ui.element("div").classes("rd-splash-go"):
                ui.button("Start", on_click=_enter, color=None) \
                    .props("unelevated no-caps") \
                    .classes("rd-splash-btn")

    content = ui.column().classes("w-full min-w-0 gap-4 p-4")
    ctx.refresh = render_selected
    render_selected()

    def pump():
        running = bool(orchestrator.is_running)
        mod, text = _mode(_gate_dry(), running)
        mode_badge.set_text(text)
        mode_badge.classes(replace=f"rd-badge rd-badge-{mod}")
        stop_btn.set_visibility(running)

    pump()

    ui.timer(1.0, pump)


def run():
    web = ("--web" in sys.argv) or (os.environ.get("RAINDANCE_WEB") == "1")
    # Honor an externally-assigned port (e.g. the preview harness sets PORT);
    # fall back to the default 8213 when unset.
    port = int(os.environ.get("PORT") or 8213)
    if web:
        # Bind to loopback. NiceGUI resolves host=None to 0.0.0.0 when
        # native=False, which put an unauthenticated control plane — arm tasks,
        # clear force_dry_run, start a live checkout — on every interface. Set
        # RAINDANCE_HOST to opt into LAN access deliberately.
        host = os.environ.get("RAINDANCE_HOST") or "127.0.0.1"
        ui.run(title="RainDance", native=False, show=False, reload=False,
               port=port, host=host)
    else:
        ui.run(
            title="RainDance",
            native=True,
            show=False,
            reload=False,
            port=port,
            window_size=(1280, 860),
        )


if __name__ in {"__main__", "__mp_main__"}:
    run()
