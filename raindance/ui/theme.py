"""Design tokens + shared components for the drop-lifecycle UI.

Storm-indigo ground, a signal-amber accent spent only on the drop moment
(the START disc and the countdown), and semantic colours kept off the accent
so "in stock" never reads as "armed". Monospace carries the display role.

Call `inject()` once per page, then build with the components below.
"""
from __future__ import annotations

import html as _html
from contextlib import contextmanager

from nicegui import ui


def esc(value: object) -> str:
    """Escape a value before it goes into trusted markup.

    Product names, retailer names and frequencies come from config.json and the
    search API — author-controlled markup is fine, interpolated data is not.
    """
    return _html.escape(str(value))

STATUS_TEXT = {
    "in_stock": "In stock",
    "in_stock_retail": "In stock · retail",
    "in_stock_reseller": "Reseller",
    "in_stock_over_msrp": "Over MSRP",
    "in_stock_unverified": "Unverified",
    "out_of_stock": "Out of stock",
    "blocked": "Blocked",
    "error": "Error",
    "unknown": "Unknown",
    "pending": "Pending",
}

# Task status -> chip colour class (reuses the product status palette).
TASK_CHIP = {
    "success": "in_stock", "failed": "error", "stopped": "pending",
    "monitoring": "watch", "checkout": "watch", "atc": "watch",
    "launching": "watch", "queued": "watch", "submitting": "watch",
    "triggered": "unknown", "captcha": "unknown", "retrying": "unknown",
    "armed": "pending", "idle": "pending",
    # seller/MSRP classifier verdicts (product-level)
    "in_stock_retail": "in_stock", "in_stock_reseller": "error",
    "in_stock_over_msrp": "unknown", "in_stock_unverified": "unknown",
    "blocked": "unknown",
}

