"""Create Playwright browser contexts with evasion (stealth + proxy + coherence).

Public surface unchanged: same signature, sync API, (browser, context,
page) return, caller-owned Playwright lifetime.

Review fixes beyond the previous revision:
* __init__ (was misnamed and never ran) [review #1];
* per-session SessionInfo attached to the context — no more describe()-of-
  the-wrong-profile or global last_* races [review #2, race];
* fingerprint Chrome version is bound to the engine that actually launched
  (probed once, cached) [review #4];
* verification now cross-checks snapshot values against the profile AND the
  natural pre-injection browser state; summary is honest:
  "configured browser checks passed", not "all checks passed"
  [review #6, #7];
* proxy health is a real HTTPS request through the proxy, not a TCP ping
  [review #8].

Tactics: use Patchright in place of Playwright when the caller passes a
patchright sync_playwright() object — its source-level CDP patches
(Runtime.enable, Console.enable, automation flags, init-script
injection via routes) survive scanners that catch runtime monkey-patching.
When Patchright is detected we skip playwright-stealth to avoid
double-patching [7][8][9].
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    from raindance.evasion.fingerprint_manager import FingerprintManager
    from raindance.evasion.proxy_manager import ProxyManager
except ImportError:
    from fingerprint_manager import FingerprintManager
    from proxy_manager import ProxyManager

log = logging.getLogger(__name__)

_STEALTH_ARGS: List[str] = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-infobars",
    "--disable-dev-shm-usage",
    "--disable-popup-blocking",
    "--password-store=basic",
    "--use-mock-keychain",
]

_BLANK = "about:blank"


class EvasionError(RuntimeError):
    """Evasion was switched on but could not actually be applied."""


@dataclass
class SessionInfo:
    """Everything this factory decided for ONE create_context() call."""
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


class BrowserFactory:
    def __init__(  # FIX review #1: was "def init"
        self,
        proxy_manager: Optional[ProxyManager] = None,
        fingerprint_manager: Optional[FingerprintManager] = None,
        evasion_enabled: bool = False,
        strict_evasion: bool = True,
        on_step: Optional[Callable[[str], None]] = None,
        chrome_channel: bool = True,
        verify_proxy_http: bool = True,
    ):
        self.proxy_manager = proxy_manager
        self.fingerprint_manager = fingerprint_manager or FingerprintManager()
        self.evasion_enabled = bool(evasion_enabled)
        self.strict_evasion = bool(strict_evasion)
        self.on_step = on_step
        self.chrome_channel = bool(chrome_channel)
        self.verify_proxy_http = bool(verify_proxy_http)
        self._engine_version_cache: Optional[str] = None
        # Session-scoped result; legacy globals kept for old callers only
        # (documented: racy under threads — use session_info(context)).
        self.last_session: Optional[SessionInfo] = None
        self.last_proxy: Optional[Dict] = None
        self.last_fingerprint: str = ""
        self.last_stealth: str = ""
        self.last_engine: str = ""

    def configure(self, *, evasion_enabled: Optional[bool] = None,
                  proxy_manager: Optional[ProxyManager] = None,
                  strict_evasion: Optional[bool] = None,
                  chrome_channel: Optional[bool] = None,
                  verify_proxy_http: Optional[bool] = None) -> None:
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

    def session_info(self, context) -> Optional[SessionInfo]:
        """Race-free accessor: read this session's decisions from its context."""
        return getattr(context, "_evasion_info", None)

    def _step(self, msg: str) -> None:
        log.info(msg)
        if self.on_step is not None:
            self.on_step(msg)

    # ------------------------------------------------------------------ #
    def create_context(
        self, playwright, *, account_id: str = "default", headless: bool = False,
        user_data_dir: Optional[str] = None, slow_mo: int = 0,
        viewport: Optional[Dict] = None, user_agent: Optional[str] = None,
        for_harvester: bool = False, keep_open_hint: bool = False,
    ) -> Tuple[Any, Any, Any]:
        """Launch a browser/context/page. Returns (browser, context, page).
        For persistent profiles browser is None (context owns the process)."""
        if self.evasion_enabled and headless:
            self._step("note: headful is strongly recommended for Akamai/"
                       "Queue-it/DataDome; headless adds SwiftShader/webdriver risk")

        is_patchright = "patchright" in (type(playwright).__module__ or "").lower()
        if is_patchright:
            self._step("engine: patchright (source-level CDP patches active)")

        # -- 1. proxy pick + REAL health check (review #8) ----------------
        proxy = self._pick_proxy(account_id, for_harvester)
        proxy_health = None
        if proxy is not None and self.verify_proxy_http and \
                hasattr(self.proxy_manager, "check"):
            proxy_health = self.proxy_manager.check(proxy)
            self._step(f"proxy health: {proxy_health.get('summary')}")
            if not proxy_health.get("ok") and self.strict_evasion:
                raise EvasionError(f"proxy health: {proxy_health.get('summary')}")
        self._step(f"proxy: {proxy['server'] if proxy else 'none'}")

        # -- 2. bind fingerprint to the REAL engine version (review #4) ---
        if self.evasion_enabled:
            version = self._detect_engine_version(playwright)
            if version and hasattr(self.fingerprint_manager, "set_runtime_browser"):
                self.fingerprint_manager.set_runtime_browser(version)
                self._step(f"fingerprint bound to engine {version}")

        # -- 3. sticky profile, aligned to the observed exit ---------------
        profile = None
        if self.evasion_enabled:
            profile = self.fingerprint_manager.profile_for(account_id)
            if hasattr(self.fingerprint_manager, "align_to_proxy"):
                self.fingerprint_manager.align_to_proxy(profile, proxy)
            context_opts = self.fingerprint_manager.context_options(
                override={"viewport": viewport, "user_agent": user_agent,
                          "_profile": profile})
            fingerprint_desc = (
                self.fingerprint_manager.describe(profile)
                if hasattr(self.fingerprint_manager, "describe")
                else "static stub")                    # FIX review #2
        else:
            context_opts = {}
            if viewport:
                context_opts["viewport"] = viewport
            if user_agent:
                context_opts["user_agent"] = user_agent
            fingerprint_desc = ""
        self._step(f"fingerprint: {fingerprint_desc or 'disabled'}")

        # -- 4. launch ------------------------------------------------------
        browser = None
        if user_data_dir:
            Path(user_data_dir).mkdir(parents=True, exist_ok=True)
            context = playwright.chromium.launch_persistent_context(
                user_data_dir,
                **self._launch_kwargs(proxy, headless, slow_mo),
                **context_opts,
            )
            self._step(f"launched persistent context: {user_data_dir}")
        else:
            browser, context = self._launch(
                playwright, proxy, headless, slow_mo, context_opts)
            self._step("launched fresh (non-persistent) context")

        # -- 5. NATURAL snapshot BEFORE any init script (review #7) --------
        natural: Dict[str, Any] = {}
        if self.evasion_enabled:
            natural = self._natural_snapshot(context)

        # -- 6. stealth ------------------------------------------------------
        if self.evasion_enabled:
            # Skip playwright-stealth under Patchright: double patching adds
            # detectable Runtime-API overrides [8][9].
            self.last_stealth = self._apply_stealth(
                context, profile, use_stealth_lib=not is_patchright)
            self._step(f"stealth: {self.last_stealth}")
        else:
            self.last_stealth = ""
            self._step("stealth: skipped (evasion disabled)")

        # add_init_script only affects pages created AFTER it runs; the
        # about:blank page a persistent context opens must not survive.
        for stale in list(context.pages):
            try:
                stale.close()
            except Exception:
                pass

        # -- 7. verify on a genuinely post-stealth page ---------------------
        verdict: Dict[str, Any] = {"summary": "not run", "snapshot": {}}
        if self.evasion_enabled:
            probe = context.new_page()
            verdict = self._verify(probe, profile, natural)
            self._step(f"verify: {verdict['summary']}")
            self.last_stealth = f"{self.last_stealth} · verify {verdict['summary']}"

        page = context.pages[0] if context.pages else context.new_page()
        self._step(f"page ready ({len(context.pages)} open)")

        session = SessionInfo(
            account_id=account_id, proxy=proxy, profile=profile,
            fingerprint=fingerprint_desc, stealth=self.last_stealth,
            engine=self.last_engine, verify_summary=verdict.get("summary", ""),
            verify=verdict, natural_snapshot=natural,
            proxy_health=proxy_health,
            warnings=verdict.get("warnings", []),
        )
        try:
            context._evasion_info = session
        except Exception:
            pass
        self.last_session = session
        self.last_proxy = proxy
        self.last_fingerprint = fingerprint_desc
        return browser, context, page

    # -- launching ------------------------------------------------------- #
    def _launch_kwargs(self, proxy, headless, slow_mo) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {"headless": headless, "slow_mo": int(slow_mo or 0)}
        if self.evasion_enabled:
            kwargs["args"] = list(_STEALTH_ARGS)
            kwargs["ignore_default_args"] = ["--enable-automation"]
        if proxy:
            kwargs["proxy"] = proxy
        if self.chrome_channel:
            kwargs["channel"] = "chrome"
        return kwargs

    def _detect_engine_version(self, playwright) -> Optional[str]:
        """Launch a throwaway browser once, read its real version, cache it.
        This is what makes UA/client-hints version claims TRUE (review #4)."""
        if self._engine_version_cache:
            return self._engine_version_cache
        kwargs: Dict[str, Any] = {"headless": True}
        if self.chrome_channel:
            kwargs["channel"] = "chrome"
        try:
            probe = playwright.chromium.launch(**kwargs)
        except Exception as exc:
            self._step(f"engine version probe failed ({exc}); using fallback majors")
            return None
        try:
            self._engine_version_cache = probe.version
            return self._engine_version_cache
        finally:
            try:
                probe.close()
            except Exception:
                pass

    def _launch(self, playwright, proxy, headless, slow_mo, context_opts):
        kwargs = self._launch_kwargs(proxy, headless, slow_mo)
        try:
            browser = playwright.chromium.launch(**kwargs)
            context = browser.new_context(**context_opts)
            self.last_engine = f"chrome channel {browser.version}"
        except Exception as exc:
            if "chrome" in (kwargs.get("channel") or ""):
                self._step(f"chrome channel unavailable ({exc}); using chromium")
                kwargs.pop("channel", None)
                self._engine_version_cache = None  # different binary now
                browser = playwright.chromium.launch(**kwargs)
                context = browser.new_context(**context_opts)
                self.last_engine = f"chromium {browser.version}"
            else:
                raise
        self._step(f"engine: {self.last_engine}")
        return browser, context

    # -- stealth ------------------------------------------------------------ #
    def _apply_stealth(self, context, profile=None, use_stealth_lib: bool = True) -> str:
        notes: List[str] = []
        script = ""
        try:
            if hasattr(self.fingerprint_manager, "init_script"):
                script = self.fingerprint_manager.init_script(profile)
            else:
                notes.append("no init_script on this FingerprintManager")
        except Exception as exc:
            return self._problem(f"init_script raised {type(exc).__name__}: {exc}", exc)
        if script:
            try:
                context.add_init_script(script)
                notes.append("init_script ok")
            except Exception as exc:
                return self._problem(
                    f"add_init_script failed {type(exc).__name__}: {exc}", exc)
        if not use_stealth_lib:
            notes.append("playwright-stealth skipped (patchright provides "
                         "source-level patches)")
            return " · ".join(notes)
        try:
            import playwright_stealth
        except ImportError as exc:
            return self._problem(
                "playwright-stealth is not installed "
                "(pip install playwright-stealth)", exc, notes)
        version = getattr(playwright_stealth, "__version__",
                          getattr(playwright_stealth, "version", "unknown"))
        stealth_cls = getattr(playwright_stealth, "Stealth", None)
        try:
            if stealth_cls is not None:
                stealth = stealth_cls()
                fn = getattr(stealth, "apply_stealth_sync", None)
                if fn is None:
                    fn = getattr(stealth, "stealth_sync", None) or \
                        getattr(playwright_stealth, "stealth_sync", None)
                if fn is None:
                    return self._problem(
                        f"playwright-stealth {version} exposes no known entrypoint",
                        None, notes)
                fn(context)
                notes.append(f"stealth lib ok [playwright-stealth {version}]")
            else:
                legacy = getattr(playwright_stealth, "stealth_sync", None)
                if legacy is None:
                    return self._problem(
                        f"playwright-stealth {version} exposes no known entrypoint",
                        None, notes)
                legacy(context)
                notes.append(f"stealth_sync ok [playwright-stealth {version}]")
        except Exception as exc:
            return self._problem(
                f"playwright-stealth failed {type(exc).__name__}: {exc}", exc, notes)
        return " · ".join(notes)

    # -- verification ------------------------------------------------------ #
    _PROBE_JS = """
    () => ({
      webdriver: navigator.webdriver,
      ua_headless: /Headless/i.test(navigator.userAgent),
      ua_chrome_major: (navigator.userAgent.match(/Chrome\\/(\\d+)/) || [])[1] || null,
      plugins: navigator.plugins ? navigator.plugins.length : -1,
      notification: (typeof Notification !== 'undefined')
        ? Notification.permission : 'absent',
      webgl: (() => {
        try {
          const c = document.createElement('canvas');
          const gl = c.getContext('webgl') || c.getContext('webgl2');
          if (!gl) return 'no-webgl';
          const ext = gl.getExtension('WEBGL_debug_renderer_info');
          return ext ? String(gl.getParameter(ext.UNMASKED_RENDERER_WEBGL))
                     : String(gl.getParameter(gl.RENDERER));
        } catch (e) { return 'error'; }
      })(),
      languages: (navigator.languages || []).join(','),
      platform: navigator.platform,
      hardware_concurrency: navigator.hardwareConcurrency,
      device_memory: navigator.deviceMemory,
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
      screen: [screen.width, screen.height],
      ua_data_platform: (navigator.userAgentData && navigator.userAgentData.platform) || null,
    })
    """

    def _natural_snapshot(self, context) -> Dict:
        """Probe the UNTOUCHED browser state before any init script runs.
        This is what makes the WebGL diagnostic honest: we can then state
        'injected renderer active; natural renderer was X' instead of
        pretending the patched value proves environment consistency."""
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(self._BLANK)
            return dict(page.evaluate(self._PROBE_JS))
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}

    def _verify(self, page, profile=None, natural=None) -> Dict:
        """Interrogate the live, stealthed page and cross-check coherence.
        'Configured browser checks passed' means exactly that: the values we
        configured are live AND internally consistent. It does NOT claim the
        host GPU really is the claimed one (see natural_snapshot)."""
        try:
            page.goto(self._BLANK)
            snap = dict(page.evaluate(self._PROBE_JS))
        except Exception as exc:
            return self._problem(
                f"verification could not run ({type(exc).__name__}: {exc})", exc)

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
                "SwiftShader" in snap["webgl"] or "llvmpipe" in snap["webgl"]):
            fails.append(f"software WebGL renderer ({snap['webgl'][:40]})")

        # Honesty about the WebGL result (review #7): the post-stealth value
        # is our injection; the natural value is the environment's truth.
        if profile and natural and isinstance(natural.get("webgl"), str):
            if natural["webgl"] != snap.get("webgl"):
                if "SwiftShader" in natural["webgl"] or "llvmpipe" in natural["webgl"]:
                    warns.append(
                        f"natural GPU is software ({natural['webgl'][:40]}); "
                        "injected renderer masks it — headful/real GPU recommended")
            else:
                warns.append("natural renderer unchanged by profile "
                             "(host GPU may already match)")
        if natural and natural.get("webdriver") is True:
            warns.append("natural navigator.webdriver was true (now patched)")

        # Coherence: snapshot must AGREE with the profile (review #6).
        if profile:
            if snap.get("languages", "").split(",")[0] != profile["locale"]:
                fails.append(f"languages {snap.get('languages')!r} != locale "
                             f"{profile['locale']!r}")
            if snap.get("platform") != profile["platform"]:
                fails.append(f"platform {snap.get('platform')!r} != profile")
            if snap.get("hardware_concurrency") != profile["hardware_concurrency"]:
                fails.append("hardwareConcurrency != profile")
            if snap.get("device_memory") != profile["device_memory"]:
                fails.append("deviceMemory != profile")
            if snap.get("timezone") != profile["timezone_id"]:
                fails.append(f"timezone {snap.get('timezone')!r} != "
                             f"{profile['timezone_id']!r}")
            if str(snap.get("ua_chrome_major")) != str(profile["chrome_major"]):
                fails.append(f"UA major {snap.get('ua_chrome_major')} != "
                             f"profile {profile['chrome_major']}")
            if (self._engine_version_cache and
                    str(snap.get("ua_chrome_major")) !=
                    self._engine_version_cache.split(".")[0]):
                fails.append("UA major != running engine major")

        summary = ("configured browser checks passed" if not fails
                   else "FAILED: " + "; ".join(fails))
        if fails and self.strict_evasion:
            raise EvasionError("stealth verification: " + summary)
        if fails:
            log.warning("stealth verification: %s", summary)
        return {"summary": summary, "snapshot": snap, "warnings": warns}

    # -- internals ---------------------------------------------------------- #
    def _pick_proxy(self, account_id: str, for_harvester: bool) -> Optional[Dict]:
        if not (self.evasion_enabled and self.proxy_manager):
            return None
        try:
            return self.proxy_manager.get_proxy(
                account_id, for_harvester=for_harvester)
        except TypeError:
            return self.proxy_manager.get_proxy(account_id)

    def _problem(self, msg: str, cause: Optional[BaseException] = None,
                 notes: Optional[List[str]] = None) -> str:
        if self.strict_evasion:
            raise EvasionError(msg) from cause
        log.warning("evasion degraded: %s", msg)
        parts = list(notes or [])
        parts.append(f"NOT APPLIED - {msg}")
        return " · ".join(parts)
