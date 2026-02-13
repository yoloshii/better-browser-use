# better-browser-use

Agentic browser automation with ARIA snapshots, three stealth tiers, and human-like behavior simulation.

An AI agent controls the browser by observing ARIA accessibility trees (not screenshots or HTML), reasoning about page state, and executing actions through element refs. Sessions persist cookies, storage, and fingerprints across runs. Anti-bot protection is handled through progressive stealth escalation.

## How It Works

```
Agent                          Server                         Browser
  │                              │                              │
  ├─ launch(tier=1, url) ──────►│─── open browser ────────────►│
  │◄──── {session_id} ──────────│                               │
  │                              │                              │
  ├─ snapshot ──────────────────►│─── ARIA tree ───────────────►│
  │◄──── @e1 link "Login"       │◄── {tree, refs} ─────────────│
  │      @e2 input "Email"      │                              │
  │      @e3 button "Submit"    │                              │
  │                              │                              │
  ├─ click @e1 ─────────────────►│─── humanized click ─────────►│
  │◄──── {page_changed: true}    │◄── result ───────────────────│
  │                              │                              │
  ├─ snapshot ──────────────────►│   (new refs after nav)       │
  │◄──── @e4 input "Password"   │                              │
  │      ...                     │                              │
```

The agent loop: **snapshot** (observe) → **reason** (decide) → **act** (execute) → repeat.

## Quick Start

### Install

```bash
git clone https://github.com/yoloshii/better-browser-use.git
cd better-browser-use
pip install aiohttp pydantic>=2.0
pip install playwright && playwright install chromium
```

### Start Server

```bash
python scripts/server.py --port 8500
```

### Use

```bash
# Launch a browser session
curl -s -X POST http://127.0.0.1:8500/ \
  -H 'Content-Type: application/json' \
  -d '{"op":"launch","tier":1,"url":"https://example.com"}'

# Get ARIA snapshot with element refs
curl -s -X POST http://127.0.0.1:8500/ \
  -H 'Content-Type: application/json' \
  -d '{"op":"snapshot","session_id":"<id>","compact":true}'

# Click an element
curl -s -X POST http://127.0.0.1:8500/ \
  -H 'Content-Type: application/json' \
  -d '{"op":"action","session_id":"<id>","action":"click","params":{"ref":"@e1"}}'

# Close session
curl -s -X POST http://127.0.0.1:8500/ \
  -H 'Content-Type: application/json' \
  -d '{"op":"close","session_id":"<id>"}'
```

## Stealth Tiers

Three browser engines with progressive anti-detection:

| Tier | Engine | Tracker Blocking | Humanization | Use Case |
|------|--------|:---:|:---:|------|
| 1 | Playwright (Chromium) | - | Opt-in | General browsing, friendly sites |
| 2 | Patchright (patched Chromium) | Yes | Auto | Moderate anti-bot (stealth defaults, no `navigator.webdriver` leak) |
| 3 | Camoufox (Firefox C++ fork) | Yes | Auto | Turnstile, DataDome, PerimeterX — with GeoIP + residential proxy |

Dependencies auto-install on first use per tier.

```bash
# Tier 1 (default)
{"op": "launch", "tier": 1, "url": "https://example.com"}

# Tier 2 — stealth Chromium
{"op": "launch", "tier": 2, "url": "https://protected-site.com"}

# Tier 3 — anti-detect Firefox with fingerprint
{"op": "launch", "tier": 3, "url": "https://heavily-protected.com", "profile": "my-identity"}
```

## Actions

### Core

| Action | Params | Description |
|--------|--------|-------------|
| `navigate` | `{url}` | Go to URL |
| `click` | `{ref}` | Click element by ref |
| `fill` | `{ref, value}` | Clear + fill (forms) |
| `type` | `{ref, text, delay_ms?}` | Character-by-character typing (search, compose) |
| `scroll` | `{direction, amount}` | `up`/`down`, pixels or `"page"` |
| `press` | `{key, ref?}` | Keyboard: `"Enter"`, `"Tab"`, `"Escape"` |
| `select` | `{ref, value}` | Dropdown selection |
| `wait` | `{ms}` | Explicit wait (max 30s) |
| `evaluate` | `{js}` | Execute JavaScript (requires `BROWSER_USE_EVALUATE=1`) |
| `screenshot` | `{full_page?}` | Base64 PNG |
| `snapshot` | `{compact?, max_depth?}` | ARIA tree + refs |

