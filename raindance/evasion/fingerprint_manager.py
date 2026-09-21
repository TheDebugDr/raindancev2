"""Realistic, internally-consistent browser fingerprints for Playwright.

A fingerprint is only useful when its parts agree. A macOS user agent paired
with a Windows `navigator.platform`, or 16 GB of `deviceMemory` on a machine
reporting 2 CPU cores, is a *louder* signal than no spoofing at all. So this
manager never randomizes fields independently — it picks ONE coherent device
profile and derives every value from it: user agent, platform, viewport,
screen, device pixel ratio, timezone, locale, CPU/'memory', and the WebGL
vendor/renderer strings that match that GPU.

Seed the manager with a profile's ``fingerprint_seed`` and the same identity
comes back on every launch, so a sticky proxy exit IP is paired with a stable
fingerprint instead of a fresh one each session (which is itself a tell).

Two surfaces:
  * ``context_options(override=…)`` — only valid Playwright ``new_context`` /
    ``launch_persistent_context`` kwargs. ANY key the caller puts in ``override``
    is forwarded (caller wins over the profile-derived value), so new context
    options need no edit here; keys starting with ``_`` are reserved for this
    module and are consumed instead (``_profile`` is the one in use today).
  * ``init_script(profile=None)`` — JS the BrowserFactory injects before any page
    loads, to align ``navigator`` / WebGL with those same kwargs.

Per account: ``profile_for(account_id)`` hands back one sticky profile per
account — the same dict every time, so an account that goes away and comes back
comes back as the same machine — and ``align_to_proxy(fp, proxy)`` nudges that
profile's locale/languages/timezone to agree with the proxy exit's country
(reversibly: a profile remembers the locale, languages and timezone it was born
with). Pass the result through ``context_options(override={"_profile": fp})``
and ``init_script(fp)`` to launch as that identity; call either bare and you get
the default profile.

Versions are STRUCTURED, never reparsed: every profile carries ``chrome_full``,
``chrome_major``, ``sec_ch_ua`` and ``brands``, and the user agent is derived
FROM the version rather than the version being scraped back out of the UA.
``set_runtime_browser(full_version)`` binds all of that to the browser binary
that will actually serve the pages, so the UA, the ``Sec-CH-UA`` header and the
real engine tell one story — which is the only reason forging that header is
safe here. Until the factory calls it, the pool's own pinned version stands.
"""
from __future__ import annotations

import json
import random
import re
import zlib
from typing import Any, Dict, List, Optional

# Pinned to the bundled Chromium's major so the UA, the real engine, and the
# Sec-CH-UA client hints Chromium emits all agree. A UA that claims an older
# Chrome than the build actually running is a mismatch anti-bot systems read
# directly off the client hints — louder than a slightly-newer version string.
# Bump this whenever the Playwright Chromium is upgraded (check navigator.userAgent).
# This is only the FALLBACK: set_runtime_browser() makes the binary that really
# launched authoritative, which is the version worth trusting.
_CHROME = "148.0.0.0"
_CHROME_MAJOR = _CHROME.split(".")[0]


def _sec_ch_ua(major: str) -> str:
    """The ``Sec-CH-UA`` value for a Chrome major, in Chrome's own brand order."""
    return (
        f'"Chromium";v="{major}", "Google Chrome";v="{major}", "Not_A Brand";v="24"'
    )


def _brands(major: str) -> List[Dict[str, str]]:
    """``brands`` list matching _sec_ch_ua() entry for entry, in the same order."""
    return [
        {"brand": "Chromium", "version": major},
        {"brand": "Google Chrome", "version": major},
        {"brand": "Not_A Brand", "version": "24"},
    ]


def _ua_chrome_version(full: str, major: str) -> str:
    """The version Chrome actually writes into its UA string.

    Since the UA reduction, Chrome reports ``<major>.0.0.0`` there and keeps the
    build/patch numbers for the high-entropy client hints only — which is why the
    pool below is pinned as ``148.0.0.0`` rather than a real four-part build. The
    full version stays on the profile as ``chrome_full``. One function so a
    different convention is one edit, not a hunt through the module.
    """
    return f"{major}.0.0.0"


