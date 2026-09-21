# raindance.sites — handler override layer

Every handler in this package (built-in retailers and stores the user
registers) reads its selectors, form fields, frame hints, waits and
confirmation rules from ONE merged view:

```
per-store config  >  handler defaults  >  generic defaults
task["site_params"]  <Handler>.DEFAULT_SITE_PARAMS  GenericHandler.DEFAULT_SITE_PARAMS
```

`task["site_params"]` is `retailer_configs[<site id>].site_params` from
config.json merged with the task row's keys (see `core/execute_queue.py`).
The merge is implemented once, in `base.py` (`SiteHandler.effective`,
`effective_params`, `default_site_params`); dicts deep-merge, lists and
scalars replace.

## The contract

```jsonc
"site_params": {
  "selectors": {
    "atc":          ["button[data-test='add-to-cart']", "..."],
    "checkout":     ["a[href*='/checkout']"],
    "place_order":  ["button#place-order"],
    "queue":        ["text=You're in line", "#queue-it"],   // empty = no queue
    "confirmation": ["h1:has-text('Thanks for your order')"]
  },
  "fields":      { "#email": "email", "#card": "payment.card_number" },
  "fields_only": false,        // true → only `fields`, drop handler defaults
  "frames":      ["payment", "stripe"],   // frame name / URL substring hints
  "waits":       { "atc_enable_s": 30, "queue_max_s": 2700, "confirm_s": 20 },
  "confirmation": {
    "order_id_regex": "order\\s*(?:number|#)[:\\s#]*([0-9]{6,})",
    "success_text":   ["thank you", "order confirmed"]
  }
}
```

Every key is optional; an absent key inherits. A selector value may be one
string or a list. `fields` values are dotted profile paths (`email`,
`phone`, `shipping.zip`, `billing.city`, `payment.cvc` …). The older store
shape `{"<profile.path>": "<css>"}` is still accepted.

Some handlers honour extra selector keys (Pokémon Center: `guest`,
`continue`, `variant`, `sold_out`; generic: `quantity`).
`raindance.sites.handler_capabilities(site_id)["honors"]` lists them.

## Helpers for the UI

* `raindance.core.retailer_config.default_site_params_for(site_id)` — the
  handler's defaults in the shape above (placeholders for the editor).
* `raindance.core.retailer_config.effective_site_params_for(settings, site)`
  — defaults with the saved overrides on top (what a run will use).
* `raindance.sites.handler_capabilities(site_id)` — `custom_queue`,
  `custom_atc`, `custom_checkout`, `frame_aware`, `seller_detection`, `honors`.

## Lookup path

`SiteHandler.find_first(page, selector)` → `frames.find_first` searches the
main document and every child frame (`frames` hints bias the order). It is
the only place that calls `page.locator` directly. `find_any`,
`wait_for_first`, `any_visible`, `human_click`, `human_type`, `fill_if` all
route through it.

## Shared building blocks

* `wait_out_queue` — poll `selectors.queue` until nothing is visible, up to
  `waits.queue_max_s`; CAPTCHA check each pass; "Access denied" aborts.
* `GenericHandler.enter_checkout / fill_checkout_fields / place_order /
  confirm_order` — the checkout pieces; `place_order` returns
  `{ok: False, error}` when no button matches and never fabricates an id.

## Adding a retailer

Copy `_template.py`, keep the phase signatures, register the class in
`__init__.py`. Best Buy / Walmart / Target defaults are best-effort and
unverified against the live sites — fix them from the Catalog page when
they drift; no code change needed.
