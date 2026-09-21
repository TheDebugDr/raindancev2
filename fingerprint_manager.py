"""Coherent browser fingerprints for anti-bot evasion.

Every signal (UA, Sec-CH-UA, userAgentData, platform, cores, memory,
screen, WebGL vendor/renderer, locale, languages, timezone, geolocation)
derives from ONE canonical profile dict, and the Chrome version inside
that profile is bound to the browser binary that will actually launch
(FingerprintManager.set_runtime_browser, called by BrowserFactory after
it probes the real engine). Incoherent combos — en-GB locale with
['en-US','en'] languages, a "Chrome/131" UA on a 151 binary, a Denver
timezone on a Tokyo exit — are exactly what Akamai and Queue-it's
2025 cross-validation layer flag.
"""
from __future__ import annotations

import json
import random
from typing import Any, Dict, List, Optional

# Fallbacks only — a live probe (proxy health check / engine detect)
# overrides these. Chrome 151 stable Jul 28 2026, 152 Aug 25 2026 [10].
_FALLBACK_CHROME_MAJORS: List[int] = [148, 149, 150, 151, 152]

_TZ_BY_CC: Dict[str, str] = {
    "US": "America/New_York", "CA": "America/Toronto", "GB": "Europe/London",
    "DE": "Europe/Berlin", "FR": "Europe/Paris", "NL": "Europe/Amsterdam",
    "ES": "Europe/Madrid", "IT": "Europe/Rome", "JP": "Asia/Tokyo",
    "AU": "Australia/Sydney", "SG": "Asia/Singapore",
}
_LOCALE_BY_CC: Dict[str, str] = {
    "US": "en-US", "CA": "en-CA", "GB": "en-GB", "AU": "en-AU",
    "DE": "de-DE", "FR": "fr-FR", "NL": "nl-NL", "ES": "es-ES",
    "IT": "it-IT", "JP": "ja-JP", "SG": "en-SG",
}


def _languages_for(locale: str) -> List[str]:
    """navigator.languages derived from the SAME locale Playwright is told."""
    base = locale.split("-")[0].lower()
    return [locale, base] if base != locale.lower() else [locale]


def _ua_for(kind: str, chrome: str) -> str:
    if kind == "win":
        return ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                f"(KHTML, like Gecko) Chrome/{chrome} Safari/537.36")
    return ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{chrome} Safari/537.36")


def _sec_ch_ua(major: str) -> str:
    # Structured input, no UA-string reparsing. Shape matches real Chrome
    # (Chromium / Google Chrome / GREASE brand) [11].
    return f'"Chromium";v="{major}", "Google Chrome";v="{major}", "Not_A Brand";v="24"'


def _profile(kind: str, chrome_full: str) -> Dict[str, Any]:
    major = chrome_full.split(".")[0]
    chrome_zero = f"{major}.0.0.0"
    fp: Dict[str, Any] = {
        "ua_kind": kind,
        "chrome_major": major,            # FIX #4/#5: structured versions
        "chrome_full": chrome_full,
        "user_agent": _ua_for(kind, chrome_zero),
        "sec_ch_ua": _sec_ch_ua(major),
        "brands": [
            {"brand": "Chromium", "version": major},
            {"brand": "Google Chrome", "version": major},
            {"brand": "Not_A Brand", "version": "24"},
        ],
        "locale": "en-US",
        "languages": _languages_for("en-US"),   # FIX #3: derived, never hardcoded
        "timezone_id": "America/New_York",
    }
    if kind == "win":
        fp.update({
            "platform": "Win32",
            "platform_name": "Windows",
            "sec_ch_ua_platform": '"Windows"',
            "hardware_concurrency": random.choice([4, 8, 12, 16]),
            "device_memory": random.choice([4, 8, 16]),
            "screen": {"width": 1920, "height": 1080},
            "webgl_vendor": "Google Inc. (NVIDIA)",
            "webgl_renderer": random.choice([
                "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 SUPER "
                "(0x00001F02) Direct3D11 vs_5_0 ps_5_0, D3D11)",
                "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 "
                "(0x00002503) Direct3D11 vs_5_0 ps_5_0, D3D11)",
                "ANGLE (Intel, Intel(R) UHD Graphics 630 "
                "(0x00003E92) Direct3D11 vs_5_0 ps_5_0, D3D11)",
            ]),
        })
    else:  # mac
        fp.update({
            "platform": "MacIntel",
            "platform_name": "macOS",
            "sec_ch_ua_platform": '"macOS"',
            "hardware_concurrency": random.choice([8, 10, 12]),
            "device_memory": random.choice([8, 16]),
            "screen": {"width": 1728, "height": 1117},
            "webgl_vendor": "Google Inc. (Apple)",
            "webgl_renderer": random.choice([
                "ANGLE (Apple, ANGLE Metal Renderer: Apple M1, Unspecified Version)",
                "ANGLE (Apple, ANGLE Metal Renderer: Apple M2, Unspecified Version)",
                "ANGLE (Apple, Radeon Pro 5500M OpenGL Engine, OpenGL 4.1)",
            ]),
        })
    return fp