CSS = """
<style>
:root {
  --rd-ground:#EEF0F6; --rd-surface:#FFFFFF; --rd-surface-2:#E4E8F2;
  --rd-line:#D2D8E6; --rd-line-soft:#E2E6F0;
  --rd-ink:#131829; --rd-ink-dim:#5C6480; --rd-ink-faint:#8D94AC;
  --rd-accent:#9A6212; --rd-accent-lit:#C97F17;
  --rd-ok:#17795A; --rd-live:#C0322F; --rd-watch:#2C5CB8; --rd-idle:#767D97;
  --rd-term-bg:#070A10; --rd-term-ink:#7CFC98;
  --rd-mono: ui-monospace,"SF Mono",SFMono-Regular,Menlo,Consolas,monospace;
}
body.body--dark, .dark {
  --rd-ground:#0C0F1A; --rd-surface:#151A2B; --rd-surface-2:#1E2540;
  --rd-line:#2E3654; --rd-line-soft:#232B47;
  --rd-ink:#E8EAF4; --rd-ink-dim:#8A90AD; --rd-ink-faint:#626A8A;
  --rd-accent:#F2A93B; --rd-accent-lit:#FFC062;
  --rd-ok:#43C08A; --rd-live:#F0524D; --rd-watch:#5B8CE6; --rd-idle:#626A8A;
}

.rd-lab { font-family:var(--rd-mono); text-transform:uppercase; letter-spacing:.13em;
          font-size:10.5px; font-weight:600; color:var(--rd-ink-faint); }
.rd-title { font-family:var(--rd-mono); text-transform:uppercase; letter-spacing:.14em;
            font-size:13px; font-weight:700; color:var(--rd-ink); }
.rd-lede { color:var(--rd-ink-dim); max-width:62ch; font-size:14px; }
.rd-num { font-variant-numeric:tabular-nums; }
.rd-mono { font-family:var(--rd-mono); }

.rd-card { background:var(--rd-surface); border:1px solid var(--rd-line);
           border-radius:5px; }

.rd-chip { font-family:var(--rd-mono); font-size:9.5px; font-weight:700;
           letter-spacing:.09em; text-transform:uppercase;
           padding:3px 6px; border-radius:2.5px; white-space:nowrap; }
.rd-in_stock { color:var(--rd-ok);    background:color-mix(in srgb,var(--rd-ok) 14%,transparent); }
.rd-error    { color:var(--rd-live);  background:color-mix(in srgb,var(--rd-live) 14%,transparent); }
.rd-unknown  { color:var(--rd-accent);background:color-mix(in srgb,var(--rd-accent) 16%,transparent); }
.rd-pending  { color:var(--rd-idle);  background:color-mix(in srgb,var(--rd-idle) 16%,transparent); }
.rd-watch    { color:var(--rd-watch); background:color-mix(in srgb,var(--rd-watch) 14%,transparent); }
.rd-out_of_stock { color:var(--rd-live); background:color-mix(in srgb,var(--rd-live) 14%,transparent); }

/* lifecycle rail */
.rd-rail { display:grid; grid-template-columns:repeat(5,1fr);
           border-top:1px solid var(--rd-line); border-bottom:1px solid var(--rd-line);
           background:var(--rd-surface); }
.rd-node { padding:14px 10px 13px; text-align:left; cursor:pointer; position:relative;
           border-right:1px solid var(--rd-line-soft); }
.rd-node:last-child { border-right:0; }
.rd-node:hover { background:var(--rd-surface-2); }
.rd-node .rd-idx { font-family:var(--rd-mono); font-size:10px; letter-spacing:.1em;
                   color:var(--rd-ink-faint); font-variant-numeric:tabular-nums; }
.rd-node .rd-nm { font-family:var(--rd-mono); text-transform:uppercase; letter-spacing:.1em;
                  font-size:11.5px; font-weight:600; color:var(--rd-ink-dim); }
.rd-node.rd-viewing .rd-nm { color:var(--rd-ink); }
.rd-node.rd-viewing::after { content:""; position:absolute; left:0; right:0; bottom:-1px;
                             height:2px; background:var(--rd-ink); }
.rd-dot { position:absolute; top:16px; right:12px; width:7px; height:7px;
          border-radius:50%; background:var(--rd-idle); opacity:.5; }
.rd-node.rd-bot  .rd-dot { background:var(--rd-accent); opacity:1; }
.rd-node.rd-bot  .rd-nm  { color:var(--rd-accent); }
.rd-node.rd-done .rd-dot { background:var(--rd-ok); opacity:1; }

/* retailer accordion */
.rd-acchead { display:flex; align-items:center; gap:12px; padding:13px 15px; cursor:pointer; }
.rd-acchead:hover { background:var(--rd-surface-2); }
.rd-caret { font-family:var(--rd-mono); font-size:10px; color:var(--rd-ink-faint); width:9px; }
.rd-rname { font-size:14.5px; color:var(--rd-ink-dim); }
.rd-rname.rd-on { color:var(--rd-ink); }
.rd-accbody { background:color-mix(in srgb,var(--rd-surface-2) 42%,transparent);
              border-top:1px solid var(--rd-line-soft); }
.rd-item { display:flex; align-items:center; gap:13px; padding:11px 15px 11px 36px;
           border-bottom:1px solid var(--rd-line-soft); }
.rd-empty { padding:14px 15px 14px 36px; color:var(--rd-ink-faint); font-size:13px; }

.rd-row { display:flex; align-items:center; gap:13px; padding:13px 15px;
          border-bottom:1px solid var(--rd-line-soft); cursor:pointer; }
.rd-row:hover { background:var(--rd-surface-2); }
.rd-pname { font-size:14.5px; color:var(--rd-ink); }
.rd-pmeta { color:var(--rd-ink-faint); font-size:12px; }

/* the drop moment. Quasar's bg-* utilities are !important, so match them. */
.q-btn.rd-disc, .rd-disc {
  width:172px; height:172px; min-height:172px; border-radius:50%; border:0; padding:0;
  background:radial-gradient(circle at 50% 42%,var(--rd-accent-lit),var(--rd-accent) 62%) !important;
  color:#12100A !important; font-family:var(--rd-mono); font-weight:700; letter-spacing:.2em;
  text-transform:uppercase; font-size:15px;
  transition:transform .18s cubic-bezier(.2,.8,.3,1);
  box-shadow:0 0 0 1px color-mix(in srgb,var(--rd-accent) 35%,transparent),
             0 18px 50px -20px var(--rd-accent);
}
.q-btn.rd-disc .q-btn__content { color:#12100A; }
.q-btn.rd-disc:hover, .rd-disc:hover { transform:scale(1.035); }

/* concentric rain rings — the ritual, not decoration */
.rd-ritual { position:relative; display:grid; place-items:center; }
.rd-ritual i {
  position:absolute; width:172px; height:172px; border-radius:50%;
  border:1px solid var(--rd-accent); opacity:0; pointer-events:none;
  animation:rd-rain 3.6s ease-out infinite;
}
.rd-ritual i:nth-of-type(2) { animation-delay:1.2s; }
.rd-ritual i:nth-of-type(3) { animation-delay:2.4s; }
@keyframes rd-rain {
  0%   { transform:scale(1);   opacity:.55; }
  100% { transform:scale(1.7); opacity:0; }
}
@media (prefers-reduced-motion: reduce) { .rd-ritual i { animation:none; } }
.rd-clock { font-family:var(--rd-mono); font-weight:700; font-variant-numeric:tabular-nums;
            font-size:clamp(40px,9vw,74px); color:var(--rd-accent); line-height:1; }

.rd-block { padding:11px 12px; border-radius:3px; font-size:12.5px; color:var(--rd-ink);
            background:color-mix(in srgb,var(--rd-live) 10%,transparent);
            border:1px solid color-mix(in srgb,var(--rd-live) 40%,transparent); }
.rd-block b { color:var(--rd-live); }
.rd-needyou { padding:10px 11px; border-radius:3px; font-size:12.5px;
              background:color-mix(in srgb,var(--rd-accent) 13%,transparent);
              border:1px solid color-mix(in srgb,var(--rd-accent) 45%,transparent); }

.rd-badge-dry  { color:var(--rd-ok); border:1px solid var(--rd-ok); }
.rd-badge-live { color:#fff; background:var(--rd-live); border:1px solid var(--rd-live); }
.rd-badge-off  { color:var(--rd-ink-faint); border:1px solid var(--rd-line); }
.rd-badge { font-family:var(--rd-mono); font-size:10.5px; font-weight:700;
            letter-spacing:.12em; text-transform:uppercase; padding:5px 10px; border-radius:3px; }

.rd-sess { padding:15px; border-left:2px solid var(--rd-idle); }
.rd-sess.rd-s-captcha { border-left-color:var(--rd-accent); }
.rd-sess.rd-s-success { border-left-color:var(--rd-ok); }
.rd-sess.rd-s-failed  { border-left-color:var(--rd-live); }
.rd-sess.rd-s-active  { border-left-color:var(--rd-watch); }

.rd-note { padding:15px 16px; border-radius:5px; border:1px dashed var(--rd-line);
           color:var(--rd-ink-dim); font-size:13px; }
.rd-note code, .rd-pmeta code { font-family:var(--rd-mono); font-size:11.5px;
           background:var(--rd-surface-2); padding:1px 5px; border-radius:2px; color:var(--rd-ink); }

/* grouped drawer navigation - themed to match, replacing the old flat list */
.rd-nav.q-btn { color:var(--rd-ink-dim); border-radius:4px; margin:1px 4px;
                min-height:36px; font-weight:500; letter-spacing:.01em; }
.rd-nav.q-btn .q-btn__content { justify-content:flex-start; }
.rd-nav.q-btn:hover { background:var(--rd-surface-2); color:var(--rd-ink); }
.rd-nav-on.q-btn { color:var(--rd-ink); background:var(--rd-surface-2); }
.rd-nav-on.q-btn::before { content:""; position:absolute; left:0; top:7px; bottom:7px;
                           width:2px; border-radius:2px; background:var(--rd-accent); }

/* header: the mark + wordmark are one click target back to Start */
.rd-brand { display:flex; align-items:center; gap:10px; cursor:pointer; }

/* header STOP: only shown while the orchestrator runs */
.q-btn.rd-stop { font-family:var(--rd-mono); font-size:10.5px; font-weight:700;
                 letter-spacing:.12em; color:var(--rd-live); padding:3px 10px;
                 border-radius:3px; margin-left:8px; }
.q-btn.rd-stop:hover { background:color-mix(in srgb,var(--rd-live) 14%,transparent); }

/* the slim bar above every non-wizard page: the one way back to Start */
.rd-backbar { display:flex; align-items:center; gap:12px; width:100%; min-height:32px;
              padding:0 0 8px; border-bottom:1px solid var(--rd-line); }
.q-btn.rd-back { font-family:var(--rd-mono); font-size:11px; letter-spacing:.08em;
                 color:var(--rd-ink-dim); border-radius:4px; padding:2px 8px; }
.q-btn.rd-back:hover { color:var(--rd-ink); background:var(--rd-surface-2); }
.rd-backbar-name { font-family:var(--rd-mono); text-transform:uppercase;
                   letter-spacing:.14em; font-size:11px; color:var(--rd-ink-faint); }

/* primary header tabs */
.rd-tab { font-family:var(--rd-mono); text-transform:uppercase; letter-spacing:.12em;
          font-size:11.5px; font-weight:600; color:var(--rd-ink-dim);
          padding:7px 14px; border-radius:4px; cursor:pointer; position:relative; }
.rd-tab:hover { color:var(--rd-ink); background:var(--rd-surface-2); }
.rd-tab-on { color:var(--rd-ink); background:var(--rd-surface-2); }
.rd-tab-on::after { content:""; position:absolute; left:12px; right:12px; bottom:2px;
                    height:2px; border-radius:2px; background:var(--rd-accent); }

/* ---------------- splash ---------------- */
.rd-splash { position:fixed; inset:0; z-index:9999; display:grid; place-items:center;
             background:var(--rd-ground); transition:opacity .55s ease, visibility .55s; }
.rd-splash.rd-gone { opacity:0; visibility:hidden; pointer-events:none; }
.rd-splash-inner { display:flex; flex-direction:column; align-items:center; gap:26px; }
.rd-splash-stage { position:relative; display:grid; place-items:center;
                   width:260px; height:260px; }
/* expanding rain rings */
.rd-splash-stage i { position:absolute; width:120px; height:120px; border-radius:50%;
                     border:1px solid var(--rd-accent); opacity:0;
                     animation:rd-splash-ring 3.2s cubic-bezier(.2,.7,.3,1) infinite; }
.rd-splash-stage i:nth-of-type(2){ animation-delay:.8s; }
.rd-splash-stage i:nth-of-type(3){ animation-delay:1.6s; }
.rd-splash-stage i:nth-of-type(4){ animation-delay:2.4s; }
@keyframes rd-splash-ring {
  0%   { transform:scale(.35); opacity:0; }
  18%  { opacity:.75; }
  100% { transform:scale(2.05); opacity:0; }
}
/* the mark condenses out of the rings */
.rd-splash-mark { width:96px; height:96px; border-radius:50%;
                  border:2px solid var(--rd-accent); position:relative;
                  animation:rd-mark-in 1.15s cubic-bezier(.2,.8,.25,1) both; }
.rd-splash-mark::after { content:""; position:absolute; inset:22px; border-radius:50%;
                         background:radial-gradient(circle at 50% 40%,
                            var(--rd-accent-lit), var(--rd-accent) 65%);
                         box-shadow:0 0 46px -6px var(--rd-accent); }
@keyframes rd-mark-in {
  0%   { transform:scale(2.4); opacity:0; filter:blur(9px); }
  60%  { opacity:1; filter:blur(0); }
  100% { transform:scale(1); opacity:1; }
}
.rd-splash-word { font-family:var(--rd-mono); font-weight:700; font-size:24px;
                  letter-spacing:.52em; text-transform:uppercase; color:var(--rd-ink);
                  padding-left:.52em;
                  animation:rd-word-in 1s ease-out .55s both; }
@keyframes rd-word-in {
  0% { opacity:0; letter-spacing:1.1em; }
  100% { opacity:1; letter-spacing:.52em; }
}
.rd-splash-sub { font-family:var(--rd-mono); font-size:11px; letter-spacing:.2em;
                 text-transform:uppercase; color:var(--rd-ink-faint);
                 animation:rd-fade-in .8s ease-out 1.15s both; }
@keyframes rd-fade-in { from{opacity:0} to{opacity:1} }
.rd-splash-go { animation:rd-fade-in .7s ease-out 1.5s both; }

/* Quasar's .q-btn background wins over an inline style, so match its
   specificity the same way .rd-disc does. */
.q-btn.rd-splash-btn, .rd-splash-btn {
  border-radius:3px !important; padding:12px 42px !important;
  font-family:var(--rd-mono); font-weight:700; letter-spacing:.22em;
  text-transform:uppercase; font-size:12px;
  background:radial-gradient(circle at 50% 40%,
             var(--rd-accent-lit), var(--rd-accent) 65%) !important;
  color:#12100A !important;
  box-shadow:0 14px 40px -16px var(--rd-accent);
  transition:transform .16s cubic-bezier(.2,.8,.3,1);
}
.q-btn.rd-splash-btn .q-btn__content { color:#12100A; }
.q-btn.rd-splash-btn:hover { transform:scale(1.04); }

/* ---------------- stepper ---------------- */
.rd-steps { display:flex; align-items:center; gap:0; }
.rd-step { display:flex; align-items:center; gap:9px; padding:8px 16px;
           font-family:var(--rd-mono); font-size:11px; letter-spacing:.11em;
           text-transform:uppercase; color:var(--rd-ink-faint); cursor:default; }
.rd-step .rd-step-n { width:19px; height:19px; border-radius:50%; display:grid;
                      place-items:center; font-size:9.5px; font-weight:700;
                      border:1px solid var(--rd-line); color:var(--rd-ink-faint); }
.rd-step.rd-step-on { color:var(--rd-ink); }
.rd-step.rd-step-on .rd-step-n { border-color:var(--rd-accent);
                                 background:var(--rd-accent); color:#12100A; }
.rd-step.rd-step-done { color:var(--rd-ink-dim); cursor:pointer; }
.rd-step.rd-step-done .rd-step-n { border-color:var(--rd-ok); color:var(--rd-ok); }
.rd-step-sep { width:26px; height:1px; background:var(--rd-line); }

/* selectable tile (products + retailers) */
.rd-tile { border:1px solid var(--rd-line); border-radius:5px; background:var(--rd-surface);
           cursor:pointer; transition:border-color .14s, background .14s; }
.rd-tile:hover { border-color:var(--rd-ink-faint); background:var(--rd-surface-2); }
.rd-tile.rd-tile-on { border-color:var(--rd-accent);
                      background:color-mix(in srgb, var(--rd-accent) 9%, var(--rd-surface)); }
/* ---------------- thumbnails ---------------- */
/* One well, two states. With no URL it draws exactly the letter/code badge this
   app has always drawn, so an image-less catalog — or a machine with no network
   — renders as it always did. With a URL the image is laid over that same box
   and letterboxed on the well's own ground, so a wide set logo is never cropped
   or stretched. Call sites keep their own dimensions by passing their existing
   well/badge classes, and may style the two states apart via .rd-thumb-has. */
.rd-thumb { position:relative; display:grid; place-items:center;
            --rd-thumb-bg:var(--rd-surface-2); }
/* Clipping is only for the letterbox. The badge-only path stays unclipped so a
   descender can never be shaved off text that renders fine today. */
.rd-thumb.rd-thumb-has { overflow:hidden; }
.rd-thumb > img { position:absolute; inset:0; width:100%; height:100%;
                  object-fit:contain; background:var(--rd-thumb-bg);
                  border-radius:inherit; }
.rd-thumb-txt { font-family:var(--rd-mono); font-weight:700; color:var(--rd-ink-dim);
                letter-spacing:.08em; line-height:1; }
/* the inline size — a thumbnail sitting beside a product name in a row */
.rd-thumb-in { flex:0 0 auto; border-radius:3px; background:var(--rd-surface-2);
               border:1px solid var(--rd-line-soft); }
.rd-thumb-in .rd-thumb-txt { font-size:9px; letter-spacing:.03em; }
</style>

<script>
/* A remote image that 404s or never arrives must not leave a hole: hide it and
   let the badge underneath show through. `error` does not bubble out of <img>,
   so this listens in the capture phase — one listener covering every thumbnail
   on the page, present and future. Head HTML accumulates across tab switches,
   so it registers itself exactly once. */
(function () {
  if (window.__rdThumbFallback) return;
  window.__rdThumbFallback = true;
  document.addEventListener('error', function (ev) {
    var el = ev.target;
    if (el && el.tagName === 'IMG' && el.closest &&
        el.closest('.rd-thumb')) {
      el.style.display = 'none';       /* reveal the badge behind it */
      var w = el.closest('.rd-thumb');
      if (w) w.classList.remove('rd-thumb-has');
    }
  }, true);
})();
</script>
"""