# Only the Chrome token of a UA; the OS half is never touched, so a rewrite
# cannot turn a Mac profile into a Windows one.
_UA_CHROME_RE = re.compile(r"Chrome/[0-9][0-9.]*")


def _ua_with_version(user_agent: str, version: str) -> str:
    """``user_agent`` with its Chrome version swapped for ``version``."""
    ua = user_agent or ""
    if not version or not _UA_CHROME_RE.search(ua):
        return ua
    return _UA_CHROME_RE.sub(f"Chrome/{version}", ua, count=1)


def _languages_for(locale: str) -> List[str]:
    """``navigator.languages`` for a locale: the locale, then its base tag.

    The single source of truth for languages, so the injected script, the
    ``Accept-Language`` header and the context ``locale`` cannot drift apart:
    ``languages[0]`` is always the locale itself.
    """
    loc = locale or "en-US"
    base = loc.split("-")[0].lower()
    return [loc, base] if base != loc.lower() else [loc]


def _accept_language(languages: List[str]) -> str:
    """``Accept-Language`` built from the SAME list navigator.languages reports.

    A header that advertises one set of languages while the page object reports
    another is a free mismatch; deriving both from one list removes it. With the
    default en-US profile this is byte-for-byte the old ``en-US,en;q=0.9``.
    """
    langs = [x for x in (languages or []) if x]
    if not langs:
        return "en-US,en;q=0.9"
    out = [langs[0]]
    q = 9
    for lang in langs[1:]:
        out.append(f"{lang};q=0.{q}")
        q = max(1, q - 1)
    return ",".join(out)


def _renderer_head(renderer: str) -> str:
    """First segment of a WebGL renderer string — 'Apple', 'NVIDIA', 'Intel'."""
    text = (renderer or "").strip()
    if not text:
        return "?"
    head = text.split("(", 1)[1] if "(" in text else text
    return head.split(",", 1)[0].strip().rstrip(")") or "?"


def _apply_version(profile: Dict[str, Any], full: str) -> None:
    """Write the structured version fields onto ``profile`` and rewrite its UA.

    Everything version-shaped is derived here from one input, so the four fields
    can never disagree with each other or with the user agent. The OS half of the
    UA is preserved — a Mac profile stays a Mac profile.
    """
    version = str(full or "").strip() or _CHROME
    major = version.split(".")[0] or _CHROME_MAJOR
    profile["chrome_full"] = version
    profile["chrome_major"] = major
    profile["sec_ch_ua"] = _sec_ch_ua(major)
    profile["brands"] = _brands(major)
    if profile.get("user_agent"):
        profile["user_agent"] = _ua_with_version(
            profile["user_agent"], _ua_chrome_version(version, major))


def _copy_value(value: Any) -> Any:
    """Deep-enough copy so a drawn profile shares nothing mutable with the pool."""
    if isinstance(value, dict):
        return {k: _copy_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_copy_value(v) for v in value]
    return value


