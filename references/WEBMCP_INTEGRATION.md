# WebMCP Integration Reference

Browser-use skill integration with Chrome's WebMCP standard for structured tool interaction on web pages.

## Overview

WebMCP exposes structured tools on websites. The Origin Trial (Chrome 149-156) unifies the API under `document.modelContext` — `registerTool()` (publisher) plus `getTools()` + `executeTool()` (consumer), all on one object. Pre-OT builds (Chrome 146-148) used the split `navigator.modelContext` (publisher) + `navigator.modelContextTesting` (consumer); `navigator.modelContext` is **deprecated in Chrome 150 but not yet removed**, and `navigator.modelContextTesting` is absent from current docs. Instead of inferring page structure from ARIA snapshots, the agent reads explicit tool contracts with JSON schemas and calls them directly. browser-use uses a **dual-path adapter** (document → navigator → interceptor) so it works across both API generations.

**Status**: Origin Trial (Chrome 149.0.7827.102 → 156). Adapter implemented and **VERIFIED on Chrome Beta 150 (OT), 2026-06-14**: stub tests (`tests/test_webmcp_adapter.py`, 20/20 on bundled Chromium) + real-OT E2E (`tests/test_webmcp_ot_live.py`, 12/12 driving the shipped handlers through real `document.modelContext`). Confirmed empirically: `--enable-features=WebMCPTesting` exposes the OT API; the `getTools()` → `executeTool(toolObject, json)` round-trip works **headless**; the descriptor carries `inputSchema` (string), `annotations.{readOnlyHint,untrustedContentHint}`, `origin`, and a live `window` ref. Still deferred (lower-value / out-of-scope): cross-origin enumeration, live `toolchange` subscription, `AbortSignal` cancellation, and OT-token flow for third-party production sites.

## Requirements

| Component | Version | Notes |
|-----------|---------|-------|
| Chrome Beta/Canary/Dev | 149.0.7827+ for the OT API; 146-148 uses the navigator fallback | `sudo apt install google-chrome-beta` (Beta auto-updates toward 149) |
| Playwright | 1.56+ (we run 1.60) | Supports `channel` param for system Chrome |
| browser-use server | Current | `BROWSER_USE_WEBMCP=1 BROWSER_USE_CHROME_CHANNEL=chrome-beta` |

Local state (2026-06-14): Chrome Beta **150.0.7871.13** installed — the OT `document.modelContext` path is verified live (`tests/test_webmcp_ot_live.py`). (Stable here is 137 / repo candidate 149; Dev candidate 151.)

## Chrome Feature Flags

