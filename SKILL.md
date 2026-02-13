---
name: browser-use
version: 0.1.0
description: Agentic browser automation with persistent sessions and ARIA snapshot-based navigation. Use when user needs to browse websites, interact with web pages, fill forms, login to sites, warm up social accounts, bypass anti-bot protection, or perform any multi-step browser task. Three stealth tiers (Playwright, Patchright, Camoufox), session persistence with cookie/storage profiles, element ref system, idle session GC, and per-session locking.
triggers:
  - browse
  - visit
  - navigate to
  - open website
  - warm up
  - social warming
  - browser agent
  - interact with page
  - fill form
  - login to
  - bypass anti-bot
---

# Browser-Use Skill

Agentic browser controller. YOU are the agent — observe page state via ARIA snapshots, reason about what to do, execute actions, repeat until done.

## Quick Start

```bash
# Launch session
curl -s -X POST http://127.0.0.1:8500/ -H 'Content-Type: application/json' \
  -d '{"op":"launch","tier":1,"url":"https://example.com"}'

# Snapshot (get ARIA tree with @e1, @e2 refs)
curl -s -X POST http://127.0.0.1:8500/ -H 'Content-Type: application/json' \
  -d '{"op":"snapshot","session_id":"<id>","compact":true}'

# Click element
curl -s -X POST http://127.0.0.1:8500/ -H 'Content-Type: application/json' \
  -d '{"op":"action","session_id":"<id>","action":"click","params":{"ref":"@e1"}}'

# Close
curl -s -X POST http://127.0.0.1:8500/ -H 'Content-Type: application/json' \
  -d '{"op":"close","session_id":"<id>"}'
```

## Execution

Persistent HTTP server on port 8500. All requests: `POST /` with JSON body.

Server binds `127.0.0.1` by default. Set `BROWSER_USE_TOKEN` for Bearer auth. Set `BROWSER_USE_EVALUATE=1` to enable arbitrary JS execution.

```bash
# Health check (no auth required)
curl -s http://127.0.0.1:8500/health

# With auth
curl -s -X POST http://127.0.0.1:8500/ \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <token>' \
  -d '<json>'

# Start server
BROWSER_USE_TOKEN=<secret> python scripts/server.py --port 8500

# Stop
pkill -f 'server.py --port 8500'
```

Idle sessions auto-reaped after TTL (default: 1 hour).

## Agent Loop

1. **Launch** session (optionally with profile)
2. **Navigate** to target URL
3. **Snapshot** — get ARIA tree with refs (@e1, @e2, ...)
4. **Reason** — analyze tree, decide action(s)
5. **Act** — execute using refs
6. **Observe** — check result, re-snapshot if page changed
7. **Repeat** 4-6 until done
8. **Save** state and **close**

## Operations

### Launch
```json
{"op": "launch", "tier": 1, "url": "https://example.com", "profile": "my-identity"}
```
Returns: `{success, session_id, tier, url, title}`

### Snapshot
```json
{"op": "snapshot", "session_id": "<id>", "compact": true}
```
Returns: `{success, tree, refs, url, title, tab_count}`

ARIA tree format:
```
Page: https://x.com/home | Title: Home / X
Tab 1 of 1

- navigation "Main"
  - link "Home" @e1
  - link "Explore" @e2
- main
  - heading "Home" @e3 [level=1]
  - article
    - link "@username · 2h" @e4
    - text "Post content here..."
    - button "Like" @e5 [pressed=false]
    - button "Reply" @e6
```

### Action
```json
{"op": "action", "session_id": "<id>", "action": "click", "params": {"ref": "@e5"}}
```
Server uses ref_map from last snapshot. Override with `"ref_map": {...}` if needed.

Returns: `{success, extracted_content, error, page_changed, new_url}`

### Screenshot
```json
{"op": "screenshot", "session_id": "<id>", "full_page": false}
```
Returns: `{success, screenshot}` (base64 PNG)

### Save / Close / Status / Profile
```json
{"op": "save", "session_id": "<id>", "profile": "my-identity"}
{"op": "close", "session_id": "<id>", "save_profile": "my-identity"}
{"op": "status"}
{"op": "status", "session_id": "<id>"}
{"op": "profile", "action": "list"}
{"op": "profile", "action": "create", "name": "x-primary", "domain": "x.com"}
{"op": "profile", "action": "load", "name": "x-primary"}
{"op": "profile", "action": "delete", "name": "x-primary"}
```

## Actions

### Core
| Action | Params | Description |
|--------|--------|-------------|
| `navigate` | `{url}` | Go to URL |
| `click` | `{ref}` | Click element by ref |
| `fill` | `{ref, value}` | Atomic fill (clears first). For forms. |
| `type` | `{ref, text, delay_ms?}` | Character-by-character typing. For compose/search. |
| `scroll` | `{direction: up\|down, amount: int\|"page"}` | Scroll page |
| `snapshot` | `{compact?, max_depth?, cursor_interactive?}` | ARIA tree + refs |
| `screenshot` | `{full_page?}` | Base64 PNG |
| `wait` | `{ms}` | Explicit wait (max 30s) |
| `evaluate` | `{js}` | Execute JavaScript (requires BROWSER_USE_EVALUATE=1) |
| `done` | `{result, success?}` | Mark task complete |

