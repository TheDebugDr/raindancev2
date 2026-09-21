# Evasion extension contract

This is the surface you build against. It is deliberately **wide and open-ended**:
a small required core, then a large catalogue of *optional* extension points the
launch pipeline calls **only if they exist**. Nothing here has been stubbed or
removed — every seam is live wiring that no-ops until you fill it, so the app
runs identically today and lights up the moment you add code. The surface is
designed to grow: adding a new hook never breaks anything that doesn't implement
it.

Three independent layers, smallest to largest commitment:

1. **Lifecycle hooks** — attach code at named points in every launch. No files
   edited; add a function or a plugin object.
2. **FingerprintManager optional methods** — the factory folds in extra launch
   args, headers, cookies, etc. if your fingerprint manager exposes them.
3. **Wholesale module replacement** — rewrite `fingerprint_manager.py` and/or
   `browser_factory.py` entirely, keeping the core signatures in §3.

---

## 1. Lifecycle hooks  (the main extension surface)

Every launch fires these in order. All optional; unimplemented = no-op. Attach
them three ways, all discovered automatically, all additive (many handlers per
hook run in order — global plugins, then factory-scoped, then `custom.py`):

```python
# a) raindance/evasion/custom.py — module-level functions (copy the .example)
def on_page_created(page, info): ...

# b) a plugin object, registered globally (also works as a class decorator)
from raindance.evasion.browser_factory import register
@register
class MyEvasion:
    def configure_context(self, opts, info): ...
    def on_context_created(self, context, info): ...

# c) one factory only
factory.register_plugin(MyEvasion())
```

### The hooks (fire in this order)

| hook | kind | signature | when |
|------|------|-----------|------|
| `configure_context` | transform | `(context_opts: dict, info) -> dict \| None` | before the context is built. Mutate `context_opts` in place or return a replacement. These become Playwright `new_context`/`launch_persistent_context` kwargs. |
| `configure_launch` | transform | `(launch_kwargs: dict, info) -> dict \| None` | before `chromium.launch*`. Same mutate-or-return rule. Holds `headless`, `slow_mo`, `args`, `ignore_default_args`, `proxy`. |
| `on_context_created` | side | `(context, info) -> None` | context exists, AFTER built-in stealth. `context.add_init_script(...)`, `add_cookies(...)`, `route(...)`, etc. |
| `on_page_created` | side | `(page, info) -> None` | the page `create_context` returns exists. It is always a NEW page (opened after the init scripts, then any page a persistent profile restored is closed), so an init script added here still needs a navigation to take effect. Page-level init scripts, event handlers. |
| `apply` | side | `(context, page, info) -> None` | last. Back-compat catch-all; anything not worth a specific hook. |

`info` is a live dict carried across the whole launch — read it, and add your own
keys to pass state between hooks:

```
{account_id, headless, user_data_dir, for_harvester,
 proxy: dict|None, fingerprint: str, stealth: str}
```