_injected = False


def inject() -> None:
    """Add the token stylesheet once per page build."""
    global _injected
    ui.add_head_html(CSS)
    _injected = True


# --------------------------------------------------------------------------- #
# components
# --------------------------------------------------------------------------- #
def title(text: str):
    return ui.label(text).classes("rd-title")


def lab(text: str):
    return ui.label(text).classes("rd-lab")


def lede(text: str):
    return ui.label(text).classes("rd-lede")


def chip(status: str):
    """A status chip. Accepts product statuses or task statuses."""
    key = TASK_CHIP.get(status, status)
    text = STATUS_TEXT.get(status, status.replace("_", " ").title())
    return ui.label(text).classes(f"rd-chip rd-{key}")


def note(html: str):
    return ui.html(html, sanitize=False).classes("rd-note")   # trusted markup


def block(html: str):
    return ui.html(html, sanitize=False).classes("rd-block")  # trusted markup


@contextmanager
def card(extra: str = ""):
    with ui.element("div").classes(f"rd-card w-full {extra}") as c:
        yield c


# --------------------------------------------------------------------------- #
# thumbnails — one helper, every site
# --------------------------------------------------------------------------- #
def thumb(url: object, fallback: str = "?", *, size: str = "",
          text_class: str = "rd-thumb-txt", classes: str = "",
          style: str = "", tip: str = ""):
    """A thumbnail well: the real image when there is one, the badge when not.

    `fallback` is the letter/code this app already drew — an initials pair, a
    set code, a line's short code — and it is ALWAYS rendered, underneath. That
    is what makes the no-image path byte-identical to the old markup and what
    catches a dead URL: the capture-phase `error` listener in CSS hides a broken
    image and the badge shows through, so a well never renders empty. The image
    is `loading=lazy decoding=async` and carries no server round-trip, so a slow
    or unreachable host never blocks the page — the app works fully offline.

    Call sites pass their own well/badge classes so each keeps its exact
    dimensions; `size` is shorthand for a fixed square (inline thumbnails), and
    `.rd-thumb-has` is set only when an image is actually being drawn, so a site
    can size the two states differently in CSS and not shift its layout.

    :param url: image URL, or "" / None for the badge-only path
    :param fallback: the badge text drawn behind the image
    :param size: fixed square edge, e.g. "26px" (default: fill the parent)
    :param text_class: the badge's class at this call site
    :param classes: extra classes for the well
    :param style: extra inline style for the well
    :param tip: tooltip for the whole well
    """
    src = str(url or "").strip()
    cls = "rd-thumb" + (" rd-thumb-has" if src else "")
    if classes:
        cls += " " + classes
    bits = [style] if style else []
    if size:
        bits.append(f"width:{size};height:{size};min-width:{size}")
    well = ui.element("div").classes(cls).style(";".join(bits))
    with well:
        ui.label(str(fallback or "?")).classes(text_class)
        if src:
            # `tag=img` makes NiceGUI render a plain <img> instead of Quasar's
            # <q-img>, which is what lets object-fit and the error fallback work.
            ui.image(src).props(
                "tag=img loading=lazy decoding=async "
                "onerror=\"this.style.display='none';"
                "this.closest('.rd-thumb')&&"
                "this.closest('.rd-thumb').classList.remove('rd-thumb-has')\"")
    if tip:
        well.tooltip(tip)
    return well