### Tabs & Navigation

| Action | Params | Description |
|--------|--------|-------------|
| `go_back` | `{}` | Browser back |
| `tab_new` | `{url?}` | Open new tab |
| `tab_switch` | `{index}` | Switch tab (0-based) |
| `tab_close` | `{index}` | Close tab |
| `cookies_get` | `{domain?}` | Get cookies |
| `cookies_set` | `{cookies}` | Set cookies |

## ARIA Snapshots & Refs

Pages are observed through ARIA accessibility trees, not raw HTML. Each interactive element gets a ref (`@e1`, `@e2`, ...):

```
Page: https://github.com/login | Title: Sign in to GitHub
Tab 1 of 1

- main
  - heading "Sign in to GitHub" @e1 [level=1]
  - form
    - text "Username or email address"
    - textbox @e2
    - text "Password"
    - textbox @e3
    - button "Sign in" @e4
  - link "Forgot password?" @e5
```

Use refs in actions: `{"action": "fill", "params": {"ref": "@e2", "value": "user@example.com"}}`.

Refs reset on every new snapshot. If an action returns "ref not found", take a new snapshot.

## Humanization

Tier 2+ auto-enable human-like behavior simulation. Force for Tier 1 with `BROWSER_USE_HUMANIZE=1`.

| Action | Humanized Behavior |
|--------|-------------------|
| **click** | Bezier curve mouse movement to element, random offset within bounding box, variable settle delay (200-500ms) |
| **type** | Gaussian inter-key delays (~80ms base), digraph optimization (common letter pairs typed faster), occasional thinking pauses |
| **scroll** | Eased acceleration/deceleration, simulated reading pauses between scroll bursts |

Non-humanized path stays unchanged for Tier 1 speed.

## Session Persistence

Profiles store identity state (cookies, localStorage, fingerprints) across sessions:

```
~/.browser-use/profiles/<name>/
  cookies.json
  storage.json       # localStorage + sessionStorage
  meta.json          # tier, domain, timestamps
  fingerprint.json   # Tier 3: BrowserForge-generated identity
```

```bash
# Launch with saved identity
{"op": "launch", "tier": 2, "url": "https://x.com", "profile": "x-primary"}

# Save on close
{"op": "close", "session_id": "<id>", "save_profile": "x-primary"}

# Profile management
{"op": "profile", "action": "list"}
{"op": "profile", "action": "create", "name": "x-primary", "domain": "x.com"}
{"op": "profile", "action": "delete", "name": "x-primary"}
```

## Anti-Bot Detection

After page-changing actions, the server runs lightweight detection for known protections:

- **Cloudflare** (Turnstile, Under Attack Mode)
- **DataDome**
- **Akamai Bot Manager**
- **PerimeterX / HUMAN**
- **Generic CAPTCHA**

When detected, the response includes:
```json
{"success": true, "page_changed": true, "blocked": true, "protection": "cloudflare"}
```

The agent can then escalate to a higher stealth tier.

## Rate Limiting

Per-domain action rate limits protect against detection:

| Domain | Limit |
|--------|-------|
| Default | 8/min |
| linkedin.com | 4/min |
| facebook.com | 5/min |
| x.com / twitter.com | 6/min |
| instagram.com | 4/min |

Read-only actions (snapshot, screenshot, cookies_get) are exempt. When rate limited:

```json
{"success": false, "code": "RATE_LIMITED", "wait_seconds": 8.2}
```

## Tracker Blocking

Tier 2 and 3 sessions automatically block 25+ tracking/fingerprinting patterns via route interception:

