# RainDance multi-task flow (Phases 0–8)

## Phase 0 — Preparation (GUI)

| UI page | What you configure |
|---------|-------------------|
| **Evasion** | Stealth on/off, `proxies.txt`, proxy groups |
| **Profiles** | Shipping, billing, payment, fingerprint seed, browser dir |
| **Tasks** | URL/PID, qty, profile, proxy group, site, guest/account, monitor, dry-run |
| **CAPTCHA** | Manual harvester (default) or solver API keys |
| **Drop Timer / Control** | Optional `schedule.start_at` |
| **Notifications** | Discord webhook + desktop/sound |

Saved to `config.json` + `data/profiles/*.json`.

## Phase 1 — Start Tasks

**Control → Start Tasks**

1. Reload proxy groups, captcha, profiles  
2. Mark enabled tasks `armed`  
3. Open worker pool (`orchestrator.max_workers`)  
4. If scheduled: idle until `start_at`  
5. Else begin Phase 2 and/or immediate checkout tasks  

## Phase 2 — Monitoring

`MonitorService` (lightweight HTTP + jitter + optional proxy):

- Polls each enabled task with `monitor=true`
- Detects ATC / sold-out signals (site handler + heuristics)
- On stock: Discord `in_stock` + activate sibling tasks sharing the URL

## Phase 3 — Isolated browser sessions

`TaskRunner` per task (own thread + Playwright):

- Sticky proxy from task’s **proxy group**
- Fingerprint from profile seed
- `BrowserFactory` + playwright-stealth when Evasion ON
- Persistent dir from profile when set

## Phase 4 — Navigation + queue

Site handler (`pokemon_center` / `generic`):

- Goto product URL (or PID-derived URL)
- Virtual queue wait (patient poll, no reload storms)
- CAPTCHA pause if challenged

## Phase 5 — ATC + CAPTCHA

- Human-ish click delays
- Quantity / variant if configured
- Manual CAPTCHA window (task status `captcha`) until cleared or timeout

## Phase 6 — Checkout

- Guest checkout path preferred on Pokémon Center
- Auto-fill from bound profile
- Multi-step Continue buttons best-effort

## Phase 7 — Submit + results

- Dry-run stops before place-order (default / force)
- LIVE clicks place-order when dry-run off
- Screenshot + Discord success/failure
- Order id scraped when present

## Phase 8 — Cleanup

- Close context/browser
- Status board update
- Retries (`max_retries`) with backoff
- Continuous monitor remains up if still running

## Code map

```
raindance/tasks/monitor.py      Phase 2
raindance/tasks/runner.py       Phases 3–8
raindance/tasks/orchestrator.py Phase 1 + fan-out
raindance/evasion/*             Phase 3 stealth/proxy/fingerprint
raindance/sites/pokemon_center.py  PC-specific nav/ATC/queue/checkout
raindance/captcha/service.py    Phase 5
raindance/profiles/store.py     Phase 0 identities
```
