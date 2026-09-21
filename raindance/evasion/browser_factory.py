"""Create Playwright browser contexts with optional evasion (stealth + proxy).

When ``evasion_enabled`` is on, a launch gets: a sticky proxy (from the
ProxyManager, per account), a coherent fingerprint (from the FingerprintManager,
applied as both context options and a pre-navigation init script), Chromium
launch flags that strip the obvious automation tells, and — if installed —
playwright-stealth on top.

When it's off, the factory launches a plain browser, honoring only explicit
``viewport`` / ``user_agent`` overrides. That keeps non-evasion runs behaving
exactly like the original app.

The factory never owns the Playwright lifetime — callers pass an already-started
``playwright`` (typically from ``with sync_playwright() as p``). For persistent
profiles ``browser`` is ``None`` (the context owns the process), matching the
CheckoutBot convention.

Every launch also builds a :class:`SessionInfo` describing THAT launch and
attaches it to the context (``factory.session_info(context)``). The legacy
``last_*`` attributes still exist and still say the same things, but they belong
to the factory rather than to a launch, so on a shared factory with two launches
in flight they are inherently racy — see the note where they are assigned.
"""
from __future__ import annotations

import inspect
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from raindance.evasion.fingerprint_manager import FingerprintManager
from raindance.evasion.proxy_manager import ProxyManager, mask as _proxy_mask

# Conservative flags: strip the automation banner/flags without disabling
# security features that could break real store pages.
_STEALTH_ARGS: List[str] = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-infobars",
    "--disable-dev-shm-usage",
    "--disable-popup-blocking",
    "--password-store=basic",  # avoid a keyring prompt on headless Linux
    "--use-mock-keychain",
]

_BLANK = "about:blank"

# The verification verdict when nothing tripped. Named, because create_context
# compares against it and the exact words matter: this says the browser matches
# what the factory configured, NOT that a site's bot detection is fooled.
_VERIFY_OK = "configured browser checks passed"

# Read the automation tells back off a live page. This block REPORTS only:
# nothing here patches anything. Most fields map to a check in _verify; the rest
# (screen) are diagnostic, carried in the snapshot for a caller to look at.
_PROBE_JS = """() => {
  let webgl = "";
  try {
    const gl = document.createElement("canvas").getContext("webgl");
    const dbg = gl && gl.getExtension("WEBGL_debug_renderer_info");
    if (dbg) { webgl = String(gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) || ""); }
  } catch (e) { webgl = ""; }
  let tz = "";
  try { tz = Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch (e) { tz = ""; }
  let uaMajor = null;
  try {
    const m = /Chrome\\/(\\d+)/.exec(navigator.userAgent);
    uaMajor = m ? Number(m[1]) : null;
  } catch (e) { uaMajor = null; }
  let scr = null;
  try { scr = { width: screen.width, height: screen.height }; } catch (e) { scr = null; }
  return {
    webdriver: navigator.webdriver,
    ua_headless: /HeadlessChrome/.test(navigator.userAgent),
    plugins: (navigator.plugins && navigator.plugins.length) || 0,
    notification: (window.Notification && Notification.permission) || "",
    webgl: webgl,
    ua_chrome_major: uaMajor,
    hardware_concurrency: navigator.hardwareConcurrency,
    device_memory: navigator.deviceMemory,
    timezone: tz,
    screen: scr,
  };
}"""


log = logging.getLogger(__name__)


class EvasionError(RuntimeError):
    """Evasion was switched on but could not actually be applied."""


# --------------------------------------------------------------------------- #
# Engine-version probe cache
#
# tasks/runner.py builds a NEW BrowserFactory for every task, so anything cached
# on the instance is re-derived per task. Probing the engine version means
# launching a throwaway browser, and per task that is a real cost on the
# checkout hot path. The cache is therefore module-level (once per PROCESS),
# keyed by the channel the probe used — "chrome" and bundled chromium are
# different binaries with different versions, so they cannot share an entry.
# A failed probe caches None: the failure is the same every time (no Chrome
# installed, no browser deps), and re-paying a failed launch per task is exactly
# what this cache exists to prevent.
# --------------------------------------------------------------------------- #
_ENGINE_VERSION_CACHE: Dict[Tuple[Optional[str]], Optional[str]] = {}
_ENGINE_VERSION_LOCK = threading.Lock()


