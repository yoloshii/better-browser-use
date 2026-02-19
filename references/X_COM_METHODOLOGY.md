# X.com Browser Methodology

*Proven: 15-20s for 5 most recent posts, bookmarks, or search.*

---

## Core Principles

### Connection: CDP Debug Port Only
- Chrome debug-port profile local (CDP 9222)
- No API, no browser extension
- No bird CLI

### Tab Management: Cached Reuse
- Per-mode cached tab IDs: `profile`, `bookmarks`, `search`
- If mode tab missing: open once and cache — never open new tabs per run
- Warm dedicated search tab + one reusable profile tab
- Keep user on web UI: avoid visible tab stealing

### Navigation: Direct URL Only
- Direct URL navigation (no typing/clicking in X UI)
- No search box interaction — navigate to `x.com/search?q=...` directly
- No profile menu clicking — navigate to `x.com/<username>` directly

### Extraction: Single-Pass Evaluate
- Run single-pass `evaluate` extraction first (no snapshots/click loops)
- No ARIA snapshot overhead for data extraction
- Use snapshots only when interaction is needed (not data retrieval)

### Selectors: Stable X.com Targets
```
article                              # Tweet container
a[href*="/status/"]                  # Tweet permalink
[data-testid="tweetText"]           # Tweet text body
[data-testid="User-Name"]           # Username display
time[datetime]                       # Timestamp
```

### DOM Scoping
- Scope queries to `main` first (not whole document)
- Only fall back to `document` when `main` yields nothing

### Feed Order
- Keep top→down (DOM order = feed order)
- No timestamp resort — trust X's rendering order

### Output: Lean Default
- Default: `url + text_200` (URL + first 200 chars)
- Richer fields only when explicitly requested
- If `tweetText` missing: article-text fallback, strip UI junk / trailing "Show more"

### Scrolling: Zero-First
- Zero-scroll first pass — extract what's visible
- Micro-scroll only if needed to hit target count
- No infinite scroll harvesting

### Timing: Adaptive Short Waits
- 250-350ms waits with bounded loops
- No fixed long sleeps
- Adaptive: shorter if content loads fast, bail if not

### Error Handling: Fail Fast
- Quick no-data signal → hard bail
- Return partial results + reason instead of long hangs
- Never hang waiting for content that won't load

### Caching: Short Same-Query
- 30-60s cache for immediate reruns
- Prevents duplicate fetches when iterating
- Optional — disable for real-time needs

---

## Performance Targets

| Operation | Target | Method |
|-----------|--------|--------|
| 5 recent posts | 15-20s | Direct profile URL + evaluate |
| Bookmarks | 15-20s | Direct bookmarks URL + evaluate |
| Search | 15-20s | Direct search URL + evaluate |

---

## Integration Notes

### For browser-use skill
- Use `evaluate` action with the selectors above instead of snapshot→click loops
- `search_page` for quick text matching without full snapshot overhead
- `extract` for full page markdown when evaluate selectors miss dynamic content
- `wait` with `text` or `selector` param to detect content load before extracting
- Launch with `profile: "x-primary"` for warm session with cached auth
- `cookies_export`/`cookies_import` as lightweight alternative to full profile save/restore
- Tab management maps to `tab_new`/`tab_switch` actions

---

## Anti-Patterns (Avoid)

| Pattern | Problem |
|---------|---------|
| New tab per run | Tab explosion, visible stealing |
| Search box interaction | Slow, detectable, fragile selectors |
| Full ARIA snapshot for data | Overhead — evaluate is 10x faster |
| Timestamp resorting | Breaks feed context, wastes compute |
| Long fixed waits | Wasted time on fast loads, still fails on slow |
| Infinite scroll harvesting | Rate limiting, detection, memory |
| Full document scope | Picks up sidebar/nav noise |