# Each profile is a coherent, real-world device. Add one to widen the pool.
_PROFILES: List[Dict] = [
    {
        "label": "macbook-pro-14-m2",
        "user_agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{_CHROME} Safari/537.36"
        ),
        "platform": "MacIntel",
        "viewport": {"width": 1512, "height": 916},
        "screen": {"width": 1512, "height": 982},
        "device_scale_factor": 2,
        "timezone_id": "America/New_York",
        "locale": "en-US",
        "hardware_concurrency": 10,
        "device_memory": 16,
        "webgl_vendor": "Google Inc. (Apple)",
        "webgl_renderer": "ANGLE (Apple, ANGLE Metal Renderer: Apple M2, Unspecified Version)",
    },
    {
        "label": "macbook-air-m1",
        "user_agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{_CHROME} Safari/537.36"
        ),
        "platform": "MacIntel",
        "viewport": {"width": 1440, "height": 812},
        "screen": {"width": 1440, "height": 900},
        "device_scale_factor": 2,
        "timezone_id": "America/Los_Angeles",
        "locale": "en-US",
        "hardware_concurrency": 8,
        "device_memory": 8,
        "webgl_vendor": "Google Inc. (Apple)",
        "webgl_renderer": "ANGLE (Apple, ANGLE Metal Renderer: Apple M1, Unspecified Version)",
    },
    {
        "label": "windows-11-nvidia",
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{_CHROME} Safari/537.36"
        ),
        "platform": "Win32",
        "viewport": {"width": 1920, "height": 969},
        "screen": {"width": 1920, "height": 1080},
        "device_scale_factor": 1,
        "timezone_id": "America/Chicago",
        "locale": "en-US",
        "hardware_concurrency": 16,
        "device_memory": 16,
        "webgl_vendor": "Google Inc. (NVIDIA)",
        "webgl_renderer": (
            "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"
        ),
    },
    {
        "label": "windows-10-intel",
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{_CHROME} Safari/537.36"
        ),
        "platform": "Win32",
        "viewport": {"width": 1536, "height": 731},
        "screen": {"width": 1536, "height": 864},
        "device_scale_factor": 1.25,
        "timezone_id": "America/New_York",
        "locale": "en-US",
        "hardware_concurrency": 8,
        "device_memory": 8,
        "webgl_vendor": "Google Inc. (Intel)",
        "webgl_renderer": (
            "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"
        ),
    },
]


# Structured version fields for every entry above, derived from ONE version
# string so nothing downstream has to reparse a user agent to find the Chrome
# major. Applied by a helper rather than typed out four times: a profile you add
# to the pool needs only its device fields and picks these up for free.
def _stamp_pool(pool: List[Dict], version: str = _CHROME) -> None:
    """Give every entry in a profile pool its structured version fields.

    Call it again after appending your own profiles at runtime and they get the
    same treatment; a pool entry that already carries them is simply restamped.
    """
    for entry in pool:
        _apply_version(entry, version)


_stamp_pool(_PROFILES)

# Rough country -> timezone map for proxy/geo coherence. Extend as needed.
_TZ_BY_CC: Dict[str, str] = {
    "US": "America/New_York", "CA": "America/Toronto", "GB": "Europe/London",
    "DE": "Europe/Berlin", "FR": "Europe/Paris", "NL": "Europe/Amsterdam",
    "ES": "Europe/Madrid", "IT": "Europe/Rome", "JP": "Asia/Tokyo",
    "AU": "Australia/Sydney", "SG": "Asia/Singapore",
}

# Country -> locale, paired one-for-one with _TZ_BY_CC above. align_to_proxy()
# reads BOTH from the same country code and derives languages from the locale it
# lands on, so the timezone, the locale, navigator.languages and the
# Accept-Language header can never end up telling different stories.
_LOCALE_BY_CC: Dict[str, str] = {
    "US": "en-US", "CA": "en-CA", "GB": "en-GB",
    "DE": "de-DE", "FR": "fr-FR", "NL": "nl-NL",
    "ES": "es-ES", "IT": "it-IT", "JP": "ja-JP",
    "AU": "en-AU", "SG": "en-SG",
}

# Proxy dict keys Playwright uses to reach the proxy itself. Everything else a
# proxy carries is treated as a hint about the exit and recorded on the profile
# (see align_to_proxy); credentials never are.
_PROXY_TRANSPORT_KEYS = ("server", "username", "password", "bypass")