### Extended
| Action | Params | Description |
|--------|--------|-------------|
| `press` | `{key, ref?}` | Keyboard press ("Enter", "Tab", "Escape") |
| `select` | `{ref, value}` | Dropdown selection |
| `go_back` | `{}` | Browser back |
| `cookies_get` | `{domain?}` | Get cookies |
| `cookies_set` | `{cookies: [...]}` | Set cookies |
| `tab_new` | `{url?}` | New tab |
| `tab_switch` | `{index}` | Switch tab (0-based) |
| `tab_close` | `{index}` | Close tab |

## Ref System

- Refs assigned sequentially: `@e1`, `@e2`, `@e3`, ...
- Reset on every new snapshot
- Server persists ref_map from each snapshot — actions use latest automatically
- If action fails with "ref not found", take a new snapshot
- Covers: buttons, links, inputs, checkboxes, headings, articles
- `[cursor-interactive]` = non-ARIA clickables detected by `cursor: pointer`

## Session Persistence

Profiles store identity state across sessions:
```
~/.browser-use/profiles/<name>/
├── cookies.json
├── storage.json    (localStorage + sessionStorage)
├── meta.json       (tier, domain, timestamps)
└── fingerprint.json (Tier 3: BrowserForge)
```

Use `"profile": "<name>"` in launch to restore, `"save_profile": "<name>"` in close to persist.

## Error Handling

| Error | Recoverability | Action |
|-------|---------------|--------|
| Element not found / ref invalid | RECOVERABLE | Re-snapshot, retry with new refs |
| Navigation timeout | RECOVERABLE | Retry navigate, check URL |
| Page crashed / context destroyed | NON_RECOVERABLE | Close session, relaunch |
| Anti-bot detection (403/captcha) | ESCALATABLE | Escalate tier or rotate proxy |
| Session not found / expired | NON_RECOVERABLE | Launch new session |
| Auth error (401/403 on server) | NON_RECOVERABLE | Check BROWSER_USE_TOKEN |
| Response truncated | RECOVERABLE | Use more targeted snapshot (compact=true, reduce max_depth) |

## Stealth Tiers

| Tier | Engine | When |
|------|--------|------|
| 1 | Playwright (Chromium) | General browsing, friendly sites |
| 2 | Patchright (patched Chromium) | Moderate anti-bot (no custom UA, stealth defaults) |
| 3 | Camoufox (Firefox C++ fork) | Turnstile, DataDome — with GeoIP + residential proxy |

## Architecture

| Component | File | Purpose |
|-----------|------|---------|
| Server | `scripts/server.py` | aiohttp HTTP server, auth, request routing, truncation |
| Agent | `scripts/agent.py` | stdin/stdout JSON interface (alternative to server) |
| Browser Engine | `scripts/browser_engine.py` | Multi-tier browser lifecycle (Playwright/Patchright/Camoufox), session management, idle GC |
| Actions | `scripts/actions.py` | Action dispatcher (10 core + 8 extended) |
| Snapshot | `scripts/snapshot.py` | ARIA tree parser, ref assignment |
| Session | `scripts/session.py` | Profile persistence (cookies/storage), path-safe naming |
| FSM | `scripts/agent_fsm.py` | State machine for agent loop |
| Compaction | `scripts/context_compaction.py` | LLM history summarization |
| Errors | `scripts/errors.py` | Error classification with AI-friendly transforms |
| Config | `scripts/config.py` | Settings (env vars + defaults) |
| Models | `scripts/models.py` | Pydantic v2 type definitions |

## Dependencies

**Core (all tiers):**
- Python 3.10+
- pydantic v2 (`pip install pydantic>=2.0`) — request/response models
- aiohttp (`pip install aiohttp`) — HTTP server

**Tier 1 — Playwright (Chromium):**
- playwright (`pip install playwright && playwright install chromium`)

**Tier 2 — Patchright (stealth Chromium):**
- patchright (`pip install patchright && patchright install chromium`)
- Patched Playwright fork with stealth defaults (no `navigator.webdriver` leak, isolated JS eval)

**Tier 3 — Camoufox (anti-detect Firefox):**
- camoufox (`pip install camoufox[geoip] && python -m camoufox fetch`)
- playwright (`pip install playwright`) — Camoufox uses Playwright Firefox protocol
- browserforge (installed with camoufox) — statistical fingerprint generation

All tiers auto-install their dependencies on first use if not already present.

## Do NOT Use For

- Simple URL scraping → use a dedicated scraper
- Direct API calls → use `curl` / HTTP