def set_art(catalog: object, set_id: str) -> str:
    """A set's logo URL, or "" — never an exception.

    `CatalogStore.set_image` is part of the catalog's image work and may not
    exist yet in a given build, so this falls back to the record's own `image`
    field and then to nothing at all, which `thumb` renders as the badge.
    """
    if not catalog or not set_id:
        return ""
    getter = getattr(catalog, "set_image", None)
    if callable(getter):
        try:
            return str(getter(set_id) or "").strip()
        except Exception:                                   # noqa: BLE001
            pass
    try:
        rec = catalog.get_set(set_id) or {}                  # type: ignore[attr-defined]
        return str(rec.get("image") or "").strip()
    except Exception:                                        # noqa: BLE001
        return ""


def product_art(catalog: object, set_id: str, line_id: str = "") -> str:
    """A product's image URL, falling back to the set logo, then to "".

    Same contract as `set_art`: `CatalogStore.product_image` may be absent, and
    an absent one is not an error — it just means the badge gets drawn.
    """
    if not catalog:
        return ""
    getter = getattr(catalog, "product_image", None)
    if callable(getter) and set_id and line_id:
        try:
            found = str(getter(set_id, line_id) or "").strip()
            if found:
                return found
        except Exception:                                   # noqa: BLE001
            pass
    return set_art(catalog, set_id)