# JS injected before page scripts run. __CFG__ is replaced with a JSON blob;
# every patch is wrapped so one failure never aborts the rest.
_INIT_TEMPLATE = r"""
(() => {
  const cfg = __CFG__;
  // --- toString masking -------------------------------------------------
  // Every function patched below would otherwise betray itself: patchedFn
  // .toString() returns the patch SOURCE instead of
  // "function x() { [native code] }", which is exactly what JS telemetry
  // (Akamai sensor, Queue-it checks) looks for. All patched functions are
  // registered in _maskedNames, and Function.prototype.toString returns a
  // native-looking string for them. The override masks itself too, so its
  // own toString() also reads as native. Names live in a WeakMap — never as
  // own properties on the functions, which would be enumerable tells.
  const _maskedNames = new WeakMap();
  const _nativeToString = Function.prototype.toString;
  const _maskFn = (fn, name) => {
    try { _maskedNames.set(fn, name); } catch (e) {}
    return fn;
  };
  const _toStringOverride = function () {
    try {
      const n = _maskedNames.get(this);
      if (n !== undefined) return `function ${n}() { [native code] }`;
    } catch (e) {}
    return _nativeToString.call(this);
  };
  _maskedNames.set(_toStringOverride, 'toString');
  Function.prototype.toString = _toStringOverride;
  // Real Chrome exposes these as enumerable getters (Navigator.prototype
  // .webdriver, Notification.permission) — enumerable:false would itself be
  // a mismatch, so def() matches the real descriptor shape.
  const def = (obj, prop, val) => {
    try { Object.defineProperty(obj, prop, { get: () => val, enumerable: true, configurable: true }); }
    catch (e) {}
  };
  // Automation tell — real Chrome exposes this as false, headless-automation as true.
  def(Navigator.prototype, 'webdriver', false);
  // Automation tell, confirmed live against raw Playwright with NO evasion at
  // all: Notification.permission reports 'denied' on every Playwright-launched
  // Chromium context, HEADLESS AND HEADED ALIKE, because Chromium auto-denies
  // the notification prompt under --enable-automation (no UI to show it to).
  // A real visitor's fresh profile reads 'default' (never asked), not
  // 'denied' (explicitly refused) — those are different facts about the
  // user, and 'denied' is the one that reads as a bot. The permissions.query
  // patch just below only made navigator.permissions.query('notifications')
  // AGREE with this value; it never corrected the value itself, so both
  // surfaces stayed 'denied' until now.
  def(Notification, 'permission', 'default');
  def(navigator, 'platform', cfg.platform);
  def(navigator, 'hardwareConcurrency', cfg.hardwareConcurrency);
  def(navigator, 'deviceMemory', cfg.deviceMemory);
  def(navigator, 'languages', cfg.languages);
  try {
    if (!window.chrome) { window.chrome = {}; }
    if (!window.chrome.runtime) { window.chrome.runtime = {}; }
  } catch (e) {}
  // Patched on Permissions.prototype (where the real one lives), not on the
  // instance — an own-property 'query' on navigator.permissions is itself a
  // tell. toString-masked like every other patch below.
  try {
    const permsProto = (navigator.permissions && Object.getPrototypeOf(navigator.permissions))
      || (window.Permissions && Permissions.prototype);
    if (permsProto && permsProto.query) {
      const origQuery = permsProto.query;
      permsProto.query = _maskFn(function (p) {
        return (p && p.name === 'notifications')
          ? Promise.resolve({ state: Notification.permission })
          : origQuery.call(this, p);
      }, 'query');
    }
  } catch (e) {}
  try {
    const patch = (proto) => {
      const gp = proto.getParameter;
      // Masked: an unmasked override's toString() leaks the patch source.
      proto.getParameter = _maskFn(function (p) {
        if (p === 37445) return cfg.webglVendor;     // UNMASKED_VENDOR_WEBGL
        if (p === 37446) return cfg.webglRenderer;    // UNMASKED_RENDERER_WEBGL
        return gp.apply(this, [p]);
      }, 'getParameter');
    };
    if (window.WebGLRenderingContext) patch(WebGLRenderingContext.prototype);
    if (window.WebGL2RenderingContext) patch(WebGL2RenderingContext.prototype);
  } catch (e) {}

  // Canvas / WebGL pixel noise. The point is NOT to look "clean" — real GPUs
  // produce near-unique canvas hashes, so a scrubbed canvas is itself a tell.
  // Instead we perturb a sparse, deterministic set of pixels by ±1 so the image
  // is visually identical but the fingerprint hash differs from the raw machine
  // and, via cfg.noiseSeed, is stable within a session (a canvas that changes
  // between two reads in one session is a louder signal than any fixed value).
  try {
    const seed = (cfg.noiseSeed >>> 0);
    // Cheap integer hash of a pixel index; mixes in the per-session seed.
    const h32 = (x) => {
      x = (x ^ seed) >>> 0;
      x = Math.imul(x ^ (x >>> 16), 0x45d9f3b) >>> 0;
      x = Math.imul(x ^ (x >>> 16), 0x45d9f3b) >>> 0;
      return (x ^ (x >>> 16)) >>> 0;
    };
    // Nudge ~0.8% of pixels by ±1 on one RGB channel (alpha untouched),
    // keyed only by pixel index so every read of the same buffer matches.
    const perturb = (data) => {
      for (let i = 0; i < data.length; i += 4) {
        const g = h32(i);
        if ((g & 0xff) < 2) {
          const c = i + (g % 3);
          const v = data[c] + ((g & 0x100) ? 1 : -1);
          data[c] = v < 0 ? 0 : (v > 255 ? 255 : v);
        }
      }
    };
    const origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
    // Encode via a throwaway copy so the source canvas is never mutated and
    // noise never compounds across repeated toDataURL/toBlob calls.
    const noisyEncode = (canvas, fn, args) => {
      try {
        const w = canvas.width, h = canvas.height;
        if (!w || !h) return fn.apply(canvas, args);
        const tmp = document.createElement('canvas');
        tmp.width = w; tmp.height = h;
        const tctx = tmp.getContext('2d');
        tctx.drawImage(canvas, 0, 0);
        const img = origGetImageData.call(tctx, 0, 0, w, h);
        perturb(img.data);
        tctx.putImageData(img, 0, 0);
        return fn.apply(tmp, args);
      } catch (e) { return fn.apply(canvas, args); }
    };
    CanvasRenderingContext2D.prototype.getImageData = _maskFn(function (sx, sy, sw, sh) {
      const img = origGetImageData.apply(this, arguments);
      try { perturb(img.data); } catch (e) {}
      return img;
    }, 'getImageData');
    const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
    HTMLCanvasElement.prototype.toDataURL = _maskFn(function () {
      return noisyEncode(this, origToDataURL, arguments);
    }, 'toDataURL');
    const origToBlob = HTMLCanvasElement.prototype.toBlob;
    if (origToBlob) {
      HTMLCanvasElement.prototype.toBlob = _maskFn(function () {
        return noisyEncode(this, origToBlob, arguments);
      }, 'toBlob');
    }
    // WebGL readback: only byte buffers (the UNSIGNED_BYTE fingerprint path);
    // leave float/other typed reads alone so real rendering math is untouched.
    const patchReadPixels = (proto) => {
      const rp = proto.readPixels;
      proto.readPixels = _maskFn(function (x, y, w, hh, fmt, type, pixels) {
        rp.apply(this, arguments);
        try {
          if (pixels instanceof Uint8Array || pixels instanceof Uint8ClampedArray) {
            perturb(pixels);
          }
        } catch (e) {}
      }, 'readPixels');
    };
    if (window.WebGLRenderingContext) patchReadPixels(WebGLRenderingContext.prototype);
    if (window.WebGL2RenderingContext) patchReadPixels(WebGL2RenderingContext.prototype);
  } catch (e) {}
})();
"""