class FingerprintManager:
    """Coherent, per-account-sticky fingerprints bound to the real engine."""

    def __init__(self, seed: Optional[int] = None, profile_kind: Optional[str] = None):
        self.rng = random.Random(seed)
        self._forced_kind = profile_kind
        self._sticky: Dict[str, Dict[str, Any]] = {}
        self._runtime_browser: Optional[str] = None  # e.g. "151.0.7922.108"

    # ------------------------------------------------------------------ #
    def set_runtime_browser(self, full_version: str) -> None:
        """FIX #4: bind every profile to the browser that will launch."""
        self._runtime_browser = full_version
        for fp in self._sticky.values():
            self._apply_version(fp)

    def _apply_version(self, fp: Dict[str, Any]) -> None:
        if not self._runtime_browser:
            return
        major = self._runtime_browser.split(".")[0]
        fp["chrome_full"] = self._runtime_browser
        fp["chrome_major"] = major
        fp["user_agent"] = _ua_for(fp["ua_kind"], f"{major}.0.0.0")
        fp["sec_ch_ua"] = _sec_ch_ua(major)          # FIX #5: derived from structure
        fp["brands"] = [
            {"brand": "Chromium", "version": major},
            {"brand": "Google Chrome", "version": major},
            {"brand": "Not_A Brand", "version": "24"},
        ]

    def _new_profile(self) -> Dict[str, Any]:
        kind = self._forced_kind or self.rng.choice(["win", "mac"])
        full = self._runtime_browser or \
            f"{self.rng.choice(_FALLBACK_CHROME_MAJORS)}.0.0.0"
        return _profile(kind, full)

    def profile_for(self, account_id: str = "default") -> Dict[str, Any]:
        fp = self._sticky.get(account_id)
        if fp is None:
            fp = self._new_profile()
            self._sticky[account_id] = fp
        elif self._runtime_browser and fp["chrome_full"] != self._runtime_browser:
            self._apply_version(fp)
        return fp

    def align_to_proxy(self, fp: Dict[str, Any], proxy: Optional[Dict]) -> None:
        """Timezone + locale + languages aligned to the proxy exit.

        proxy["timezone"]/["geo"] now come from the live health check
        (Cloudflare trace), not a guess. Queue-it's cross-validation
        flags tz-vs-IP-geo mismatches [2].
        """
        if not proxy:
            return
        cc = (proxy.get("geo") or "").upper()
        tz = proxy.get("timezone") or _TZ_BY_CC.get(cc)
        if tz:
            fp["timezone_id"] = tz
        locale = _LOCALE_BY_CC.get(cc)
        if locale:
            fp["locale"] = locale
            fp["languages"] = _languages_for(locale)   # stays consistent
        if proxy.get("latitude") and proxy.get("longitude"):
            fp["geolocation"] = {
                "latitude": proxy["latitude"], "longitude": proxy["longitude"]}

    # -- packaged contract ---------------------------------------------- #
    def generate(self) -> Dict:
        """Legacy single-shot surface."""
        return self.context_options()

    def context_options(self, override: Optional[Dict] = None) -> Dict:
        ov = dict(override or {})
        fp = ov.pop("_profile", None) or self._new_profile()
        opts: Dict[str, Any] = {
            "user_agent": fp["user_agent"],
            "viewport": dict(fp["screen"]),
            "screen": dict(fp["screen"]),
            "locale": fp["locale"],
            "timezone_id": fp["timezone_id"],
            "color_scheme": "light",
            "device_scale_factor": 1,
            "extra_http_headers": {
                "Sec-CH-UA": fp["sec_ch_ua"],
                "Sec-CH-UA-Mobile": "?0",
                "Sec-CH-UA-Platform": fp["sec_ch_ua_platform"],
            },
        }
        if fp.get("geolocation"):
            opts["geolocation"] = dict(fp["geolocation"])
            opts["permissions"] = ["geolocation"]
        # Explicit caller values are authoritative (documented precedence):
        # the factory passes the caller's configured viewport, so a settings
        #-level 1280x900 wins over the profile's screen — while "screen"
        # stays the believable monitor size.
        for key in ("viewport", "user_agent"):
            if ov.get(key):
                opts[key] = ov[key]
        return opts

    def describe(self, fp: Optional[Dict[str, Any]] = None) -> str:
        """FIX #2: describes the profile actually used, when given."""
        fp = fp or self.profile_for("default")
        return (f"{fp['ua_kind']} chrome{fp['chrome_major']} · "
                f"{fp['hardware_concurrency']}c/{fp['device_memory']}GB · "
                f"{fp['webgl_renderer'].split(',')[0]} · "
                f"{fp['locale']}/{','.join(fp['languages'])} · {fp['timezone_id']} · "
                f"webgl-injected")

    # -- script injection ----------------------------------------------- #
    def init_script(self, profile: Optional[Dict[str, Any]] = None) -> str:
        fp = profile or self.profile_for()
        payload = json.dumps({
            "platform": fp["platform"],
            "hardware_concurrency": fp["hardware_concurrency"],
            "device_memory": fp["device_memory"],
            "languages": fp["languages"],          # FIX #3: from the profile
            "webgl_vendor": fp["webgl_vendor"],
            "webgl_renderer": fp["webgl_renderer"],
            "ua_platform": fp["platform_name"],
            "brands": fp["brands"],
        })
        return """(() => {
  const fp = __PAYLOAD__;
  const def = (obj, prop, value) => {
    try { Object.defineProperty(obj, prop, {get: () => value, configurable: true}); } catch (e) {}
  };
  def(navigator, 'webdriver', undefined);
  def(navigator, 'platform', fp.platform);
  def(navigator, 'hardwareConcurrency', fp.hardware_concurrency);
  def(navigator, 'deviceMemory', fp.device_memory);
  def(navigator, 'languages', Object.freeze(fp.languages.slice()));
  // Client hints on the JS side must match the headers (Chromium-derived).
  if (navigator.userAgentData) {
    try { navigator.userAgentData.__defineGetter__('brands', () => fp.brands); } catch (e) {}
    try { navigator.userAgentData.__defineGetter__('platform', () => fp.ua_platform); } catch (e) {}
  }
  def(navigator, 'plugins', [
    {name:'Chrome PDF Viewer', filename:'internal-pdf-viewer', description:'Portable Document Format files', length:1, 0:{type:'application/pdf', suffixes:'pdf', description:'Portable Document Format', enabledPlugin:null}},
    {name:'Chrome PDF Viewer', filename:'internal-pdf-viewer', description:'', length:1, 0:{type:'application/x-google-chrome-pdf', suffixes:'pdf', description:'Portable Document Format', enabledPlugin:null}},
    {name:'Chromium PDF Viewer', filename:'internal-pdf-viewer', description:'', length:1, 0:{type:'application/pdf', suffixes:'pdf', description:'', enabledPlugin:null}},
    {name:'Microsoft Edge PDF Viewer', filename:'internal-pdf-viewer', description:'', length:1, 0:{type:'application/pdf', suffixes:'pdf', description:'', enabledPlugin:null}},
    {name:'WebKit built-in PDF', filename:'internal-pdf-viewer', description:'', length:1, 0:{type:'application/pdf', suffixes:'pdf', description:'', enabledPlugin:null}}
  ]);
  def(navigator, 'mimeTypes', [0,1,2,3,4].map(i => navigator.plugins[i][0]));
  def(navigator, 'maxTouchPoints', 0);
  window.chrome = window.chrome || {};
  if (!window.chrome.runtime) {
    window.chrome.runtime = {connect: () => {}, sendMessage: () => {}, id: undefined,
      PlatformOs: {MAC: 'mac', WIN: 'win', ANDROID: 'android', CROS: 'cros', LINUX: 'linux', OPENBSD: 'openbsd'}};
  }
  window.chrome.app = window.chrome.app || {isInstalled: false,
    InstallState: {INSTALLED: 'installed', DISABLED: 'disabled', NOT_INSTALLED: 'not_installed'},
    getDetails: () => null, getIsInstalled: () => false};
  if (typeof Notification !== 'undefined') { def(Notification, 'permission', 'prompt'); }
  if (navigator.permissions && navigator.permissions.query) {
    const orig = navigator.permissions.query.bind(navigator.permissions);
    navigator.permissions.query = (p) => (p && p.name === 'notifications')
      ? Promise.resolve({state: 'prompt'}) : orig(p);
  }
  const patchGetParameter = (proto) => {
    const orig = proto.getParameter;
    proto.getParameter = function (p) {
      try {
        const ext = this.getExtension('WEBGL_debug_renderer_info');
        if (ext) {
          if (p === ext.UNMASKED_VENDOR_WEBGL) return fp.webgl_vendor;
          if (p === ext.UNMASKED_RENDERER_WEBGL) return fp.webgl_renderer;
        }
      } catch (e) {}
      return orig.call(this, p);
    };
  };
  patchGetParameter(WebGLRenderingContext.prototype);
  if (typeof WebGL2RenderingContext !== 'undefined') patchGetParameter(WebGL2RenderingContext.prototype);
})();""".replace("__PAYLOAD__", payload)
