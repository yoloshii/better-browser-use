# WebMCP Integration Reference

Browser-use skill integration with Chrome's WebMCP standard for structured tool interaction on web pages.

## Overview

WebMCP exposes structured tools on websites via `navigator.modelContext` (publisher) and `navigator.modelContextTesting` (consumer). Instead of inferring page structure from ARIA snapshots, the agent reads explicit tool contracts with JSON schemas and calls them directly.

**Status**: Early preview, Chrome 147+ behind flag, expiry milestone 155.

## Requirements

| Component | Version | Notes |
|-----------|---------|-------|
| Chrome Beta/Canary/Dev | 147.0.7721.0+ | `sudo apt install google-chrome-beta` |
| Playwright | 1.56+ | Supports `channel` param for system Chrome |
| browser-use server | Current | `BROWSER_USE_WEBMCP=1 BROWSER_USE_CHROME_CHANNEL=chrome-beta` |

WSL2 verified: Chrome Beta 146.0.7680.0 headless works.

## Chrome Feature Flags

Two separate runtime features gated independently:

| Flag | Enables | CLI arg |
|------|---------|---------|
| `WebMCP` | `navigator.modelContext` (publisher API) | `--enable-features=WebMCP` |
| `WebMCPTesting` | `navigator.modelContextTesting` (consumer API) + implies WebMCP | `--enable-features=WebMCPTesting` |

`--enable-experimental-web-platform-features` enables WebMCP but NOT WebMCPTesting.

**Always use `WebMCPTesting`** — it enables both publisher and consumer APIs.

Source: `third_party/blink/renderer/platform/runtime_enabled_features.json5`
```json5
{ name: "WebMCP", implied_by: ["WebMCPTesting"], status: "experimental" },
{ name: "WebMCPTesting", status: "experimental" },
```

## API Surface

### Publisher API (`navigator.modelContext`)

Pages use this to declare tools. We don't call this directly — pages do.

Chrome 147 removed `provideContext()` and `clearContext()` (spec PR #132, issue #101).
The API is now purely additive — `registerTool()` / `unregisterTool()`.
`unregisterTool()` design under revision (issue #130) — may change to require original dict.

```webidl
interface ModelContext {
  undefined registerTool(ModelContextTool tool);   // throws InvalidStateError on duplicate name
  undefined unregisterTool(DOMString name);
};

dictionary ModelContextTool {
  required DOMString name;
  required DOMString description;
  object inputSchema;                 // JSON Schema
  required ToolExecuteCallback execute;
  ToolAnnotations annotations;
};

dictionary ToolAnnotations {
  boolean readOnlyHint = false;       // hint: tool only reads data
};

callback ToolExecuteCallback = Promise<any> (object input, ModelContextClient client);

interface ModelContextClient {
  Promise<any> requestUserInteraction(UserInteractionCallback callback);
};
```

### Consumer API (`navigator.modelContextTesting`)

We call this from `page.evaluate()` to discover and execute tools.

```webidl
interface ModelContextTesting {
  sequence<RegisteredTool> listTools();                        // synchronous
  Promise<DOMString?> executeTool(                             // async
      DOMString tool_name,
      DOMString input_arguments,                               // JSON string, NOT object
      optional ExecuteToolOptions options = {}                  // { signal: AbortSignal }
  );
  undefined registerToolsChangedCallback(ToolsChangedCallback callback);
  Promise<DOMString> getCrossDocumentScriptToolResult();       // after navigation
};

dictionary RegisteredTool {
  required DOMString name;
  required DOMString description;
  DOMString inputSchema;              // JSON string, parse before use
};
```

**Critical details:**
- `listTools()` is synchronous — returns immediately
- `executeTool()` takes a **JSON string** as second arg, not an object
- `executeTool()` returns `null` when tool triggers navigation (cross-document)
- `inputSchema` in `RegisteredTool` is a **string** — must `JSON.parse()` it
- Accessible from `page.evaluate()` — no extension privileges needed

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

```
webmcp_discover / webmcp_call
  |
  +-- 1st: navigator.modelContextTesting (native Chrome API)
  |        listTools() -> [{name, description, inputSchema}]
  |        executeTool(name, JSON.stringify(args)) -> Promise<string|null>
  |
  +-- 2nd: Init script interceptor (fallback)
           Monkey-patches registerTool/unregisterTool
           Scans <form toolname> for declarative tools
           Calls tool._ref.execute(args, mockClient) or fills+submits form
```

Native API is preferred — interceptor exists for edge cases where `modelContextTesting` isn't available (e.g., `WebMCP` flag without `WebMCPTesting`).

### Init script interceptor

Injected via `context.add_init_script()` before any page JS runs. Captures:
- Imperative tools: patches `registerTool()`, `unregisterTool()` (Chrome 147+ — no provideContext/clearContext)
- Declarative tools: scans `<form toolname>` elements on DOMContentLoaded
- Execution: `window.__webmcp.executeTool(name, args)` handles both paths, passes mockClient for `requestUserInteraction`

For declarative form filling, uses native input setter to trigger React/framework state updates:
```javascript
const nativeSetter = Object.getOwnPropertyDescriptor(
    HTMLInputElement.prototype, 'value'
)?.set;
nativeSetter.call(el, value);
el.dispatchEvent(new Event('input', { bubbles: true }));
el.dispatchEvent(new Event('change', { bubbles: true }));
```

### File locations

| File | WebMCP additions |
|------|-----------------|
| `config.py` | `WEBMCP_ENABLED`, `CHROME_CHANNEL`, `CHROME_EXECUTABLE` env vars |
| `browser_engine.py` | `WEBMCP_INIT_SCRIPT`, `_inject_webmcp_script()`, `_build_chrome_launch_opts()`, session `webmcp_*` fields |
| `actions.py` | `action_webmcp_discover()`, `action_webmcp_call()` |
| `snapshot.py` | `webmcp_tools` param on `take_snapshot()`, tool header in output |
| `server.py` | Pass `webmcp_tools` through session context |
| `rate_limiter.py` | Both actions in `EXEMPT_ACTIONS` |

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

1. **Chrome-only** — WebMCP is a Chrome proposal, no Firefox/Safari support
2. **Early preview** — API may change, flag expires at milestone 155
3. **Adoption** — very few sites implement WebMCP currently
4. **Cross-document results** — `getCrossDocumentScriptToolResult()` not wired into `webmcp_call` yet
5. **Tool change detection** — `registerToolsChangedCallback` not used; manual re-discover required
6. **AbortSignal** — `ExecuteToolOptions.signal` not exposed via browser-use API
7. **Declarative tool execution** — Chrome's native `executeTool()` for declarative forms hangs when the page's `respondWith()` callback doesn't resolve. Discovery and schema extraction work correctly; execution requires the page to properly implement `SubmitEvent.respondWith()`. The 15s timeout in `webmcp_call` prevents server hangs.
8. **`webmcp_available` on all pages** — With Chrome Beta + `WebMCPTesting` flag, `navigator.modelContext` exists on all pages. Check `tool_count` (not `available`) to determine if WebMCP tools are registered.

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

## Test suite (2026-02-17)

Located at `tests/test_webmcp.py`. Run with:
```bash
BROWSER_USE_WEBMCP=1 BROWSER_USE_CHROME_CHANNEL=chrome-beta python3 server.py --port 8500
# In another terminal:
python3 tests/test_webmcp.py
```

### Results: 48/48 passed

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
