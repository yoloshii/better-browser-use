# better-browser-use

HTTP-based browser automation server (port 8500) with ARIA snapshot navigation, 3 stealth tiers, 35 actions, WebMCP support, and human-like behavior simulation.

## Running

```bash
# Start server
python scripts/server.py --port 8500

# With auth (your own token, not an official browser-use API key)
BROWSER_USE_TOKEN=<secret> python scripts/server.py --port 8500

# Health check
curl http://127.0.0.1:8500/health
```

## Core Loop

1. `launch` → get `session_id`
2. `snapshot` → get ARIA tree with `@e1`, `@e2` refs
3. Decide action based on tree content
4. Execute action using refs
5. Re-snapshot if `page_changed: true`
6. Repeat until task is done
7. `close` session

## Decision Tree

```
Need to interact with a form on a WebMCP-enabled page?
  YES → webmcp_discover → webmcp_call (2 requests vs 6+)
  NO ↓

Know exact element to interact with?
  YES (have ref) → click/fill/type/select with ref
  NO ↓

Need to find an element?
  → search_page (text search) or find_elements (by role/text in refs)
  → Then snapshot to get refs

Need full page content?
  → extract (returns Markdown, expensive)

Element has no ARIA role?
  → click_coordinate as last resort
```

## Response Signals

| Signal | Meaning | Action |
|--------|---------|--------|
| `page_changed: true` | Navigation occurred | Re-snapshot to get new refs |
| `blocked: true` | Anti-bot detected | Escalate tier or try different approach |
| `loop_warning` | Repetitive action detected | Change approach (see warning level) |
| `new_element_count` | Elements appeared since last snapshot | Check `*`-prefixed items in tree |
| `changed_element_count` | Elements changed since last snapshot | Check `~`-prefixed items in tree |
| `removed_element_count` | Elements removed since last snapshot | Check `[removed since last snapshot]` section |
| `code: RATE_LIMITED` | Too many actions on domain | Wait `wait_seconds`, then retry |

## Common Patterns

**Login flow:**
```
launch → navigate to login page → snapshot →
fill username → fill password → click submit →
snapshot (verify logged in) → save_state
```

**Data extraction:**
```
launch → navigate → search_page "target text" →
snapshot → extract (if ARIA tree lacks detail) → done
```

**Form with WebMCP:**
```
launch → navigate → webmcp_discover →
webmcp_call toolName {field1: "val", field2: "val"} → done
```

## Ref Rules

- Refs like `@e1` are assigned per-snapshot and reset each time
- Always use refs from the MOST RECENT snapshot
- If "ref not found" → take a new snapshot
- `[cursor-interactive]` refs are non-ARIA clickable elements detected by CSS

## Key Files

| File | Purpose |
|------|---------|
| `scripts/server.py` | HTTP server, routing, loop detection, popup/download surfacing |
| `scripts/browser_engine.py` | Browser lifecycle, WebMCP init script, popup/download handlers |
| `scripts/actions.py` | 35 action handlers with humanization |
| `scripts/snapshot.py` | ARIA tree parser, ref assignment, snapshot diff (new/changed/removed) |
| `scripts/models.py` | Pydantic models, PageFingerprint, ActionLoopDetector |
| `scripts/config.py` | All settings and env vars |
| `scripts/behavior.py` | Bezier mouse, Gaussian typing, eased scrolling |
| `scripts/detection.py` | Anti-bot detection |
| `scripts/rate_limiter.py` | Per-domain sliding window rate limiter |

## Architecture

- All requests: `POST /` with JSON `{"op": "...", ...}`
- Sessions are in-memory dicts in `browser_engine._sessions`
- Ref maps persist between requests per session
- Loop detection is advisory (warnings in response, never blocks)
- WebMCP tools discovered per-page, stored in session dict

## Rate Limiting

Social media sites have lower limits (4-6/min). Read-only actions (snapshot, screenshot, cookies_get, cookies_export, search_page, find_elements, extract, get_downloads, get_value, get_attributes, get_bbox, rotate_fingerprint) are exempt.

## Proxy Configuration

Tier 2 and 3 support optional proxy for stealth browsing and geo-targeting. You must provide your own proxy credentials via environment variables:

```bash
PROXY_SERVER=http://your-proxy-host:port   # SOCKS5 or HTTP proxy URL
PROXY_USERNAME=your_username
PROXY_PASSWORD=your_password
```

- No proxy is configured by default — all tiers work without one
- Tier 3 (Camoufox) auto-detects timezone/locale from proxy exit IP when `geoip=True`
- For anti-bot sites requiring residential IPs, use a residential/ISP proxy provider
- Set `BROWSER_USE_GEO` to match browser locale to your proxy exit location

## Error Recovery

- **RECOVERABLE**: Re-snapshot and retry with new refs
- **ESCALATABLE**: Try higher tier, different URL, or rotate proxy
- **NON_RECOVERABLE**: Close session and relaunch

## Testing Changes

```bash
# AST check (all files parse)
python -c "import ast, pathlib; [ast.parse(f.read_text()) for f in pathlib.Path('scripts').glob('*.py')]"

# Start server + test
python scripts/server.py --port 8500 &
curl -s -X POST http://127.0.0.1:8500/ -H 'Content-Type: application/json' \
  -d '{"op":"launch","tier":1,"url":"https://example.com"}'
```

## Dependencies

Core: `aiohttp`, `pydantic>=2.0`, `markdownify`, `pyee>=13,<14`, `python-dotenv`
Tier 1: `playwright>=1.51,<1.56` + chromium (avoid 1.56+ WSL2 regression)
Tier 2: `patchright` + chromium (requires pyee>=13)
Tier 3: `camoufox[geoip]` + `playwright`

Install order: pyee → playwright → patchright → aiohttp/pydantic/markdownify