@dataclass
class SessionInfo:
    """Everything one launch decided, attached to the context it describes.

    The factory's ``last_*`` attributes answer "what did the last launch do?",
    which is the wrong question the moment two launches overlap. This answers
    "what did THIS context's launch do?" and is fetched with
    ``factory.session_info(context)``.
    """

    account_id: str
    proxy: Optional[Dict]
    profile: Optional[Dict]
    fingerprint: str
    stealth: str
    engine: str
    verify_summary: str
    verify: Dict = field(default_factory=dict)
    natural_snapshot: Dict = field(default_factory=dict)
    proxy_health: Optional[Dict] = None
    warnings: List[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Evasion extension surface
#
# A launch fires a fixed set of NAMED lifecycle hooks, in order. Every hook is
# OPTIONAL: if nothing implements it, the launch behaves exactly as it would
# with no hooks at all. You attach code three ways, all discovered automatically
# and all additive (many handlers per hook, run in registration order, custom
# module last):
#
#   1. raindance/evasion/custom.py — define any subset of the hook functions
#      below at module level. (gitignored; copy custom.py.example to start.)
#   2. register(plugin) — pass any object exposing hook methods; applies to
#      every factory built afterwards. Good for a class that carries state.
#   3. factory.register_plugin(plugin) — same, but scoped to one factory.
#
# The hook catalogue (see INTERFACE.md for the full spec). TRANSFORM hooks get a
# dict and may mutate it in place OR return a replacement; SIDE hooks get live
# Playwright objects and return nothing:
#
#   configure_launch(launch_kwargs, info)   TRANSFORM  before chromium.launch*
#   configure_context(context_opts, info)   TRANSFORM  before the context is made
#   on_context_created(context, info)       SIDE       context exists
#   on_page_created(page, info)             SIDE       first page exists
#   apply(context, page, info)              SIDE       back-compat catch-all, last
#
# `info` is a live dict carried across the whole launch:
#   {account_id, headless, user_data_dir, for_harvester, proxy, fingerprint,
#    stealth}. Later hooks see fields earlier stages filled in; you may add your
#    own keys to pass state down the chain.
#
# The surface is meant to GROW. Adding a new named hook is: pick a name, call it
# from create_context via _dispatch_*, document it in INTERFACE.md. Nothing that
# does not implement the new name is affected.
# --------------------------------------------------------------------------- #

# Handlers registered process-wide. Instance-scoped handlers live on the factory.
_PLUGINS: List[Any] = []


def register(plugin: Any) -> Any:
    """Register an evasion plugin (any object exposing hook methods) globally.

    Returns the plugin, so it doubles as a class decorator. Idempotent.
    """
    if plugin not in _PLUGINS:
        _PLUGINS.append(plugin)
    return plugin


def unregister(plugin: Any) -> None:
    try:
        _PLUGINS.remove(plugin)
    except ValueError:
        pass


# Ordered list of the hooks create_context fires, so the surface is inspectable.
LIFECYCLE_HOOKS = (
    "configure_context",    # transform: context_opts   (fires first)
    "configure_launch",     # transform: launch_kwargs
    "on_context_created",   # side: (context, info)
    "on_page_created",      # side: (page, info)
    "apply",                # side: (context, page, info) - back-compat, last
)


class BrowserFactory:
    """Creates Playwright browser contexts with optional evasion features."""

    def __init__(
        self,
        proxy_manager: Optional[ProxyManager] = None,
        fingerprint_manager: Optional[FingerprintManager] = None,
        evasion_enabled: bool = False,
        strict_evasion: bool = False,
        bus=None,
        # Launch the real Google Chrome instead of Playwright's bundled
        # Chromium when it is installed (create_context falls back).
        chrome_channel: bool = True,
        # Ask the ProxyManager to prove the proxy actually carries HTTP before
        # the launch. COSTS a blocking network round-trip on the checkout hot
        # path, which is why it is a flag and not unconditional.
        verify_proxy_http: bool = True,
        # Bind fingerprints to the engine version that will really launch.
        # Costs one throwaway browser launch per PROCESS (see the cache above),
        # and only when the FingerprintManager can actually use the answer.
        detect_engine_version: bool = True,
        # Run the playwright-stealth library step in _apply_stealth. OFF still
        # injects the fingerprint init script - only the library is skipped -
        # and is a plain switch, never inferred: nothing in this file inspects
        # the driver or the environment to decide it for you.
        use_stealth_lib: bool = True,
    ):
        self.bus = bus
        self.proxy_manager = proxy_manager
        self.fingerprint_manager = fingerprint_manager or FingerprintManager()
        self.evasion_enabled = bool(evasion_enabled)
        # When True, a launch that cannot be patched raises instead of running
        # a session that only looks protected.
        self.strict_evasion = bool(strict_evasion)
        self.chrome_channel = bool(chrome_channel)
        self.verify_proxy_http = bool(verify_proxy_http)
        self.detect_engine_version = bool(detect_engine_version)
        self.use_stealth_lib = bool(use_stealth_lib)
        # LEGACY mirrors of the last launch. Kept because runner.py:129-131 and
        # the Evasion page read them; see the single assignment block at the end
        # of create_context for why they are racy and what to use instead.
        self.last_proxy: Optional[Dict] = None
        self.last_fingerprint: str = ""
        self.last_stealth: str = ""
        self.last_engine: str = ""
        # The SessionInfo of the most recent launch. Same raciness as last_*;
        # session_info(context) is the per-context accessor.
        self.last_session: Optional[SessionInfo] = None
        # Engine version this factory bound profiles to, "" when it never did.
        self._bound_engine_version: str = ""
        # Handlers scoped to this factory (globals in _PLUGINS also fire).
        self._plugins: List[Any] = []
        # import attempt for raindance/evasion/custom.py, cached after first try.
        self._custom_cached = False
        self._custom_module: Any = None

    def configure(
        self,
        *,
        evasion_enabled: Optional[bool] = None,
        proxy_manager: Optional[ProxyManager] = None,
        strict_evasion: Optional[bool] = None,
        chrome_channel: Optional[bool] = None,
        verify_proxy_http: Optional[bool] = None,
        detect_engine_version: Optional[bool] = None,
        use_stealth_lib: Optional[bool] = None,
    ) -> None:
        """Hot-update factory flags (e.g. after the Evasion UI toggle)."""
        if evasion_enabled is not None:
            self.evasion_enabled = bool(evasion_enabled)
        if proxy_manager is not None:
            self.proxy_manager = proxy_manager
        if strict_evasion is not None:
            self.strict_evasion = bool(strict_evasion)
        if chrome_channel is not None:
            self.chrome_channel = bool(chrome_channel)
        if verify_proxy_http is not None:
            self.verify_proxy_http = bool(verify_proxy_http)
        if detect_engine_version is not None:
            self.detect_engine_version = bool(detect_engine_version)
        if use_stealth_lib is not None:
            self.use_stealth_lib = bool(use_stealth_lib)

    def session_info(self, context) -> Optional[SessionInfo]:
        """The SessionInfo for THIS context, or None if it did not come from here.

        This is the correct accessor for "what did this session launch with?" —
        unlike the last_* attributes it cannot be overwritten by another launch.
        """
        return getattr(context, "_evasion_info", None)

    def _log_launch_verdict(
        self,
        session: SessionInfo,
        *,
        task_id: Optional[str] = None,
        site: Optional[str] = None,
        proxy_group: Optional[str] = None,
    ) -> None:
        """Append one JSONL verdict line for this launch. Best-effort: never
        raises, so a logging problem can never change launch behavior.

        The exit identity is masked with proxy_manager.mask - "direct" when no
        proxy was used, otherwise host:port with credentials hidden.
        """
        try:
            from raindance.evasion import launch_log  # lazy: stdlib-only
            proxy = session.proxy
            try:
                exit_label = "direct" if proxy is None else _proxy_mask(proxy)
            except Exception:  # noqa: BLE001 - a malformed proxy dict logs raw
                exit_label = "direct" if proxy is None else "proxy (unmaskable)"
            record = launch_log.build_record(
                task_id=task_id,
                site=site,
                evasion_enabled=self.evasion_enabled,
                stealth=session.stealth,
                verify_summary=session.verify_summary,
                engine=session.engine,
                fingerprint=session.fingerprint,
                exit_label=exit_label,
                proxy_group=proxy_group,
                warnings=session.warnings,
            )
            launch_log.append_launch(record)
        except Exception:  # noqa: BLE001 - see docstring
            pass

    def create_context(
        self,
        playwright,
        *,
        account_id: str = "default",
        headless: bool = False,
        user_data_dir: Optional[str] = None,
        slow_mo: int = 0,
        viewport: Optional[Dict] = None,
        user_agent: Optional[str] = None,
        for_harvester: bool = False,
        keep_open_hint: bool = False,  # reserved for callers
        # Labels for the per-launch JSONL verdict (raindance/evasion/launch_log).
        # Purely diagnostic: they never affect the launch itself.
        task_id: Optional[str] = None,
        site: Optional[str] = None,
        proxy_group: Optional[str] = None,
    ) -> Tuple[Any, Any, Any]:
        """Launch a browser/context/page.

        Returns (browser, context, page). For persistent profiles, browser is
        None (the context owns the process).

        Every launch also appends one verdict line to
        data/evasion_launches.jsonl (raindance.evasion.launch_log) - the only
        durable record of what the launch decided. That write is best-effort
        and can never change launch behavior.

        Everything this launch decides is accumulated in LOCALS and published to
        the factory's legacy last_* attributes exactly once, at the end. Writing
        them progressively (as this used to) meant a concurrent launch could
        overwrite last_stealth between this launch setting it and this launch's
        caller reading it, so a task could report another task's session.
        """
        # ---- launch-local state (never self.*) ---------------------------- #
        fingerprint: str = ""
        stealth: str = ""
        engine: str = ""
        verify: Dict[str, Any] = {}
        verify_summary: str = ""
        natural: Dict[str, Any] = {}
        proxy_health: Optional[Dict] = None
        warnings: List[str] = []

        proxy = self._pick_proxy(account_id, for_harvester)

        # Proxy health BEFORE the launch: a dead exit is worth knowing about
        # before a browser exists, and failing here costs no teardown. Optional
        # on the manager (hasattr) so an older ProxyManager is unaffected.
        proxy_health = self._check_proxy_http(proxy, warnings)

        # Carried across every hook in this launch; hooks may add their own keys.
        info: Dict[str, Any] = {
            "account_id": account_id, "headless": headless,
            "user_data_dir": user_data_dir, "for_harvester": for_harvester,
            "proxy": proxy, "fingerprint": "", "stealth": "",
        }

        # Context options: full fingerprint when evasion on; otherwise only the
        # explicit overrides the caller passed.
        profile = None
        if self.evasion_enabled:
            fm = self.fingerprint_manager
            # Bind the identity to the engine that will really launch, BEFORE a
            # profile is picked — a profile built against Chrome 140 and then
            # served by Chrome 131 is a self-inflicted tell. Optional on the
            # manager, and the probe only runs when the manager can use it.
            self._bind_engine_version(playwright)
            # A sticky identity for this account, then aligned to the exit we
            # just picked so the fingerprint and the proxy tell one story. Both
            # methods are optional on a swapped-in manager, so both are
            # hasattr-guarded (INTERFACE.md 2): a manager without them behaves
            # exactly as before.
            profile = fm.profile_for(account_id) if hasattr(fm, "profile_for") else None
            if profile is not None and hasattr(fm, "align_to_proxy"):
                fm.align_to_proxy(profile, proxy)
            # override is KEYWORD-only on the FingerprintManager, and carries
            # the profile so the options describe that identity, not a fresh one.
            context_opts = fm.context_options(
                override={"viewport": viewport, "user_agent": user_agent,
                          "_profile": profile}
            )
            # describe(account_id) on a manager that keeps per-account profiles;
            # older ones take no argument. Without this, last_fingerprint - which
            # runner.py logs for every task - always named the default account.
            try:
                fingerprint = fm.describe(account_id)
            except TypeError:
                fingerprint = fm.describe()
        else:
            context_opts = {}
            if viewport:
                context_opts["viewport"] = viewport
            if user_agent:
                context_opts["user_agent"] = user_agent
            fingerprint = ""
        info["fingerprint"] = fingerprint
        context_opts = self._dispatch_transform("configure_context", context_opts, info)

        launch_kwargs = self._launch_kwargs(
            headless=headless, slow_mo=slow_mo, proxy=proxy)
        launch_kwargs = self._dispatch_transform("configure_launch", launch_kwargs, info)

        browser, context, engine = self._launch(
            playwright, launch_kwargs, context_opts, user_data_dir)

        # Past this point a browser is RUNNING, so every exit has to close it. A
        # raise here - strict mode, a hook, stealth, verification - propagates
        # before the caller's `browser, context, page = create_context(...)`
        # binds anything, so runner.py's own finally: sees None/None and closes
        # nothing: the Chromium process is orphaned. _verify makes that routine
        # rather than rare (a VM reporting SwiftShader trips it every run).
        try:
            if context is None:
                # Non-persistent: the context is built HERE, outside the launch
                # fallback, so a bad context option cannot masquerade as a
                # missing Chrome channel - and is torn down by this handler.
                context = browser.new_context(**context_opts)

            if self.evasion_enabled:
                # Read the tells off the UNTOUCHED browser first: add_init_script
                # only affects pages opened after it, so this is the only moment
                # a natural reading exists. The page it opens is deliberately
                # left open - the stale-page sweep below closes it, which is also
                # what stops it leaking.
                natural = self._natural_snapshot(context)

                stealth = self._apply_stealth(context, profile)
                # "evasion=ON" only ever meant the toggle. Say what actually happened,
                # so a session that is not really patched cannot pass unnoticed.
                unpatched = "NOT APPLIED" in stealth
                self._emit(f"stealth: {stealth}",
                           "warn" if unpatched else "info")
            else:
                stealth = ""
            info["stealth"] = stealth

            # Context-scoped hooks (add_init_script, add_cookies, routing, ...).
            self._dispatch_side("on_context_created", context, info)

            # add_init_script only affects pages created AFTER it runs, so the
            # about:blank page a persistent context opens must not survive. The
            # replacement is opened FIRST: closing the old pages before creating
            # it would leave a persistent context momentarily at zero pages.
            stale = list(getattr(context, "pages", None) or [])
            page = context.new_page()
            for old in stale:
                try:
                    old.close()
                except Exception:  # noqa: BLE001 - a page we are discarding anyway
                    pass

            # Page-scoped hooks, then the back-compat apply() catch-all, run last.
            self._dispatch_side("on_page_created", page, info)
            self._dispatch_side("apply", context, page, info)

            if self.evasion_enabled:
                # Read the tells back off the page we are about to hand over,
                # once every patch - built-in and hook - has been applied.
                applied = stealth
                verify = self._verify(
                    page, profile=profile, natural=natural,
                    engine_version=self._running_version(browser, context))
                verify_summary = verify["summary"]
                warnings.extend(verify.get("warnings") or [])
                # Composed from the pre-verify value: _verify may itself have
                # rewritten last_stealth through _problem, and a NOT APPLIED
                # from _apply_stealth has to survive into the combined string.
                stealth = f"{applied} \u00b7 verify {verify_summary}"
                info["stealth"] = stealth
                self._emit(f"stealth verify: {verify_summary}",
                           "info" if verify_summary == _VERIFY_OK
                           else "warn")

            session = SessionInfo(
                account_id=account_id,
                proxy=proxy,
                profile=profile,
                fingerprint=fingerprint,
                stealth=stealth,
                engine=engine,
                verify_summary=verify_summary,
                verify=verify,
                natural_snapshot=natural,
                proxy_health=proxy_health,
                warnings=warnings,
            )
            try:
                context._evasion_info = session
            except Exception as exc:  # noqa: BLE001 - a slotted/proxied context
                self._emit(f"could not attach session info to the context: {exc}",
                           "warn")

            # ---- legacy mirrors, assigned exactly once, at the very end ---- #
            # These belong to the FACTORY, not to a launch: app.py builds one
            # factory for the whole process, so two overlapping launches share
            # them and a reader can be handed the other launch's values. They
            # are RACY UNDER THREADS BY CONSTRUCTION and cannot be made
            # otherwise. Writing them here, once, rather than progressively
            # through the launch at least means a reader sees one launch's
            # values and never a half-built mixture of two.
            #   Correct accessor for new code: factory.session_info(context).
            # A launch that aborts before this block therefore leaves the
            # PREVIOUS launch's values on show: the raised exception, not a
            # last_* read, is what tells a caller THIS launch failed.
            # (One mid-flight write survives on purpose: _problem() sets
            # last_stealth to its "NOT APPLIED - ..." string because under
            # strict_evasion it raises, and this block is then never reached —
            # it is the only record of why the launch aborted.)
            self.last_proxy = proxy
            self.last_fingerprint = fingerprint
            self.last_stealth = stealth
            self.last_engine = engine
            self.last_session = session

            # Durable record of this launch's verdict. Logging-only: it cannot
            # change the launch, and it never raises.
            self._log_launch_verdict(
                session,
                task_id=task_id,
                site=site,
                proxy_group=proxy_group,
            )

            return browser, context, page
        except BaseException:
            self._close_quietly(context, browser)
            raise

    # -- internals --------------------------------------------------------- #
    def register_plugin(self, plugin: Any) -> Any:
        """Register an evasion plugin on THIS factory only. Returns it."""
        if plugin not in self._plugins:
            self._plugins.append(plugin)
        return plugin

    def _get_custom_module(self):
        """Import raindance/evasion/custom.py once; cache the result (incl. absent).

        Absent module is the normal case and caches as None. A module that
        exists but fails to import is surfaced (and raised under strict), never
        silently swallowed.
        """
        if self._custom_cached:
            return self._custom_module
        self._custom_cached = True
        try:
            from raindance.evasion import custom  # type: ignore
            self._custom_module = custom
        except ImportError:
            self._custom_module = None
        except Exception as exc:  # noqa: BLE001 - a broken custom.py must be visible
            self._emit(f"custom evasion module failed to import: {exc}", "warn")
            self._custom_module = None
            if self.strict_evasion:
                raise
        return self._custom_module

    def _collect_handlers(self, hook: str) -> List[Any]:
        """Every callable registered for `hook`, in order: global plugins,
        this factory's plugins, then the custom module's module-level function."""
        handlers: List[Any] = []
        for source in (_PLUGINS, self._plugins):
            for plugin in source:
                fn = getattr(plugin, hook, None)
                if callable(fn):
                    handlers.append(fn)
        mod = self._get_custom_module()
        if mod is not None:
            fn = getattr(mod, hook, None)
            if callable(fn):
                handlers.append(fn)
        return handlers

    def _hook_error(self, hook: str, exc: BaseException) -> None:
        self._emit(f"evasion hook {hook}() raised {type(exc).__name__}: {exc}", "warn")
        if self.strict_evasion:
            raise exc

    def _dispatch_side(self, hook: str, *args) -> None:
        """Fire a side-effect hook (context/page work). Errors are logged, and
        raised only under strict_evasion. A no-op when nothing implements it."""
        for fn in self._collect_handlers(hook):
            try:
                fn(*args)
                self._emit(f"evasion hook {hook}() applied", "info")
            except Exception as exc:  # noqa: BLE001
                self._hook_error(hook, exc)

    def _dispatch_transform(self, hook: str, value: Any, info: Dict) -> Any:
        """Fire a transform hook over `value` (e.g. launch/context kwargs).

        Each handler may mutate `value` in place OR return a replacement; a
        None return keeps the current value. Returns the final value unchanged
        when nothing implements the hook."""
        for fn in self._collect_handlers(hook):
            try:
                result = fn(value, info)
                if result is not None:
                    value = result
                self._emit(f"evasion hook {hook}() applied", "info")
            except Exception as exc:  # noqa: BLE001
                self._hook_error(hook, exc)
        return value

    # -- proxy health ------------------------------------------------------ #
    def _check_proxy_http(self, proxy: Optional[Dict],
                          warnings: List[str]) -> Optional[Dict]:
        """Ask the ProxyManager to prove the exit actually carries HTTP.

        OPTIONAL on the manager: guarded by hasattr, so a ProxyManager without
        check_http() leaves this a no-op and the launch behaves exactly as
        before. The manager is expected to return a dict with at least
        {ok, summary} (and typically stage/status/latency_ms/error/exit_ip/
        geo/timezone), and may enrich `proxy` in place with the exit details.

        COST: a real, blocking network round-trip on the checkout hot path,
        paid before every launch that has a proxy. That is why verify_proxy_http
        is a flag — turn it off when latency matters more than knowing early.
        Called BEFORE the launch on purpose: nothing is running yet, so a strict
        abort here needs no teardown.
        """
        if not proxy or not self.verify_proxy_http:
            return None
        pm = self.proxy_manager
        if pm is None or not hasattr(pm, "check_http"):
            return None
        try:
            health = pm.check_http(proxy)
        except Exception as exc:  # noqa: BLE001
            self._emit(f"proxy health check raised {type(exc).__name__}: {exc}",
                       "warn")
            if self.strict_evasion:
                raise
            warnings.append(f"proxy health check raised {type(exc).__name__}")
            return None
        if not isinstance(health, dict):
            self._emit("proxy health check returned "
                       f"{type(health).__name__}, not an object", "warn")
            return None
        ok = bool(health.get("ok"))
        summary = str(health.get("summary") or ("ok" if ok else "check failed"))
        self._emit(f"proxy health: {summary}", "info" if ok else "warn")
        if not ok:
            warnings.append(f"proxy health: {summary}")
            if self.strict_evasion:
                raise EvasionError(f"proxy health check failed: {summary}")
        return health

    # -- engine version binding -------------------------------------------- #
    def _bind_engine_version(self, playwright) -> None:
        """Tell the FingerprintManager which engine will really serve the pages.

        OPTIONAL on the manager: without set_runtime_browser there is nothing to
        bind, so the probe never runs and this costs nothing — that is the whole
        laziness rule. A failure degrades to "profiles keep their built-in
        version" and never aborts a launch: a version LABEL is not worth losing
        a checkout over.
        """
        fm = self.fingerprint_manager
        if not self.detect_engine_version or not hasattr(fm, "set_runtime_browser"):
            return
        version = self._detect_engine_version(playwright)
        if not version:
            return
        try:
            fm.set_runtime_browser(version)
            self._bound_engine_version = version
            self._emit(f"fingerprints bound to engine {version}", "info")
        except Exception as exc:  # noqa: BLE001
            self._emit(f"set_runtime_browser({version}) raised "
                       f"{type(exc).__name__}: {exc}", "warn")

    def _detect_engine_version(self, playwright) -> Optional[str]:
        """The version string of the browser that a real launch would get.

        Launches one throwaway headless browser and reads `browser.version`.
        Cached MODULE-wide (see _ENGINE_VERSION_CACHE): runner.py builds a
        factory per task, so an instance cache would mean an extra browser
        launch per task instead of one per process.

        Probed with the SAME channel the real launch will ask for, and with the
        same chrome->chromium fallback, so the version we bind profiles to is
        the version that actually launches. Rebinding AFTER a fallback is not
        possible — context_opts are already built and Playwright has no way to
        change them post-launch — so the fallback has to happen here, before
        anything is bound.

        Still not fully reconcilable: a PERSISTENT context exposes no `browser`
        handle, so after that launch there is no way to confirm the running
        version against the bound one. We degrade (the coherence check simply
        does not fire) rather than raise over a version label.
        """
        channel = "chrome" if self.chrome_channel else None
        key = (channel,)
        with _ENGINE_VERSION_LOCK:
            if key in _ENGINE_VERSION_CACHE:
                return _ENGINE_VERSION_CACHE[key]
            version = self._probe_version(playwright, channel)
            _ENGINE_VERSION_CACHE[key] = version
            return version

    def _probe_version(self, playwright, channel: Optional[str]) -> Optional[str]:
        """One throwaway headless launch; None (with a warning) on any failure."""
        kwargs: Dict[str, Any] = {"headless": True}
        if channel:
            kwargs["channel"] = channel
        browser = None
        try:
            browser = playwright.chromium.launch(**kwargs)
            return str(getattr(browser, "version", "") or "") or None
        except Exception as exc:  # noqa: BLE001
            if channel:
                # Mirror _launch's fallback so the probe and the real launch end
                # up on the same binary. Cache the bundled answer under its own
                # key too - it is the same browser either way.
                self._emit(f"engine probe: chrome channel unavailable ({exc}); "
                           "probing chromium", "warn")
                fallback = self._probe_version(playwright, None)
                _ENGINE_VERSION_CACHE[(None,)] = fallback
                return fallback
            self._emit(f"engine version probe failed "
                       f"({type(exc).__name__}: {exc}); profiles keep their "
                       "built-in browser version", "warn")
            return None
        finally:
            self._close_quietly(None, browser)

    def _running_version(self, browser, context) -> str:
        """Version of the browser that actually launched, "" when unknowable.

        Prefers the live handle; falls back to what we bound. A persistent
        context has no browser of its own (and older Playwright does not expose
        context.browser), which is the case that stays unreconciled.
        """
        b = browser
        if b is None and context is not None:
            b = getattr(context, "browser", None)
        try:
            version = str(getattr(b, "version", "") or "")
        except Exception:  # noqa: BLE001 - version is a property; never fatal
            version = ""
        return version or self._bound_engine_version

    def _merge_fp_launch(self, launch_kwargs: Dict) -> None:
        """Fold in optional FingerprintManager launch contributions, if present.

        A replacement FingerprintManager MAY expose launch_args() -> list[str]
        and/or ignore_default_args() -> list[str]; both are additive and
        deduplicated over the built-in _STEALTH_ARGS. Absent methods change
        nothing."""
        fm = self.fingerprint_manager
        try:
            extra = fm.launch_args() if hasattr(fm, "launch_args") else None
            if extra:
                args = launch_kwargs.setdefault("args", [])
                for a in extra:
                    if a not in args:
                        args.append(a)
            ignore = fm.ignore_default_args() if hasattr(fm, "ignore_default_args") else None
            if ignore:
                cur = launch_kwargs.setdefault("ignore_default_args", [])
                for a in ignore:
                    if a not in cur:
                        cur.append(a)
        except Exception as exc:  # noqa: BLE001
            self._emit(f"fingerprint launch extras failed: {exc}", "warn")
            if self.strict_evasion:
                raise

    def _launch_kwargs(self, *, headless: bool, slow_mo: int,
                       proxy: Optional[Dict]) -> Dict[str, Any]:
        """Build the kwargs for chromium.launch*.

        Factored out of create_context, which still runs the configure_launch
        hook over the result, so a hook keeps the last word on every key here.
        """
        kwargs: Dict[str, Any] = {
            "headless": headless,
            "slow_mo": int(slow_mo or 0),
        }
        if self.evasion_enabled:
            kwargs["args"] = list(_STEALTH_ARGS)
            # Drop the "Chrome is being controlled by automated test software" flag.
            kwargs["ignore_default_args"] = ["--enable-automation"]
        if proxy:
            kwargs["proxy"] = proxy
        if self.chrome_channel:
            kwargs["channel"] = "chrome"  # real Chrome: no Chromium tells
        if self.evasion_enabled:
            self._merge_fp_launch(kwargs)
        return kwargs

    def _launch(self, playwright, launch_kwargs: Dict[str, Any],
                context_opts: Dict[str, Any],
                user_data_dir: Optional[str]) -> Tuple[Any, Any, str]:
        """Launch the browser - persistent or not - and report the engine label.

        Returns (browser, context, engine): the persistent path returns
        (None, context, ...) because the context owns the process, and the plain
        path returns (browser, None, ...), leaving new_context() to the caller.
        The engine label is RETURNED rather than written to self.last_engine, so
        create_context can publish every last_* value in one place at the end.

        ONLY the launch call is inside the try. Wrapping new_context() in the
        same try meant a bad context option relaunched a second browser, leaked
        the first, and reported the whole thing as a missing Chrome channel; a
        failure that is not the channel now re-raises unchanged. The fallback is
        on BOTH paths: runner.py passes user_data_dir from the task profile, so
        real runs go persistent and would otherwise hard-fail on any machine
        with no Chrome installed.
        """
        persistent = bool(user_data_dir)
        if persistent:
            Path(user_data_dir).mkdir(parents=True, exist_ok=True)
            # Cross-process guard: ProfileLeases (raindance.core.leases) only
            # sees other tasks in THIS Python process. Verified directly that
            # Playwright's launch_persistent_context does not itself refuse a
            # second launch on an already-open profile dir — it succeeds
            # silently, which is how a second `python app.py`, or a Chromium
            # orphaned by a crash, could end up writing the same cart and
            # cookie jar as a live session. Fail-open by design: see the
            # module docstring on CrossProcessGuard for exactly what "can't
            # tell" resolves to and why.
            from raindance.core.leases import CrossProcessGuard
            refusal = CrossProcessGuard.check(user_data_dir)
            if refusal:
                raise EvasionError(refusal)

        def _start(kwargs: Dict[str, Any]) -> Tuple[Any, Any]:
            if persistent:
                # Persistent: launch flags and context options go in one call.
                ctx = playwright.chromium.launch_persistent_context(
                    user_data_dir,
                    **kwargs,
                    **context_opts,
                )
                CrossProcessGuard.acquire(user_data_dir)
                return None, ctx
            return playwright.chromium.launch(**kwargs), None

        browser = context = None
        try:
            browser, context = _start(launch_kwargs)
        except Exception as exc:  # noqa: BLE001
            if launch_kwargs.get("channel") != "chrome":
                raise
            # A launch that raised should not have left anything running, but
            # never leak it if it did.
            self._close_quietly(context, browser)
            self._emit(f"chrome channel unavailable ({exc}); using chromium", "warn")
            fallback = {k: v for k, v in launch_kwargs.items() if k != "channel"}
            browser, context = _start(fallback)
            engine = "chromium"
        else:
            engine = (
                self._engine_label(browser, context)
                if launch_kwargs.get("channel") == "chrome" else "chromium"
            )
        return browser, context, engine

    def _engine_label(self, browser, context) -> str:
        """Engine label for a channel launch: "chrome channel <version>".

        The persistent path has no browser handle of its own, so ask the context
        for one; a Playwright old enough not to expose it simply reports no
        version rather than failing the launch over a log label.
        """
        b = browser
        if b is None and context is not None:
            b = getattr(context, "browser", None)
        try:
            version = getattr(b, "version", "") or ""
        except Exception:  # noqa: BLE001 - version is a property; never fatal
            version = ""
        return f"chrome channel {version}" if version else "chrome channel"

    def _close_quietly(self, context, browser) -> None:
        """Close context then browser, ignoring close errors.

        Used by every path that fails with a browser already running: teardown
        must never mask the exception that caused it.
        """
        for obj in (context, browser):
            if obj is None:
                continue
            try:
                obj.close()
            except Exception:  # noqa: BLE001 - already tearing down
                pass

    def _pick_proxy(self, account_id: str, for_harvester: bool) -> Optional[Dict]:
        if not (self.evasion_enabled and self.proxy_manager):
            return None
        try:
            return self.proxy_manager.get_proxy(account_id, for_harvester=for_harvester)
        except TypeError:
            # Tolerate an older ProxyManager without the for_harvester kwarg.
            return self.proxy_manager.get_proxy(account_id)

    def _apply_stealth(self, context, profile: Any = None) -> str:
        """Inject the custom fingerprint script. This is the ONLY stealth layer.

        playwright-stealth is deliberately never applied: it ran second and
        overwrote parts of this script's patches, leaks a well-known public
        stealth signature of its own, and adds almost nothing on headed real
        Chrome. The custom script is the single source of truth.
        """
        notes: List[str] = []

        script = ""
        if not hasattr(type(self.fingerprint_manager), "init_script"):
            notes.append("no init_script on this FingerprintManager")
        else:
            try:
                script = self._init_script(profile)
            except Exception as exc:  # noqa: BLE001 - incl. failures *inside*
                return self._problem(
                    f"init_script raised {type(exc).__name__}: {exc}", exc)
        if script:
            try:
                context.add_init_script(script)
                notes.append("init_script ok")
            except Exception as exc:  # noqa: BLE001
                return self._problem(
                    f"add_init_script failed {type(exc).__name__}: {exc}", exc)
        notes.append("custom stealth only (playwright-stealth disabled)")
        return " \u00b7 ".join(notes)

    def _init_script(self, profile: Any) -> str:
        """init_script(profile) on a manager that takes one, else init_script().

        Detected, not assumed: the shipped FingerprintManager's init_script()
        takes no argument, and a TypeError raised *inside* a manager that does
        accept one must not be misread as a signature mismatch.
        """
        fn = self.fingerprint_manager.init_script
        takes_profile = True
        try:
            inspect.signature(fn).bind(profile)
        except (TypeError, ValueError):
            takes_profile = False
        return fn(profile) if takes_profile else fn()

    def _natural_snapshot(self, context) -> Dict[str, Any]:
        """Read the tells off the browser BEFORE anything has been injected.

        Gives verification something to compare against: "WebGL reports Apple
        M2" means nothing on its own, but "the untouched browser said
        SwiftShader and the patched one says Apple M2" says the patch took.

        MUST run before _apply_stealth — add_init_script only affects pages
        opened after it, so a page opened any later is already patched. The page
        this opens is deliberately LEFT OPEN: create_context's stale-page sweep
        closes it along with anything a persistent profile restored, which is
        exactly what keeps it from leaking. Never raises — a missing baseline
        degrades the diagnosis, it does not fail the launch.
        """
        try:
            page = context.new_page()
            page.goto(_BLANK)
            snap = page.evaluate(_PROBE_JS)
        except Exception as exc:  # noqa: BLE001 - diagnostics are never fatal
            return {"error": f"{type(exc).__name__}: {exc}"}
        if not isinstance(snap, dict):
            return {"error": f"probe returned {type(snap).__name__}, not an object"}
        return snap

    @staticmethod
    def _pair(snap: Dict, profile: Any, snap_key: str, profile_key: str):
        """(live, configured) when BOTH sides carry the value, else None.

        A check missing either side is SKIPPED, never failed. An older
        FingerprintManager whose profiles carry no `chrome_major`, or a probe
        that did not report a field, is a GAP IN THE DIAGNOSIS — not evidence
        that the browser is misconfigured. Calling it a failure would abort
        every launch under strict_evasion for no reason at all.
        """
        if not isinstance(profile, dict) or not isinstance(snap, dict):
            return None
        if profile.get(profile_key) is None:
            return None
        if snap.get(snap_key) is None:
            return None
        return snap[snap_key], profile[profile_key]

    def _verify(self, page, profile: Any = None,
                natural: Optional[Dict] = None,
                engine_version: str = "") -> Dict[str, Any]:
        """Read the automation tells back off a live page.

        ALWAYS returns {"summary": str, "snapshot": dict, "warnings": list,
        "natural": dict} - the caller indexes ['summary'], so a bare string here
        was a TypeError in exactly the degraded case the string existed to
        report. Verification only reports; it patches nothing. It runs last, so
        under strict_evasion a failed check aborts the launch like any other
        evasion failure.

        The summary says "configured browser checks passed", not "all checks
        passed": this probes the tells this factory sets out to remove and the
        values it configured. It is not a verdict on whether a site's bot
        detection is fooled, and must not be read as one.
        """
        try:
            page.goto(_BLANK)
            snap = page.evaluate(_PROBE_JS)
            if not isinstance(snap, dict):
                raise TypeError(
                    f"probe returned {type(snap).__name__}, not an object")
        except Exception as exc:  # noqa: BLE001
            # The probe itself could not run. Route it through _problem so
            # strict mode stays centralised there (it raises), but wrap the
            # NOT APPLIED string it returns in the dict the caller expects.
            return {
                "summary": self._problem(
                    f"stealth verification could not run "
                    f"{type(exc).__name__}: {exc}", exc),
                "snapshot": {},
                "warnings": [],
                "natural": natural or {},
            }

        fails: List[str] = []
        warns: List[str] = []
        if snap.get("webdriver") is True:
            fails.append("navigator.webdriver is true")
        if snap.get("ua_headless"):
            fails.append("UA still says HeadlessChrome")
        if (snap.get("plugins") or 0) < 1:
            fails.append("no navigator.plugins (headless tell)")
        if snap.get("notification") == "denied":
            fails.append("Notification.permission denied (headless tell)")
        if isinstance(snap.get("webgl"), str) and (
            "SwiftShader" in snap["webgl"] or "llvmpipe" in snap["webgl"]
        ):
            fails.append(f"software WebGL renderer ({snap['webgl'][:40]})")

        # -- coherence: what the browser REPORTS vs what we CONFIGURED ------ #
        # Self-consistency diagnostics on values this factory itself set. A
        # mismatch means a patch did not take (the context option or the init
        # script was overridden, or the profile changed after the options were
        # built), not that the profile is "wrong". Every one is _pair-guarded:
        # a missing profile key or a field the probe does not report skips the
        # check. NOTE two of these - languages and platform - are inert with the
        # current _PROBE_JS field set, which reports neither; they light up on
        # their own the day the probe carries those fields.
        pair = self._pair(snap, profile, "languages", "locale")
        if pair and str(pair[0]).split(",")[0] != pair[1]:
            fails.append(f"navigator.languages {str(pair[0]).split(',')[0]!r} "
                         f"disagrees with profile locale {pair[1]!r}")

        pair = self._pair(snap, profile, "platform", "platform")
        if pair and pair[0] != pair[1]:
            fails.append(f"navigator.platform {pair[0]!r} "
                         f"disagrees with profile {pair[1]!r}")

        pair = self._pair(snap, profile, "hardware_concurrency",
                          "hardware_concurrency")
        if pair and pair[0] != pair[1]:
            fails.append(f"navigator.hardwareConcurrency {pair[0]} "
                         f"disagrees with profile {pair[1]}")

        pair = self._pair(snap, profile, "device_memory", "device_memory")
        if pair and pair[0] != pair[1]:
            fails.append(f"navigator.deviceMemory {pair[0]} "
                         f"disagrees with profile {pair[1]}")

        pair = self._pair(snap, profile, "timezone", "timezone_id")
        if pair and pair[0] != pair[1]:
            fails.append(f"timezone {pair[0]!r} "
                         f"disagrees with profile {pair[1]!r}")

        pair = self._pair(snap, profile, "ua_chrome_major", "chrome_major")
        if pair and str(pair[0]) != str(pair[1]):
            fails.append(f"UA Chrome major {pair[0]} "
                         f"disagrees with profile chrome_major {pair[1]}")

        # UA major vs the engine actually serving the pages. A WARNING, never a
        # failure: this compares an identity's version LABEL against the binary
        # Playwright happened to start, the same thing _detect_engine_version
        # degrades over rather than raising. Skipped when the version is unknown
        # - which is every persistent context, the case that cannot be
        # reconciled after the launch.
        ua_major = snap.get("ua_chrome_major")
        running_major = str(engine_version or "").split(".")[0]
        if ua_major is not None and running_major.isdigit() \
                and str(ua_major) != running_major:
            warns.append(f"UA says Chrome {ua_major} but the running engine is "
                         f"{engine_version}")

        # Natural (pre-injection) vs injected: did the patch actually change
        # what the page sees, or is the browser still showing its real hardware?
        if isinstance(natural, dict) and natural and not natural.get("error"):
            nat_webgl = natural.get("webgl")
            cur_webgl = snap.get("webgl")
            if isinstance(nat_webgl, str) and isinstance(cur_webgl, str) and nat_webgl:
                if nat_webgl != cur_webgl:
                    warns.append(f"WebGL renderer masked: {nat_webgl[:40]!r} "
                                 f"-> {cur_webgl[:40]!r}")
                else:
                    warns.append("WebGL renderer NOT masked: still the real "
                                 f"{nat_webgl[:40]!r}")

        summary = _VERIFY_OK if not fails else "FAILED: " + "; ".join(fails)
        if fails:
            if self.strict_evasion:
                raise EvasionError("stealth verification: " + summary)
            log.warning("stealth verification: %s", summary)
        for w in warns:
            log.info("stealth verification note: %s", w)
        return {"summary": summary, "snapshot": snap,
                "warnings": warns, "natural": natural or {}}

    def _emit(self, text: str, level: str = "info") -> None:
        log.info(text) if level == "info" else log.warning(text)
        if self.bus is not None:
            try:
                self.bus.log(text, level)
            except Exception:  # noqa: BLE001 - logging must never break a launch
                pass

    def _problem(self, msg: str, cause: Optional[BaseException] = None,
                 notes: Optional[List[str]] = None) -> str:
        # The one write to a last_* attribute that does NOT happen in
        # create_context's single publish block, and deliberately so: under
        # strict_evasion the next line raises, create_context never reaches that
        # block, and this string is the only record of why the launch aborted.
        # The NOT APPLIED substring is a contract - runner.py and
        # plugins/evasion.py both key off it.
        self._emit(f"evasion could not be applied: {msg}", "warn")
        self.last_stealth = f"NOT APPLIED - {msg}"
        if self.strict_evasion:
            raise EvasionError(msg) from cause
        log.warning("evasion degraded: %s", msg)
        parts = list(notes or [])
        parts.append(f"NOT APPLIED - {msg}")
        return " \u00b7 ".join(parts)
