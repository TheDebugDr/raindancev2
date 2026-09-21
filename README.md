# RainDance

A modular, plugin-driven **desktop app** for personal restock hunting. It
watches a product page and either (a) **alerts you** the moment it restocks so
you can buy it yourself, or (b) drives a real browser (via Playwright) through
add-to-cart → checkout on **your own single account**.

Two ways to use it:

- **Desktop app** — `python app.py` opens a native window with a tool sidebar,
  forms for every setting, and a live log panel. Start here.
- **CLI** — `python auto_checkout.py config.json …` is the same backend from a
  terminal. Documented lower down.

**Dry-run by default.** The final "place order" step is skipped until you
explicitly go live, so you can test the whole flow safely.

### Scope — personal single-buyer tool

Built to help you get **one** item for yourself on your own account. Keep
polling polite. If you hit a queue or CAPTCHA, clear it yourself.

An optional **Evasion** layer (stealth scripts + proxies) can be toggled on
for browser sessions used by **Orders** and **Login Session**. HTTP stock
checks on Execute are unaffected.

## Desktop app

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
python app.py                       # opens the RainDance window
```

The window has a **sidebar of pages** (each is a self-contained plugin), the
selected page's **panel**, and a **live log** that streams straight from the
backend. Pages shipped today:

| Page | What it does |
|------|--------------|
| **Control** | **Start / Stop** the multi-task bot (Phase 1). Live task board + schedule. |
| **Profiles** | Phase 0 identities: address, payment, fingerprint seed. |
| **Tasks** | Phase 0 task config: URL/PID, profile, proxy group, site handler, dry-run. |
| **Monitors** | Product discovery dashboard (SerpApi / mock). |
| **Orders** | Legacy single-product browser checkout (still available). |
| **Execute** | HTTP stock/price checks across monitored products. |
| **Drop Timer** | Countdown / `schedule.start_at`. |
| **Login Session** | Manual login into a persistent browser profile. |
| **Evasion** | Stealth + proxies for task sessions. |
| **CAPTCHA** | Manual harvester settings + optional solver keys. |
| **Notifications** | Discord / desktop / sound. |

Full phase map: [`docs/PHASES.md`](docs/PHASES.md).

### Evasion (optional)

```bash
cp proxies.example.txt proxies.txt   # optional — one proxy per line
```

1. Open the **Evasion** tool → enable the master switch → save.
2. (Optional) paste proxies into the file editor and save.
3. **Login Session** / **Orders** will log `evasion ON` when launching Chromium.
4. Test with **Launch test browser** on the Evasion page (opens amiunique.org).

When evasion is **off**, browsers launch exactly as the original drop-alert app.

Everything is driven by forms — no hand-editing config files. Products and
settings persist to `config.json` (gitignored); your login lives only in the
`profile/` folder (also gitignored).

### Discovery search & the internet search API

The Monitors page has a slide-over **search panel** that queries the internet
for popular Pokémon products and shows result cards (name, image, retailer,
price). Clicking **Add** on a card routes the product into the correct retailer
tab automatically — creating that tab if it's new.

- **Live search (SerpApi Google Shopping):** add your key one of two ways —
  paste it into **Search source / SerpApi key** in the search panel (stored in
  gitignored `config.json`, shown masked), or set the `SERPAPI_KEY` env var.
  Get a key at [serpapi.com](https://serpapi.com/) (free tier available). The
  panel header shows the active source (`SerpApi (live)` vs `Mock (offline)`).
- **No key:** a well-structured mock of popular Pokémon products is returned
  (with real product thumbnails), so the panel is fully functional offline.

Parsing notes (from SerpApi's real response schema): tabs route by the result's
`source` (retailer) — its `product_link` points at Google Shopping, not the
store — and the price comes from `extracted_price` (falling back to parsing the
`price` string). Invalid-key / quota errors are surfaced in the live log and the
panel falls back to the mock. For a different backend (Brave, Google, …) add one
`search_*` function in `raindance/core/search.py`; the UI never changes.

You can also point `search.serpapi_base` (config) at a self-hosted / test
endpoint — the real path was verified end-to-end against a local SerpApi-shaped
server before shipping.

### How checks work

The Execute page has two modes:

- **Run once** — a single pass over the selected products.
- **Auto-monitor** — keeps re-checking, each product on its own **frequency**
  (`5s / 10s / 15s / 30s`, default `30s`). Runs on a background thread so the UI
  stays responsive; a failing check is logged and skipped, never killing the
  loop; toggle off to stop.

Each check does a real, site-agnostic HTTP fetch and reads stock state + price
from the page heuristically. Server-rendered pages work; JS-heavy or
bot-protected retailers (Amazon/Target often) block it, which is reported
honestly as **Unknown/Error** rather than faked. Note: a per-product `frequency`
is a real interval now — keep it sane (≤ 30s is aggressive; it's polling real
sites from your one connection).

### Architecture (built to extend)

The backend is a **module registry**, so adding a tool means *dropping a file*,
not editing the core:

```
app.py                     desktop UI: sidebar + panels + live log
auto_checkout.py           existing backend (wrapped, not rewritten)
notifier.py                CLI notifier (unchanged)
raindance/
├── core/
│   ├── registry.py        ToolPlugin base + @register_tool + auto-discovery
│   ├── notifications.py   NotificationProvider base + @register_provider + hub
│   ├── store.py           ProductStore — monitored products, grouped by site
│   ├── sites.py           URL → retailer detection (drives the dynamic tabs)
│   ├── search.py          SearchService — SerpApi (live) or mock, + placeholders
│   ├── scanner.py         Scanner — HTTP stock/price checks on a worker thread
│   ├── engine.py          runs CheckoutBot on a worker thread (non-blocking UI)
│   ├── settings.py        config.json ↔ backend config
│   ├── events.py          thread-safe log bus (backend → UI)
│   └── context.py         services handed to each plugin/page
├── plugins/               ← drop a ToolPlugin here → new sidebar page
│   ├── monitors.py  execute.py  scheduler.py  session.py  notifications.py
└── providers/             ← drop a NotificationProvider here → new alert channel
    ├── discord.py  desktop.py  sound.py