**Error handling:** a hook that raises is logged and skipped, and the launch
continues — UNLESS `strict_evasion` is on (the Evasion page's Strict switch), in
which case it aborts the launch like any other evasion failure. Decide
deliberately whether a failure in your hook should stop the run.

**Adding a brand-new hook later** is a three-line change to `browser_factory.py`
(`create_context` calls `_dispatch_side("your_hook", ...)` or
`_dispatch_transform(...)`, and you add the name to `LIFECYCLE_HOOKS`) — then
document it in this table. Existing code is unaffected.

---

## 2. FingerprintManager optional methods

`FingerprintManager` must provide the core in §3. It **may** also expose any of
these — the factory calls each only if present, and folds the result in
additively (deduped over the built-ins). This is where a richer fingerprint
plugs in without touching the factory:

| method | returns | folded into |
|--------|---------|-------------|
| `launch_args()` | `list[str]` | Chromium `args` (deduped over the built-in stealth args) |
| `ignore_default_args()` | `list[str]` | Chromium `ignore_default_args` |

That list is intentionally short *today* — extend it the same way as hooks: have
`create_context` consult a new optional method, keep it guarded by `hasattr`, and
add a row here. Anything a `configure_context` / `configure_launch` hook can do,
you can also drive from the fingerprint manager this way if you'd rather keep it
with the identity.

---

## 3. Core signatures (required for a wholesale rewrite)

Keep these and you can replace either module's internals entirely; every caller
below keeps working untouched.

### `FingerprintManager` (raindance/evasion/fingerprint_manager.py)
Constructed `FingerprintManager(seed: int | None = None)` — `app.py:55`,
`tasks/runner.py:87` (from each task profile's `fingerprint_seed`). That seed is a
contract: the same seed must reproduce the same identity, so in a replacement
**every** random draw that feeds a profile has to come from that seeded RNG
(`self.rng`), never from module-level `random`.

| member | called from | contract |
|--------|-------------|----------|
| `context_options(*, override: dict\|None=None) -> dict` | browser_factory | Playwright context kwargs; non-None `override` values win. `override` is keyword-only. A truthy `override["_profile"]` is the profile to build the kwargs from; it is consumed here and never forwarded to Playwright (no such kwarg). Resolve a profile at most once and only if `_profile` is absent — picking one first and discarding it burns a profile and advances the RNG on every call. **Required.** |
| `describe(account_id: str\|None=None) -> str` | browser_factory | one-line log label. With an account id, describe *that* account's sticky profile; with none, the default one. `runner` logs it per task as `last_fingerprint`. **Required** — must stay callable with no argument. |
| `init_script(profile: dict\|None=None) -> str` | browser_factory | JS for `add_init_script`, built from `profile` when one is passed (pass the same profile the context options came from, so `navigator.languages` and the context `locale` agree), else from the default profile. **Optional** (guarded); return `""` or omit. |
| `profile_for(account_id="default") -> dict` | browser_factory | the account's sticky profile: created on first use, then the **same dict object** every call, so an account that goes away and comes back comes back as the same machine. Return the cached object, not a copy — callers mutate it in place. **Optional.** |
| `align_to_proxy(fp: dict, proxy: dict\|None) -> None` | browser_factory | mutate `fp`'s `locale`/`timezone_id` in place to agree with the proxy exit's country. Return immediately on a falsy `proxy`. Must be reversible — record what the profile was born with and restore it when a country has no rule of its own, or an account that once exited through Canada keeps `en-CA` for good. **Optional.** |

Per-account launch flow, for a rewrite that wants to keep it: `fp =
profile_for(account_id)` → `align_to_proxy(fp, proxy)` → `context_options(
override={"_profile": fp, ...})` + `init_script(fp)` + `describe(account_id)`.
Every one of those is safe to call bare/argument-free, which is exactly what the
pre-per-account factory did, so an older factory keeps working unchanged.

### `BrowserFactory` (raindance/evasion/browser_factory.py)
`BrowserFactory(proxy_manager=None, fingerprint_manager=None, evasion_enabled=False, strict_evasion=False, bus=None, chrome_channel=True)` — `app.py:53`, `tasks/runner.py:89`. `chrome_channel` is appended last, so every existing positional slot is unchanged and both call sites (which pass keywords, `bus=` included) are unaffected.

| member | called from | contract |
|--------|-------------|----------|
| `create_context(playwright, *, account_id="default", headless=False, user_data_dir=None, slow_mo=0, viewport=None, user_agent=None, for_harvester=False, keep_open_hint=False)` | runner:126, engine:149, auto_checkout:389 | returns `(browser, context, page)`; `browser` is `None` for a persistent context. Fires the §1 hooks. |
| `configure(*, evasion_enabled=None, proxy_manager=None, strict_evasion=None, chrome_channel=None)` | evasion/session/orders plugins | hot-update; only non-None args change. |
| `register_plugin(plugin)` | (yours) | factory-scoped hook registration. |
| `evasion_enabled` / `strict_evasion` / `chrome_channel` (attrs) | app.py | bool. `chrome_channel` launches the installed Google Chrome (`channel="chrome"`) instead of Playwright's bundled Chromium, and falls back to Chromium when it is not there. |
| `last_proxy` / `last_fingerprint` (attrs) | runner | last launch's proxy dict / describe() string |
| `last_stealth` (attr) | runner | **must contain `NOT APPLIED`** when evasion was on but couldn't be applied — the log & Evasion page key off that substring. Empty when evasion off. With evasion on it ends in ` · verify <verdict>` from the post-launch check below; a `NOT APPLIED` from either half survives into the combined string. |
| `last_engine` (attr) | `plugins/evasion.py` (status line), `tasks/runner.py` (via the session record) | what actually launched: `chrome channel <version>`, or `chromium` after a fallback. Empty before the first launch — which is exactly how consumers gate on it, so an empty value must mean "no launch yet", never "unknown". |

Three things `create_context` guarantees around the launch itself:

* **Engine + fallback.** With `chrome_channel` on, the launch asks for
  `channel="chrome"`; if that raises (no Chrome installed) it warns
  `chrome channel unavailable (...); using chromium` on the bus and relaunches
  without the channel. BOTH the persistent and the plain path fall back, and only
  a launch that actually asked for the channel does — any other launch error
  re-raises unchanged. `last_engine` records the outcome.
* **Verification.** With evasion on, the returned page is probed once (on
  `about:blank`, no network) for the tells the launch is supposed to have removed:
  `navigator.webdriver`, a `HeadlessChrome` UA, empty `navigator.plugins`, a denied
  `Notification.permission`, and a software WebGL renderer (SwiftShader/llvmpipe).
  It only reports — the verdict lands in `last_stealth` — except under
  `strict_evasion`, where a failed check aborts the launch like any other evasion
  failure. A headless VM trips the WebGL check routinely, so leave Strict off there.
* **No orphaned browser.** Everything after the launch runs inside a handler that
  closes the context and then the browser before re-raising, so a failure in a hook,
  in stealth, in verification, or from a bad context option cannot leave a Chromium
  process behind — the caller's `browser, context, page = create_context(...)`
  never binds, so its own `finally` cannot clean up what it never received.

---

## 4. Optional newer surface  (discover it, never assume it)

Everything in this section is **OPTIONAL**. Each entry exists on some installed
copies of the evasion layer and not others, so **every consumer reaches it
through `hasattr` (or `inspect.signature`) and keeps a fallback to the older
behaviour**. Nothing here may become required: a consumer that crashes, or that
drops a log line or a UI field, when one of these is missing is a bug in the
consumer, not in the manager.

The rule in code, everywhere:

```python
sess = factory.session_info(context) if hasattr(factory, "session_info") else None
proxy = sess.proxy if sess is not None else factory.last_proxy   # always a fallback
```

**Availability.** The entries marked **(new)** below ship only in the newer
evasion modules staged in `_dropin/`. Against the older installed modules they
are simply absent, and every consumer listed keeps working unchanged:

| entry | on | older installed modules |
|-------|-----|-------------------------|
| `BrowserFactory.session_info()` / `last_session` / `SessionInfo` | **(new)** | absent — consumers read `last_*` |
| `BrowserFactory(verify_proxy_http=, detect_engine_version=, use_stealth_lib=)` | **(new)** | absent — `app.py` filters them out of the call |
| `ProxyManager.check_http()` | **(new)** | absent — the Evasion page's "Test exit (HTTP)" button renders disabled |
| `FingerprintManager.set_runtime_browser()` | **(new)** | absent — profiles keep their pinned Chrome version |
| `BrowserFactory.last_engine`, `chrome_channel` | both | present on the installed modules too |
| `FingerprintManager.describe(target)` taking a **dict** | **(new)** | a str/None-only `describe(account_id=None)` |

### `BrowserFactory.session_info(context) -> SessionInfo | None`  **(new)**

The per-launch record, attached to the context that launch produced. This is the
correct accessor for "what did THIS session launch with?". The `last_*`
attributes answer "what did the *last* launch do?", which is the wrong question
the moment two launches overlap — they belong to the factory, and `app.py`
builds one factory for the whole process, so concurrent tasks race on them.
Returns `None` for a context this factory did not create.

`SessionInfo` fields:

| field | type | meaning |
|-------|------|---------|
| `account_id` | `str` | the account this launch was for |
| `proxy` | `dict \| None` | the proxy dict used, or `None` for a direct exit. Mirrors `last_proxy`. |
| `profile` | `dict \| None` | the sticky fingerprint profile the context was built from |
| `fingerprint` | `str` | the `describe()` label. Mirrors `last_fingerprint`. |
| `stealth` | `str` | what stealth did, ending in ` · verify <verdict>`. Mirrors `last_stealth` — **still contains `NOT APPLIED`** when evasion was on and could not be applied, and consumers still key off that substring. |
| `engine` | `str` | the engine label for this launch. Mirrors `last_engine`. |
| `verify_summary` | `str` | the post-launch verdict on its own, e.g. `all checks passed` or `FAILED: ...`. Also embedded at the end of `stealth`. |
| `verify` | `dict` | the full verdict, including its own `warnings` |
| `natural_snapshot` | `dict` | the pre-injection tells, for before/after comparison |
| `proxy_health` | `dict \| None` | the `check_http()` result for this launch, when it ran |
| `warnings` | `list[str]` | non-fatal problems: masked/unmasked WebGL, a UA/engine version gap, a failed proxy health check |

Consumers: `tasks/runner.py` (per-task log line), `core/engine.py` (the login
window's NOT-APPLIED warning). Both fall back to `last_proxy` / `last_stealth` /
`last_fingerprint` when `session_info` is absent, and both then log exactly what
they logged before. The engine / verify / warning lines are sourced from the
session **only**, so on the older layer they are not merely empty — they are not
emitted at all, and the log is byte-for-byte unchanged.

### `BrowserFactory` constructor flags  **(new, except `chrome_channel`)**

`BrowserFactory(..., chrome_channel=True, verify_proxy_http=True,
detect_engine_version=True, use_stealth_lib=True)` — all appended after the
existing keyword parameters, so every existing call site is unaffected.

| flag | default | effect |
|------|---------|--------|
| `chrome_channel` | `True` | launch the installed Google Chrome (`channel="chrome"`), falling back to bundled Chromium. Present on the older modules too. |
| `verify_proxy_http` | `True` | run `check_http()` before each launch that has a proxy. **Costs a real blocking round-trip on the checkout hot path** — turn it off when latency matters more than knowing early. |
| `detect_engine_version` | `True` | probe the engine about to launch and hand its version to `set_runtime_browser()` |
| `use_stealth_lib` | `True` | apply `playwright-stealth` on top of the built-in patches |

`app.py` reads all four from `settings["evasion"]` and **filters them through
`inspect.signature(BrowserFactory.__init__)`**, passing only what the installed
constructor accepts; the rest are dropped with one `warn` line on the bus naming
them. So the same `config.json` boots against either version, and against the
older one construction is byte-for-byte what it always was.

### `ProxyManager.check_http(proxy, timeout=None) -> dict`  **(new)**

A real HTTPS request *through* the proxy — CONNECT tunnel, TLS to the origin,
response body — where `check()` only proves something is listening on the port.
Blocking and a full network round-trip: it belongs on a health screen or a
pre-launch check, never in a tight loop or on a UI thread.

Returns `{ok, stage, status, latency_ms, error, exit_ip, geo, timezone,
summary}`. `stage` says where it stopped: `config`, `tcp`, `tls`, `auth`,
`timeout`, `http`, or `ok`. `summary` is the one-line human form. On success the
exit details are also written back onto the `proxy` dict passed in
(`exit_ip`/`geo`/`timezone`), which is what lets `align_to_proxy()` follow the
country the exit is really in. A failed probe does **not** mark the proxy bad.

Two cautions for consumers: `error` is raw exception text and can carry the
proxy URL, credentials included — `plugins/evasion.py` scrubs the proxy's own
username/password out of anything it displays, and shows proxies through
`mask()`. And because the probe never marks a proxy bad, it must not be allowed
to flip a UI's "N/M reachable" gate, which counts exactly that.

Consumer: `plugins/evasion.py` "Test exit (HTTP)" — rendered **disabled with a
tooltip** when the installed manager has no `check_http`, and run off the UI
thread through `run.io_bound`, the same way "Test reachability" runs
`check_all`. It probes the standalone editable pool and every named group in
`ctx.proxy_groups`, deduped by masked identity so one exit is not probed twice.

### `FingerprintManager.set_runtime_browser(full_version)`  **(new)**

Binds every profile — cached sticky ones rewritten in place, and any created
afterwards — to the browser build that will really serve pages: `chrome_full`,
`chrome_major`, `user_agent`, `sec_ch_ua` and `brands`, with the OS half of each
UA left alone. This is what makes forging `Sec-CH-UA` safe: Chromium emits
client hints from its real build, so a header agreeing with that build is
invisible while one claiming a different version is a direct mismatch.

`chrome_major` is a **`str`** everywhere it is written (`"148"`, not `148`) — in
the pool stamping, in `_new_profile`, and here — and the factory's coherence
check compares it `str()`-normalised against the UA major the page reports.

A falsy/blank version is a deliberate no-op: a failed version probe leaves the
profiles alone rather than blanking their identity. The factory calls this only
when `detect_engine_version` is on **and** the method exists, and a raise
degrades to "profiles keep their pinned version" — never a lost launch.

### `FingerprintManager.describe(target)` — str, dict, or nothing

Widened, not changed: `describe()` and `describe("acct-7")` behave as they
always did. **(new)** is `describe(profile_dict)`, which labels a profile you
already hold without touching the sticky cache. The factory still calls
`describe(account_id)` inside `try/except TypeError` and falls back to
`describe()`, so a manager whose `describe` takes no argument keeps working.

---

### Connective code that stays (never edit for an evasion swap)
proxy pools `proxy_manager.py` / `proxy_groups.py` · launch wiring
`tasks/runner.py`, `core/engine.py`, `auto_checkout.py` · toggles/status
`plugins/evasion.py`, `core/settings.py`, `core/retailer_config.py` ·
human-behaviour `evasion/behavior.py` (from `sites/base.py`) · your hook
`evasion/custom.py` (gitignored).

`from raindance.evasion.browser_factory import LIFECYCLE_HOOKS` prints the live
hook list at any time.