- Google Analytics / Tag Manager
- Facebook Pixel
- FingerprintJS, DataDome, PerimeterX, Akamai scripts
- Session recording (Hotjar, FullStory)
- Ad tracking (DoubleClick, Google Syndication)

Not applied to Tier 1 (no stealth pretense).

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `BROWSER_USE_TOKEN` | _(empty)_ | Bearer auth token. Omit to disable auth. |
| `BROWSER_USE_EVALUATE` | `1` | `0` to disable `evaluate` (arbitrary JS) |
| `BROWSER_USE_HUMANIZE` | `0` | `1` to force humanization on all tiers |
| `BROWSER_USE_GEO` | _(empty)_ | Geo profile: `us`, `uk`, `de`, `jp`, `au`, `br`, `in`, etc. |
| `PROXY_SERVER` | _(empty)_ | Proxy URL for Tier 2/3 (e.g., `http://proxy:8080`) |
| `PROXY_USERNAME` | _(empty)_ | Proxy auth username |
| `PROXY_PASSWORD` | _(empty)_ | Proxy auth password |

### Geo Profiles

Match browser timezone/locale to proxy exit location:

| Code | Timezone | Locale |
|------|----------|--------|
| `us` | America/New_York | en-US |
| `us-la` | America/Los_Angeles | en-US |
| `us-tx` | America/Chicago | en-US |
| `uk` | Europe/London | en-GB |
| `de` | Europe/Berlin | de-DE |
| `fr` | Europe/Paris | fr-FR |
| `jp` | Asia/Tokyo | ja-JP |
| `au` | Australia/Sydney | en-AU |
| `br` | America/Sao_Paulo | pt-BR |
| `in` | Asia/Kolkata | en-IN |

## Error Handling

Errors are classified by recoverability:

| Error | Recovery | Agent Action |
|-------|----------|-------------|
| Element not found / ref invalid | RECOVERABLE | Re-snapshot, retry with new refs |
| Navigation timeout | RECOVERABLE | Retry navigate, check URL |
| Rate limited (429) | RECOVERABLE | Wait, then retry slower |
| Anti-bot detection | ESCALATABLE | Escalate to higher stealth tier |
| CAPTCHA detected | ESCALATABLE | Escalate tier or wait and retry |
| Page crashed / context destroyed | NON_RECOVERABLE | Relaunch session |
| Session expired | NON_RECOVERABLE | Launch new session |

## Architecture

```
scripts/
  server.py            # aiohttp HTTP server, auth, routing, rate limiting, block detection
  agent.py             # stdin/stdout JSON interface (alternative to server)
  browser_engine.py    # Multi-tier browser lifecycle, tracker blocking, session management
  actions.py           # Action dispatcher (18 actions) with humanization layer
  behavior.py          # Bezier mouse curves, Gaussian typing delays, eased scrolling
  detection.py         # Anti-bot detection (Cloudflare/DataDome/Akamai/PerimeterX)
  fingerprint.py       # SQLite-backed fingerprint persistence, rotation on block rate
  rate_limiter.py      # Per-domain sliding window rate limiter
  snapshot.py          # ARIA tree parser, ref assignment
  session.py           # Profile persistence (cookies/storage/fingerprints)
  agent_fsm.py         # State machine for agent loop
  context_compaction.py # LLM history summarization for long sessions
  errors.py            # Error classification with AI-friendly transforms
  config.py            # Settings, geo profiles, env vars
  models.py            # Pydantic v2 type definitions
```

## Dependencies

**Core (all tiers):**
```bash
pip install aiohttp pydantic>=2.0
```

**Tier 1 — Playwright:**
```bash
pip install playwright && playwright install chromium
```

**Tier 2 — Patchright** (stealth Chromium):
```bash
pip install patchright && patchright install chromium
```

**Tier 3 — Camoufox** (anti-detect Firefox):
```bash
pip install camoufox[geoip] && python -m camoufox fetch
pip install playwright  # Camoufox uses Playwright Firefox protocol
```

All tiers auto-install their browser binaries on first use.

**Python 3.10+ required.**

## License

MIT