```

- **Add a tool:** new file in `plugins/` subclassing `ToolPlugin` with
  `@register_tool`. It appears in the sidebar automatically.
- **Add a notification channel:** new file in `providers/` subclassing
  `NotificationProvider` with `@register_provider` (copy `discord.py`). It shows
  up in the Notifications panel and starts receiving alerts — Discord is just
  the first one, not a special case.
- **Responsiveness:** the monitor/checkout runs on a daemon worker thread and
  streams logs through a queue; the UI thread never blocks. Stop is cooperative.

The backend got two small **additive** hooks for this (a log sink and a
`should_stop` check) — no existing logic was rewritten, and the CLI still works
exactly as before.

## CLI

The same backend from a terminal, if you prefer it:

## Setup

Requires Python 3.8+.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium

cp config.example.json config.json     # edit selectors + product URL
cp .env.example .env                    # fill in your shipping/payment details
```

Then edit `config.json` so the CSS selectors match your target site (see
below), and put your personal details in `.env`.

## Usage

```bash
# MONITOR: watch and just alert me on restock (Discord/desktop/sound). Never buys.
python auto_checkout.py config.json --monitor

# Check stock once and report, don't buy
python auto_checkout.py config.json --check-only

# Test that your checkout selectors work (skips stock polling), dry-run
python auto_checkout.py config.json --checkout-now

# Watch the product and dry-run checkout when it drops (safe default)
python auto_checkout.py config.json

# Go live — actually places the order when in stock, on your account
python auto_checkout.py config.json --live
```