The Origin Trial API is gated behind a local-dev flag (production sites use an OT token, which we can't inject into third-party pages — so the flag is our only viable gate for arbitrary-site automation):

| Surface | Enable | Notes |
|---------|--------|-------|
| OT `document.modelContext` (149+) | `chrome://flags/#enable-webmcp-testing` → Enabled, relaunch | Local dev. The matching `--enable-features=<token>` for headless automation needs confirmation on a real 149+ build. |
| pre-OT `navigator.modelContextTesting` (146-148) | `--enable-features=WebMCPTesting` | What `browser_engine.py` currently passes; correct for 146-148. |

`browser_engine.py` appends `--enable-features=WebMCPTesting` when a Chrome channel/executable is set or `BROWSER_USE_WEBMCP=1`. **CONFIRMED (Chrome Beta 150, 2026-06-14):** `--enable-features=WebMCPTesting` exposes `document.modelContext` (`getTools`/`executeTool`/`registerTool`) on a secure context, and also keeps the legacy `navigator.modelContextTesting` available. `--enable-features=WebMCP` alone also exposes `document.modelContext` but omits `navigator.modelContextTesting`. So the existing flag is correct — no change needed.

## API Surface

### Unified API (`document.modelContext`) — Origin Trial (Chrome 149+)

Both publisher and consumer live on `document.modelContext`. browser-use is the **consumer**: it calls `getTools()`/`executeTool()` from `page.evaluate()`.

```webidl
partial interface Document { readonly attribute ModelContext modelContext; };

interface ModelContext : EventTarget {
  undefined registerTool(ModelContextTool tool, optional RegisterOptions options);   // {signal, exposedTo}
  Promise<sequence<RegisteredTool>> getTools(optional GetToolsOptions options);       // ASYNC; {fromOrigins}
  Promise<any?> executeTool(RegisteredTool tool, DOMString inputJson, optional ExecOptions options); // {signal}
  attribute EventHandler ontoolchange;   // "toolchange" fires when the tool set changes
};

dictionary ToolAnnotations {
  boolean readOnlyHint = false;          // tool only reads data → agent may skip confirmation
  boolean untrustedContentHint = false;  // output is UGC/external → agent must spotlight it
};

dictionary RegisteredTool {              // shape returned by getTools()
  DOMString name; DOMString description;
  DOMString inputSchema;                 // JSON STRING — parse before use
  ToolAnnotations annotations;           // { readOnlyHint, untrustedContentHint }
  USVString origin;                      // origin that registered the tool
  // also carries a live `window` ref → the object CANNOT cross the page.evaluate() boundary
};
```

**Consumer details (what the adapter calls via `page.evaluate()`):**
- `getTools()` is **async** (Promise) — the evaluate body is `async`. (Pre-OT `listTools()` was synchronous.)
- `executeTool(tool, inputJson)` takes the **tool OBJECT** from `getTools()`, not the name. Since the object can't be returned to Python and passed back, `webmcp_call` re-fetches `getTools()` **inside the page**, matches by `name` (+ `origin` when known), and executes in the same evaluate.
- `executeTool()` returns `null` when the tool triggers navigation (cross-document).
- `inputSchema` in `RegisteredTool` is a **string** — parse it.
- Tool removal is via `AbortSignal` (`registerTool(tool,{signal})` → `controller.abort()`); there is no `unregisterTool` in the OT API.
- Cross-origin: `getTools({fromOrigins:[...]})` + `registerTool(tool,{exposedTo:[...]})` + iframe `allow="tools"` (Permissions-Policy `tools`, default `self`). **DEFERRED** — not wired (low value for an out-of-page driver on third-party sites).
- Origin isolation: WebMCP is disabled in documents with `document.domain` enabled (`Origin-Agent-Cluster: ?0`). Our `add_init_script` injection does NOT trip this — risk is site headers, not our launch path.

### Pre-OT API (`navigator.modelContext` + `navigator.modelContextTesting`) — Chrome 146-148

The split API older code targeted: publisher `navigator.modelContext.registerTool()`/`unregisterTool()`; consumer `navigator.modelContextTesting.listTools()` (sync) + `executeTool(name, jsonString)`. `navigator.modelContext` is deprecated in Chrome 150 (not removed); `navigator.modelContextTesting` is absent from current docs. Retained only as the adapter's **fallback** for 146-148 builds.

### Declarative API (HTML form annotations)

Pages can annotate forms instead of using JavaScript:

```html
<form toolname="search" tooldescription="Search products" toolautosubmit>
  <input name="query" toolparamtitle="Search Query" toolparamdescription="What to search for">
  <select name="category" required>
    <option value="all">All</option>
    <option value="books">Books</option>
  </select>
  <button type="submit">Search</button>
</form>
```

Declarative tools appear in `listTools()` alongside imperative tools. `executeTool()` handles both — it auto-fills and submits forms for declarative tools.

**Form events:**
- `SubmitEvent.agentInvoked` — boolean, true when agent triggered submission
- `SubmitEvent.respondWith(Promise)` — return structured result to agent
- `window 'toolactivated'` — fires when agent pre-fills form
- `window 'toolcancel'` — fires when agent cancels or form resets

**CSS pseudo-classes:**
- `:tool-form-active` — on form element during agent interaction
- `:tool-submit-active` — on submit button during agent interaction

## Integration Architecture

### Dual-path execution

The discover/call JS live as module-level constants in `actions.py` (`_WEBMCP_DISCOVER_JS`, `_WEBMCP_CALL_JS`) so tests exercise the exact shipped strings. Both try paths newest-first:

```
webmcp_discover / webmcp_call
  |
  +-- 1st: document.modelContext (Chrome 149+ Origin Trial)
  |        getTools() -> async [{name, description, inputSchema(str), annotations, origin, window}]
  |        executeTool(TOOL_OBJECT, jsonString) -> Promise<any|null>
  |        (call path re-resolves the tool object in-page by name + origin)
  |
  +-- 2nd: navigator.modelContextTesting (Chrome 146-148)
  |        listTools() (sync) / executeTool(name, jsonString)
  |
  +-- 3rd: Init script interceptor (fallback)
           Monkey-patches registerTool on document.modelContext OR navigator.modelContext
           Scans <form toolname> for declarative tools
           Calls tool._ref.execute(args, client) (gated) or fills+submits form
```

Newest API wins; lower paths are fallbacks for older builds / pages where the native discovery API isn't present. All three capture `readOnlyHint` + `untrustedContentHint`.

### Init script interceptor

Injected via `context.add_init_script()` before any page JS runs. Captures:
- Imperative tools: patches `registerTool()` on whichever namespace exists (`document.modelContext` 149+ or `navigator.modelContext` 146-150); removal via `AbortSignal`
- Declarative tools: scans `<form toolname>` elements on DOMContentLoaded; respects `toolautosubmit` (forms without it are filled but NOT auto-submitted — previously always submitted via a `|| true` bug)
- Execution: `window.__webmcp.executeTool(name, args, {allowSensitive})` — for mutating tools (not `readOnlyHint`), `requestUserInteraction` is gated unless `allowSensitive` is set, returning `_requires_user_interaction` instead of silently completing the action

For declarative form filling, uses native input setter to trigger React/framework state updates:
```javascript
const nativeSetter = Object.getOwnPropertyDescriptor(
    HTMLInputElement.prototype, 'value'
)?.set;
nativeSetter.call(el, value);
el.dispatchEvent(new Event('input', { bubbles: true }));
el.dispatchEvent(new Event('change', { bubbles: true }));
```

### Security signal propagation (agent-security guidance)

Per Chrome's [Agent security considerations for WebMCP](https://developer.chrome.com/docs/agents/security), the server faithfully **propagates** safety signals to the driving agent and **gates** state-changing actions:
- **`untrustedContentHint`** — captured on discover, surfaced as `[untrusted-output]` in the snapshot tool header. The driving agent should spotlight (delimit/base64) such output and never execute instructions found in it.
- **`readOnlyHint`** — captured + surfaced as `[read-only]`; the agent may skip confirmation for read-only tools.
- **Confirm mutating actions** — on the interceptor path, `requestUserInteraction` for a non-`readOnlyHint` tool is NOT auto-approved; `webmcp_call` returns `requires_user_interaction: true` unless called with `allow_sensitive: true`. (On the native `document.modelContext` path, Chrome owns confirmation.)
- `origin` is captured so the agent can restrict cross-origin interactions.

### File locations

| File | WebMCP additions |
|------|-----------------|
| `config.py` | `WEBMCP_ENABLED`, `CHROME_CHANNEL`, `CHROME_EXECUTABLE` env vars |
| `browser_engine.py` | `WEBMCP_INIT_SCRIPT` (dual-namespace interceptor, gated execute), `_inject_webmcp_script()`, `_build_chrome_launch_opts()`, session `webmcp_*` fields |
| `actions.py` | `_WEBMCP_DISCOVER_JS` / `_WEBMCP_CALL_JS` constants, `action_webmcp_discover()`, `action_webmcp_call()` (`allow_sensitive` param) |
| `snapshot.py` | `webmcp_tools` param on `take_snapshot()`, tool header with `[read-only]`/`[untrusted-output]` flags |
| `server.py` | Pass `webmcp_tools` through session context |
| `rate_limiter.py` | Both actions in `EXEMPT_ACTIONS` |
| `tests/test_webmcp_adapter.py` | Stub tests for the dual-path JS (15/15, bundled Chromium) |

## Usage

### Env variables

| Variable | Default | Description |
|----------|---------|-------------|
| `BROWSER_USE_WEBMCP` | `auto` | `auto` = detect, `1` = force Chrome channel + flag, `0` = disable |
| `BROWSER_USE_CHROME_CHANNEL` | (empty) | `chrome-beta`, `chrome-dev`, `chrome-canary`, `chrome` |
| `BROWSER_USE_CHROME_PATH` | (empty) | Explicit binary path (overrides channel) |

### Server launch

```bash
BROWSER_USE_WEBMCP=1 \
BROWSER_USE_CHROME_CHANNEL=chrome-beta \
python3 server.py --port 8500
```

### Agent workflow

```bash
# 1. Launch session (Chrome Beta auto-selected when CHROME_CHANNEL is set)
curl -s -X POST http://127.0.0.1:8500/ -H 'Content-Type: application/json' \
  -d '{"op":"launch","tier":1,"url":"https://example.com"}'

# 2. Discover WebMCP tools
curl -s -X POST http://127.0.0.1:8500/ -H 'Content-Type: application/json' \
  -d '{"op":"action","session_id":"<id>","action":"webmcp_discover","params":{}}'

# 3. Call a tool with structured args
curl -s -X POST http://127.0.0.1:8500/ -H 'Content-Type: application/json' \
  -d '{"op":"action","session_id":"<id>","action":"webmcp_call","params":{
    "tool":"searchFlights",
    "args":{"origin":"LON","destination":"NYC","tripType":"round-trip",
            "outboundDate":"2026-06-10","inboundDate":"2026-06-17","passengers":2}
  }}'

# 4. Snapshot (WebMCP tools appear in header)
curl -s -X POST http://127.0.0.1:8500/ -H 'Content-Type: application/json' \
  -d '{"op":"snapshot","session_id":"<id>","compact":true}'
```

### Decision tree: WebMCP vs ARIA

```
Navigate to page
  |
  +-- webmcp_discover
  |     |
  |     +-- tools found?
  |           |
  |           YES --> Does the task map to a registered tool?
  |           |         |
  |           |         YES --> webmcp_call (1 request)
  |           |         NO  --> ARIA snapshot + ref-based actions
  |           |
  |           NO --> ARIA snapshot + ref-based actions (standard path)
  |
  +-- After navigation to new page --> re-run webmcp_discover
```

### Snapshot header with WebMCP

When tools are discovered, snapshots include:
```
Page: https://example.com | Title: Example
Tab 1 of 1

WebMCP Tools:
  [imperative] searchFlights: Searches for flights with the given parameters.
  [declarative] contactForm: Submit a contact request.

- navigation "Main"
  - link "Home" @e1
  ...
```

## Comparison: WebMCP vs ARIA

### Flight search (verified E2E)

**ARIA path (6+ requests):**
```
snapshot → identify @e1-@e6 → fill @e1 "LON" → fill @e2 "NYC" →
fill @e3 "round-trip" → fill @e4 "2026-06-10" → fill @e5 "2026-06-17" →
fill @e6 "2" → click @e7 → snapshot (verify results)
```

**WebMCP path (2 requests):**
```
webmcp_discover → webmcp_call searchFlights {origin:"LON", ...}
```

### What WebMCP eliminates

| Problem | ARIA approach | WebMCP approach |
|---------|--------------|-----------------|
| Field identification | Infer from labels, guess order | Explicit `inputSchema` with names |
| Input format | Guess (date format, IATA codes) | Schema declares `format`, `pattern`, `enum` |
| Required fields | Trial and error | `required` array in schema |
| Form submission | Find and click submit button | Handled by `executeTool()` |
| Result verification | Re-snapshot, parse ARIA tree | Structured return value |
| State changes | Constant re-snapshotting | `registerTool()`/`unregisterTool()` updates tool set |

### What WebMCP does NOT replace

- Anti-bot bypass (stealth tiers, humanization, fingerprinting)
- Session persistence (cookies, storage, profiles)
- Non-form interactions (scrolling, reading, navigation, screenshots)
- Sites without WebMCP adoption
- Visual verification

## Tier compatibility

| Tier | Engine | WebMCP support |
|------|--------|---------------|
| 1 | Playwright + Chrome Beta | Yes (via channel/executable) |
| 2 | Patchright + Chrome Beta | Yes (via channel/executable) |
| 3 | Camoufox (Firefox) | No — Firefox doesn't implement WebMCP |

Tier 3 incompatibility is acceptable — sites requiring Camoufox-level anti-bot won't implement WebMCP.

## Error handling

| Scenario | Behavior |
|----------|----------|
| No Chrome 147+ | `webmcp_discover` returns `available: false` — ARIA path used |
| WebMCP flag not set | Same — `navigator.modelContextTesting` undefined |
| Tool not found | `webmcp_call` returns error with available tool names |
| Tool triggers navigation | 15s timeout catches hanging evaluate; checks URL change |
| Cross-document result | Use `getCrossDocumentScriptToolResult()` (not yet implemented in browser-use) |
| Tool execution error | Structured error returned via `respondWith()` or exception |
| Page changes tools | Re-run `webmcp_discover` after navigation |
| No tools on page | `webmcp_available=true` (API exists) + `tool_count=0` — use ARIA path |
| Declarative tool hangs | Native `executeTool()` waits for `respondWith()` — 15s timeout catches this |

## Known limitations

1. **Chrome-only** — WebMCP is a Chrome proposal, no Firefox/Safari support (Tier 3 Camoufox excluded)
2. **Origin Trial, not stable** — API may still change through Chrome 156; near-zero site adoption
3. **Real-OT conformance VERIFIED** (Chrome Beta 150, 2026-06-14) — `tests/test_webmcp_ot_live.py` drives the shipped handlers through real `document.modelContext` (discover/call/origin-fail-closed), 12/12.
4. **Headless — CONFIRMED working.** The `getTools()`/`executeTool()` round-trip runs under HeadlessChrome/150 (no UI). The docs' "no headless state" means "no browser context at all" (a pure API agent), NOT headless Chrome with a real page.
5. **`toolchange` event** — not subscribed; manual re-discover after navigation still required
6. **AbortSignal cancellation** — `executeTool({signal})` not exposed via the browser-use API (15s timeout used instead)
7. **Cross-origin** — `fromOrigins`/`exposedTo`/`allow="tools"` not wired
8. **`--enable-features` token** — RESOLVED: `WebMCPTesting` confirmed to enable the OT `document.modelContext` shape on Chrome 150 (see Feature Flags)
9. **Declarative tool execution** — native `executeTool()` for declarative forms can hang if the page's `respondWith()` never resolves; the 15s timeout in `webmcp_call` prevents server hangs
10. **`available` on all pages** — `document.modelContext`/`navigator.modelContext` exists browser-wide; check `tool_count` (not `available`) to know if tools are registered

## External references

| Resource | URL |
|----------|-----|
| W3C WebMCP Spec | https://webmachinelearning.github.io/webmcp |
| Spec repo | https://github.com/webmachinelearning/webmcp |
| Chrome early preview doc | https://developer.chrome.com/docs/ai/webmcp |
| Inspector extension | https://github.com/beaufortfrancois/model-context-tool-inspector |
| Travel demo (imperative) | https://googlechromelabs.github.io/webmcp-tools/demos/react-flightsearch/ |
| Bistro demo (declarative) | https://googlechromelabs.github.io/webmcp-tools/demos/french-bistro/ |
| Demo source / tools repo | https://github.com/GoogleChromeLabs/webmcp-tools |
| MCP-B polyfill | https://github.com/MiguelsPizza/WebMCP |
| MCP-B docs | https://docs.mcp-b.ai/ |
| Chromium bug tracker | https://crbug.com/new?component=2021259 |
| Dev Preview Group | Linked from Chrome doc |

## Test suite

### Adapter stub tests — `tests/test_webmcp_adapter.py` (current, 20/20)

Exercises the exact shipped JS (`_WEBMCP_DISCOVER_JS`/`_WEBMCP_CALL_JS` + `WEBMCP_INIT_SCRIPT`) against a fake `document.modelContext`/`navigator.modelContextTesting`/`window.__webmcp` injected into bundled Chromium. No server, no Chrome 149 — validates the document-path branch, in-page tool-object resolution, `untrustedContentHint` capture, navigator fallback, `requestUserInteraction` gating, origin fail-closed, and strict `allow_sensitive`. Run: `python3 tests/test_webmcp_adapter.py` (conda base).

### Real-OT E2E — `tests/test_webmcp_ot_live.py` (12/12, Chrome Beta 150, 2026-06-14)

Drives the SHIPPED `action_webmcp_discover`/`action_webmcp_call` against a REAL Chrome Beta (>=149) running the OT (`--enable-features=WebMCPTesting`): registers a tool via real `document.modelContext.registerTool`, then verifies document-path discovery (`source==document`), `untrustedContentHint`+`origin` capture from the real API, `inputSchema` string→object, the real `executeTool` round-trip, and origin fail-closed. Skips cleanly (exit 0) if Chrome < 149. Run: `python3 tests/test_webmcp_ot_live.py` (conda base).

### Live integration — `tests/test_webmcp.py` (PENDING real OT)

The original 48-test live suite (below) targeted the pre-OT navigator API on Chrome Beta 146 (validated 2026-02-17). It is **not currently green-claimable**: the OT `document.modelContext` path needs a 149+ channel, and the GoogleChromeLabs demo sites may have migrated to the new API. Re-validate when a 149+ channel is available.

Run with:
```bash
BROWSER_USE_WEBMCP=1 BROWSER_USE_CHROME_CHANNEL=chrome-beta python3 server.py --port 8500
# In another terminal:
python3 tests/test_webmcp.py
```

### Historical results (2026-02-17, Chrome Beta 146, navigator API): 48/48 passed

| Test | Scope | Status |
|------|-------|--------|
| 1. Imperative tools | React flight search: launch, discover, schema, call, snapshot header | 15/15 |
| 2. Declarative tools | French Bistro: launch, discover, schema extraction, call (graceful timeout) | 7/7 |
| 3. Graceful degradation | example.com: discover returns 0 tools, no WebMCP header, ARIA works | 6/6 |
| 4. Error handling | Unknown tool, missing param, call before discover | 6/6 |
| 5. Session persistence | Re-discover same count, call after re-discover, header persists | 4/4 |
| 6. Re-discover after nav | WebMCP -> non-WebMCP -> WebMCP navigation cycle | 6/6 |
| 7. Snapshot op header | Top-level snapshot op renders WebMCP header with `[type] name: desc` format | 4/4 |

### Validated E2E flows

**Imperative (searchFlights):**
```
1. launch tier=1, chrome-beta channel        -> session created
2. webmcp_discover                           -> native API, 1 tool, full JSON schema
3. webmcp_call searchFlights LON->NYC        -> page navigated, flight results rendered
4. snapshot                                  -> "WebMCP Tools:" header + ARIA tree
5. close                                     -> clean teardown
```

**Declarative (book_table_le_petit_bistro):**
```
1. launch tier=1, french-bistro demo         -> session created
2. webmcp_discover                           -> native API, 1 tool, 7-property schema
3. Schema extracted: name, phone, date, time, guests (enum), seating (enum), requests
4. webmcp_call                               -> graceful 15s timeout (Chrome preview limitation)
```

**Known: declarative `executeTool()` hangs** — Chrome's native API waits for `respondWith()` callback. Discovery and schema work; execution falls back to error after 15s timeout. This is a Chrome early preview limitation, not a browser-use bug.