class FingerprintManager:
    """Generates a coherent device fingerprint. Seed → stable identity."""

    def __init__(self, seed: Optional[int] = None):
        self.seed = seed
        self.rng = random.Random(seed)
        self._profile: Optional[Dict] = None
        # account_id -> profile, so a reintroduced account keeps its identity.
        self._sticky: Dict[str, Dict[str, Any]] = {}
        # Filled by set_runtime_browser() with the version of the binary that
        # actually launches. Empty means "nobody told us", and the pool's pinned
        # _CHROME stands — the manager is fully usable with it never called.
        self.runtime_chrome_full: str = ""
        self.runtime_chrome_major: str = ""

    # -- version binding ---------------------------------------------------- #
    def set_runtime_browser(self, full_version: Optional[str]) -> None:
        """Bind every profile to the browser build that will really serve pages.

        The factory probes the engine it is about to launch (Google Chrome via
        the channel, or the bundled Chromium after a fallback) and hands the full
        version here. Every cached sticky profile is rewritten in place —
        ``chrome_full``, ``chrome_major``, ``user_agent``, ``sec_ch_ua`` and
        ``brands`` — and profiles created afterwards are born with it, so an
        account keeps its machine and only changes Chrome version. The OS half of
        each UA is untouched: a MacIntel profile stays MacIntel.

        This is what makes forging ``Sec-CH-UA`` safe. Chromium emits client
        hints from its real build; a header that agrees with that build is
        invisible, one that claims a different version is a direct mismatch.

        A falsy/blank version is a no-op on purpose — a failed version probe
        should leave the profiles alone, not blank out their identity.
        """
        version = str(full_version or "").strip()
        if not version:
            return
        self.runtime_chrome_full = version
        self.runtime_chrome_major = version.split(".")[0] or _CHROME_MAJOR
        for fp in self._sticky.values():
            _apply_version(fp, version)
        # The default profile is normally the sticky "default" entry, but a
        # subclass may have set it another way; never leave it on a stale build.
        if self._profile is not None and not any(
                self._profile is fp for fp in self._sticky.values()):
            _apply_version(self._profile, version)

    def _new_profile(self) -> Dict[str, Any]:
        """Draw and jitter one coherent archetype from the pool.

        Every random draw that contributes to a profile comes from ``self.rng`` and
        never from module-level ``random`` — that is what makes
        ``FingerprintManager(seed=N)`` reproduce a profile exactly, which
        ``tasks/runner.py`` depends on when it builds the manager from a task
        profile's ``fingerprint_seed``.
        """
        base: Dict[str, Any] = {k: _copy_value(v)
                                for k, v in self.rng.choice(_PROFILES).items()}
        # Small deterministic height jitter so two users of the same profile
        # aren't pixel-identical, without breaking the width/screen ratio.
        jitter = self.rng.choice([0, -8, 8, -16, 16, -24])
        base["viewport"]["height"] = max(600, base["viewport"]["height"] + jitter)
        # Structured version fields: the runtime build when the factory has
        # already reported one, else whatever the pool entry was stamped with.
        # Re-applied (not assumed) so a hand-written profile added to _PROFILES
        # without them still comes out complete.
        _apply_version(base, self.runtime_chrome_full or base.get("chrome_full") or _CHROME)
        # Derived from the locale, never typed separately, so the two agree.
        base["languages"] = _languages_for(base["locale"])
        # The identity this profile was born with. align_to_proxy() rewrites
        # locale/languages/timezone_id on the *sticky* dict, so without a record of
        # the original, an account that once exited through Canada would keep en-CA
        # for the rest of its life. These three let that alignment be undone.
        base["base_locale"] = base["locale"]
        base["base_timezone_id"] = base["timezone_id"]
        base["base_languages"] = list(base["languages"])
        return base

    def profile(self) -> Dict:
        """Choose (once) and cache a coherent device profile for this instance."""
        if self._profile is None:
            self._profile = self.profile_for("default")
        return self._profile

    def profile_for(self, account_id: str = "default") -> Dict:
        """The sticky profile for one account, created and cached on first use.

        Returns the *cached* dict, not a copy: the caller aligns it to its proxy
        exit in place, and an account that comes back later has to come back as the
        same machine — a fresh fingerprint on a sticky exit IP is itself a tell.

        A cached profile whose Chrome version predates a ``set_runtime_browser``
        call is re-stamped here, so a profile created before the engine was known
        never launches claiming the wrong build.
        """
        fp = self._sticky.get(account_id)
        if fp is None:
            fp = self._new_profile()
            self._sticky[account_id] = fp
        elif (self.runtime_chrome_full
              and fp.get("chrome_full") != self.runtime_chrome_full):
            _apply_version(fp, self.runtime_chrome_full)
        return fp

    def align_to_proxy(self, fp: Dict, proxy: Optional[Dict]) -> None:
        """Move a profile's locale/languages/timezone to agree with its exit country.

        Mutates ``fp`` in place — it is the account's sticky profile. Reversible:
        each profile records the locale, languages and timezone it was born with,
        so an exit with no rule of its own restores that base instead of leaving
        the previous exit's ``en-CA`` behind for good. Alignment is therefore
        idempotent, and CA -> US actually moves back rather than sticking.

        Locale comes from ``_LOCALE_BY_CC`` and languages from ``_languages_for``
        on that same locale, so the two can never disagree; the timezone comes
        from the proxy's own hint when it has one, else ``_TZ_BY_CC``.

        A falsy ``proxy`` (no proxy at all) leaves the profile untouched; a proxy
        that simply carries no geo/timezone hint restores the base.

        Anything else the proxy carries — latitude/longitude, exit IP, an ASN,
        whatever a richer pool attaches — is recorded verbatim under
        ``fp["proxy_hints"]`` instead of being dropped, so downstream code can
        consume it. Nothing in this module reads it; credentials and the server
        address are deliberately not copied there.
        """
        if not proxy:
            return
        cc = (proxy.get("geo") or "").upper()
        tz = proxy.get("timezone") or _TZ_BY_CC.get(cc)
        if tz:
            fp["timezone_id"] = tz
        elif "base_timezone_id" in fp:
            fp["timezone_id"] = fp["base_timezone_id"]
        locale = _LOCALE_BY_CC.get(cc)
        if locale:
            fp["locale"] = locale
            fp["languages"] = _languages_for(locale)
        else:
            if "base_locale" in fp:
                fp["locale"] = fp["base_locale"]
            fp["languages"] = list(
                fp.get("base_languages") or _languages_for(fp.get("locale") or "en-US"))
        # Replaced wholesale, never merged: a previous exit's coordinates
        # lingering on a profile is the same bug as a lingering en-CA.
        fp["proxy_hints"] = {k: v for k, v in proxy.items()
                             if k not in _PROXY_TRANSPORT_KEYS}

    def context_options(self, *, override: Optional[Dict] = None) -> Dict:
        """Valid Playwright context kwargs for this fingerprint.

        Precedence is simple and total: **caller override beats profile-derived**.
        Every key in ``override`` whose value is not ``None`` is merged into the
        result, not just a fixed allowlist — so a context option this module has
        never heard of (``geolocation``, ``permissions``, ``proxy``,
        ``color_scheme``, anything Playwright grows next) can be supplied from
        your own code with no edit here. ``None`` values are skipped, which is
        what lets the factory pass ``viewport=None`` / ``user_agent=None`` for
        "not configured" without wiping the generated ones.

        Keys starting with ``_`` are RESERVED for this module: they are consumed
        here and never forwarded to Playwright, which has no such kwargs.
        ``_profile`` is the one in use today — a truthy ``override["_profile"]``
        supplies the profile to build from, so the factory can pass the launching
        account's sticky, proxy-aligned profile and get kwargs describing *that*
        machine. Note the profile is resolved at most once, and only after that
        check — picking one up front and then discarding it would burn a profile
        and advance the RNG on every call, which is exactly what breaks seed
        reproducibility.
        """
        p = override.get("_profile") if isinstance(override, dict) else None
        if not p:
            p = self.profile()
        languages = list(p.get("languages") or _languages_for(p["locale"]))
        opts: Dict = {
            "user_agent": p["user_agent"],
            "viewport": dict(p["viewport"]),
            "screen": dict(p["screen"]),
            "device_scale_factor": p["device_scale_factor"],
            "locale": p["locale"],
            "timezone_id": p["timezone_id"],
            "color_scheme": "light",
            "is_mobile": False,
            "has_touch": False,
            "extra_http_headers": {
                "Accept-Language": _accept_language(languages),
                # Read off the profile's structured field — never rebuilt by
                # splitting the UA apart, which is how a header and a UA drift
                # into disagreeing. set_runtime_browser() keeps this pinned to
                # the build that is really running.
                "Sec-CH-UA": p.get("sec_ch_ua") or _sec_ch_ua(
                    p.get("chrome_major") or _CHROME_MAJOR),
            },
        }
        if override:
            for k, v in override.items():
                if k.startswith("_"):
                    continue
                if v is not None:
                    opts[k] = v
        return opts

    def init_script(self, profile: Optional[Dict] = None) -> str:
        """JS that aligns navigator/WebGL with the generated context options.

        Pass the profile the context options were built from (the account's
        sticky, proxy-aligned one); ``None`` uses this instance's default profile.
        """
        p = profile if profile is not None else self.profile()
        # Stable within a (seed, profile) so a sticky proxy exit keeps one canvas
        # identity across launches, but distinct between profiles.
        noise_seed = zlib.crc32(f"{self.seed}:{p['label']}:canvas".encode()) & 0xFFFFFFFF
        # Taken from THIS profile rather than a fixed string: align_to_proxy()
        # can move it to en-CA/ja-JP and the context-level `locale` and the
        # Accept-Language header already follow, so navigator.languages has to
        # follow too or the page and the header disagree with each other.
        cfg = {
            "platform": p["platform"],
            "hardwareConcurrency": p["hardware_concurrency"],
            "deviceMemory": p["device_memory"],
            "webglVendor": p["webgl_vendor"],
            "webglRenderer": p["webgl_renderer"],
            "languages": list(p.get("languages") or _languages_for(p["locale"])),
            "noiseSeed": noise_seed,
        }
        # json.dumps, never repr(): any value holding a double quote (a
        # platform-brand string like '"Windows"', or this profile's own
        # sec_ch_ua, say) comes out of repr-based templating as ""Windows"" — a
        # JS syntax error that silently kills the whole injected script, patches
        # and canvas noise together.
        return _INIT_TEMPLATE.replace("__CFG__", json.dumps(cfg))

    def describe(self, target: Optional[Any] = None) -> str:
        """One-line label for logs. Takes an account id, a profile, or nothing.

        Both call styles are live, so both are supported:

        * ``describe("acct-7")`` — a **string** is an account id and you get
          *that* account's sticky profile. ``browser_factory`` calls it this way
          and ``tasks/runner.py`` logs the result per task as
          ``last_fingerprint``; without it every task reported the default
          account's machine no matter which account launched.
        * ``describe(profile)`` — a **dict** is used as the profile directly, for
          a caller that already holds one and wants no lookup (and no sticky
          entry created) as a side effect.
        * ``describe()`` — the default account, which is what the pre-per-account
          factory called and must keep working.

        Every field is read defensively so a hand-made profile dict still
        describes rather than raising in a log line.
        """
        if isinstance(target, dict):
            p: Dict[str, Any] = target
        elif target is None:
            p = self.profile()
        else:
            p = self.profile_for(str(target))
        vp = p.get("viewport") or {}
        langs = list(p.get("languages") or _languages_for(p.get("locale") or "en-US"))
        return (
            f"{p.get('label', '?')} @ {vp.get('width', '?')}x{vp.get('height', '?')}"
            f" · Chrome {p.get('chrome_major') or _CHROME_MAJOR}"
            f" · {p.get('hardware_concurrency', '?')}c"
            f"/{p.get('device_memory', '?')}GB"
            f" · {_renderer_head(p.get('webgl_renderer', ''))}"
            f" · {p.get('locale', '?')} [{','.join(langs)}]"
            f" · {p.get('timezone_id', '?')}"
        )

    # Backwards-compatible alias — older callers used generate() for context opts.
    def generate(self, *, override: Optional[Dict] = None) -> Dict:
        return self.context_options(override=override)

    # Optional methods the factory folds in when they exist (INTERFACE.md §2) —
    # launch_args() / ignore_default_args() — are deliberately NOT defined here.
    # Absent means "no opinion", which is not the same as an empty list, and
    # adding either is a pure addition to this class that needs no edit anywhere
    # else. The same is true of any new optional method the factory learns to
    # consult.