`--monitor` is the safest, most useful mode for a collector: it just pings you
so you can buy manually. The checkout modes act on your own logged-in account.

Flags: `--headful` / `--headless` override the browser mode, `--dry-run`
forces safe mode even over a live config, `--start-at "HH:MM"` arms it for a
known drop time, `--env PATH` points at a different env file.

## Being ready for a known drop

Two options that help you show up prepared for an announced drop — both are
just "be logged in" and "start on time", nothing sneaky:

- **Stay logged in** — set `browser.user_data_dir` to a folder (e.g.
  `"profile"`). Run once with `--headful`, log into *your own* account in the
  window, done. That profile (cookies + session) is reused every run, so
  checkout starts already signed in with your saved address/payment.
- **Start on time** — `schedule.start_at` or `--start-at` makes it idle until
  a time you set, then begin watching. Use ISO (`2026-07-12T09:59:30`) or just
  `HH:MM`. Arm it a little before the announced drop:

  ```bash
  python auto_checkout.py config.json --monitor --start-at "09:59:30"
  ```

## Restock alerts

When the item flips to in-stock you get an alert on every channel you enable
in `config.json` under `notifications`:

- **Discord** — set `DISCORD_WEBHOOK` in `.env` (Server Settings → Integrations
  → Webhooks → New Webhook → Copy URL). Leave blank to disable.
- **desktop** — native macOS/Linux notification banner.
- **sound** — a terminal bell plus a system chime on macOS.

`on` picks which events alert: `in_stock`, `checkout_success`, `error`. In
`--monitor` mode you're alerted once per restock (not on every poll while it
stays up).

## How the config works

- **`product.url`** — the page to watch.
- **`product.in_stock`** — how to detect availability. Any combination of:
  - `selector` — an element that exists when buyable (e.g. the add-to-cart button)
  - `expect_enabled` / `expect_visible` — the element must be clickable
  - `text_present` — this text must appear (e.g. "Add to Cart")
  - `text_absent` — if this appears, treat as out of stock (e.g. "Sold Out")
- **`login`** — optional; set `enabled: true` and list steps to sign in first.
  (If you use `browser.user_data_dir`, you can skip this and just stay logged in.)
- **`schedule.start_at`** — optional time to wait for before watching.
- **`browser.user_data_dir`** — optional folder for a persistent login profile.
- **`checkout_steps`** — the ordered actions to buy. Each step is one of:

  | action | fields | does |
  |--------|--------|------|
  | `goto` | `url` | navigate |
  | `click` | `selector` | click element |
  | `fill` | `selector`, `value` | set an input's value |
  | `type` | `selector`, `value`, `delay_ms` | type key-by-key |
  | `select` | `selector`, `value` | pick a `<select>` option |
  | `check` | `selector` | tick a checkbox |
  | `press` | `selector`, `key` | send a key (default Enter) |
  | `wait_for` | `selector`, `state` | wait for an element |
  | `wait_ms` | `value` | pause N ms |
  | `screenshot` | `name` | save a screenshot |

  Mark the real order-placing step with `"final": true` — that's the one the
  dry-run stops before.

- **`poll`** — `interval_seconds` + random `jitter_seconds` between checks,
  `max_attempts` (0 = forever). Keep this polite; hammering a site is rude and
  can get you blocked.
- **`browser`** — `headless`, `slow_mo_ms`, `viewport`, `keep_open`.

`${VAR}` anywhere in the config is replaced from the environment / `.env`, so
secrets never live in the committed JSON.

## Finding selectors

Open the product/checkout page in Chrome, right-click the element →
Inspect → right-click the node → Copy → Copy selector. Prefer stable IDs or
`data-` attributes over long auto-generated class chains.

## Please use this responsibly

Intended for automating **your own** personal purchases. Check the site's
terms of service before automating it, use your own account and payment
details, and keep the polling interval reasonable. Don't use it to bulk-buy
scarce goods or to resell.
