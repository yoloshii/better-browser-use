# Account Safety for Agentic Browser Automation

*2025-2026 detection landscape. Behavioral analysis catches bots that pass every fingerprint test.*

---

## Detection Stack (All Active on Authenticated Sessions)

### Layer 1: TLS/JA4 Fingerprinting
- Cipher suites, TLS extensions, protocol versions hashed into JA3/JA4 signatures
- Python HTTP libraries (requests, httpx) produce instantly recognizable bot signatures
- Real browser engines (Playwright, Patchright, Camoufox) produce genuine TLS — non-issue for this framework

### Layer 2: Browser Environment
- Canvas, WebGL, AudioContext, font enumeration, JS engine characteristics
- **CDP detection (2025+)**: Playwright/Puppeteer `Runtime.enable` triggers detectable side effects
- Patchright and Camoufox minimize CDP artifacts; vanilla Playwright does not
- Cross-correlation catches mismatches (canvas vs claimed GPU, fonts vs claimed OS)

### Layer 3: Behavioral Biometrics (87% accuracy)
- Mouse trajectory ML: detects linear movement, mathematically consistent acceleration
- Timing analysis: 0% variance in action intervals vs human 20-40% variance
- Event order: real clicks fire `mousemove → mousedown → mouseup → click` in sequence
- Scroll patterns: real users scroll in bursts with pauses, not continuous smooth motion
- Navigation flow: sequential access is a dead giveaway; real users backtrack

### Layer 4: IP Reputation
- Datacenter IPs: 20-40% success rate
- Residential/ISP proxies: 85-95% success rate
- 84% of sites cannot detect residential proxy abuse (F5 2025)
- Mid-session IP changes are suspicious
- ASN analysis identifies hosting providers and known proxy services

### Layer 5: Authenticated Session Signals
- **Behavioral baseline**: platforms build per-user profiles; sudden pattern changes are flagged
- **Velocity**: high activity relative to account history
- **Fingerprint consistency**: changing fingerprints between sessions for same account = instant flag
- **Action-to-engagement ratio**: one-way broadcast patterns vs genuine interaction
- **Content quality**: low acceptance rates on connection requests, generic messages

---

## Platform Thresholds

### LinkedIn (Detection: HIGH)
| Metric | Free | Premium | Sales Nav | Recruiter |
|--------|------|---------|-----------|-----------|
| Profile visits/day | 100 | 250 | 500 | 600 |
| Messages/day | 50 | 75 | 250 | 300 |
| Connection requests/day | 3% of total connections | same | same | same |

- Safe starting point: 25 actions/day, increase 10-20% per week
- Never exceed 20 connection requests/hour
- Keep unaccepted request backlog low (withdraw stale requests)
- Build 300+ connections manually before any automation
- **Warning progression**: dialog → ID verification → 24-48h lock → permanent ban

### Meta — Facebook/Instagram (Detection: VERY HIGH)
- ~50-100 interactions/hour triggers detection
- Dec 2025 new enforcement system caused massive ban wave
- ~30% of 2025 disabled accounts were false positives
- Deep device fingerprinting adopted from TikTok patterns
- Interaction entropy below normal human range is flagged
- **Often instant ban with poor/nonexistent appeal process**
- **Recommendation: avoid for valuable accounts**

### X/Twitter (Detection: MODERATE-HIGH)
- ~10-15 retweets/hour safe ceiling
- AudioContext fingerprint specifically checked
- Mass follow/unfollow cycles detected
- Identical/near-identical DMs detected
- Graduated warning system

### Reddit (Detection: HIGH for account linking)
- 5-10 comments/day per account for automation
- **#1 detection vector: shared IPs across accounts**
- Shared fingerprints, session cookie leaks between accounts
- Content similarity analysis (writing style, vocabulary)
- Accounts need age + karma before automation is safe

### Google (Detection: EXTREMELY HIGH)
- ~100-200 queries/hour for logged-in accounts
- 5-10% behavioral deviation from organic patterns is flagged
- Datacenter IPs blocked aggressively

### Amazon (Detection: HIGH)
- Zero tolerance for review/rating automation at any volume
- Price monitoring tolerated at low volumes only
- Strict checkout flow bot detection

---

## Dangerous Patterns

| Pattern | Risk |
|---------|------|
| Fixed timing between actions | CRITICAL |
| Linear mouse movements | HIGH |
| Sub-50ms click intervals, instant form fills | CRITICAL |
| Sequential page access (page/1, page/2, page/3) | HIGH |
| Operating 24/7 without breaks | HIGH |
| Activity spike (5 → 500 actions/day) | CRITICAL |
| Fingerprint change between sessions for same account | CRITICAL |
| Mismatched timezone/proxy location | HIGH |
| Missing mouse movement between clicks | HIGH |
| Rotating proxies within a session | HIGH |
| Template messages with identical content | MEDIUM-HIGH |

---

## Safe Patterns

### Timing
- 2-8 second base delay between actions
- Gaussian distribution with 20-40% variance
- 10% chance of 5-15 second "distraction" pause per action
- Content-aware reading delays: scale by visible content length (200-300 WPM)
- Min 2 seconds, max 30 seconds per page before first interaction

### Mouse
- Bezier curve movements with micro-corrections
- Slight overshoot-then-correct on targets
- Random offset within element bounding box
- Settle delay 200-500ms after movement

### Typing
- Gaussian inter-key delays (~80ms base)
- Digraph optimization (common letter pairs typed faster)
- Occasional longer pauses (thinking simulation)

### Scrolling
- 50-150px chunks with 50-150ms micro-pauses
- 20% chance of 0.5-2 second reading pause
- Acceleration/deceleration easing (not constant speed)

### Navigation
- Mix 80% target actions with 20% decoy navigation (homepage, /about, /faq)
- Non-sequential access patterns with backtracking
- Vary entry points (don't always start from same page)

### Sessions
- 30-90 minute active periods with breaks
- Operate during realistic waking hours for account timezone
- Gradual ramp-up: start 25% of target volume, +10-20% per week

---

## Valuable Account Strategy

**Goal: Maximum conservatism. Detection = catastrophic.**

### Technical
- Camoufox tier (Tier 3) — C++ level modifications, 0% detection on CreepJS/BrowserScan
- Persistent fingerprint identity per account — never change
- Residential/ISP proxy bound per account permanently
- Geographic coherence: timezone, locale, language match proxy IP
- Full behavioral simulation enabled

### Operational
- Manual foundation: use account manually for weeks/months before automation
- Max 50% of known platform rate limits
- Mix automated + manual activity (never 100% automated)
- "Human hours" only (waking hours in account timezone)
- Monitor warnings obsessively: rate limit errors, CAPTCHAs, verification requests
- At FIRST warning: stop all automation 48-72 hours, resume at 50% previous volume
- Never automate sensitive operations (password, payment, security settings)

### Isolation
- Never run valuable account alongside other accounts in any shared profile
- Never access from more than one geographic location in same day
- Dedicated proxy, dedicated fingerprint, dedicated browser profile

---

## Disposable Account Strategy

**Goal: Optimize throughput, accept losses as operational cost.**

### Technical
- Middle tier (Tier 2) acceptable
- Full profile isolation per account still non-negotiable
- Residential proxy pools with session-based assignment
- Push to 70-80% of rate limits

### Operational
- Faster ramp-up (days instead of weeks)
- Maintain account pool at various ages/stages
- Rotate activity across pool
- Retire accounts at first warning, activate replacements
- Track ban rates as a metric; adjust aggressiveness by platform
- Budget for replacement costs (phone numbers, maturation time)

### Isolation (Same as Valuable — No Shortcuts)
- Unique fingerprint per account
- Unique proxy binding per account
- Isolated cookies, localStorage, cache
- No shared email, phone, or recovery info
- Vary message templates and timing patterns across accounts

---

## Framework Configuration by Risk Level

### Conservative (Valuable Accounts)
```bash
BROWSER_USE_HUMANIZE=1
BROWSER_USE_GEO=us          # match proxy exit
PROXY_SERVER=socks5://residential-proxy:1080
```
- Tier 3 (Camoufox)
- Rate limits at 50% of platform maximums
- Profile persistence with fingerprint lock
- Session duration: 30-60 minutes with 15-30 minute breaks

### Moderate (Aged Disposable Accounts)
```bash
BROWSER_USE_HUMANIZE=1
BROWSER_USE_GEO=us
PROXY_SERVER=http://rotating-residential:8080
```
- Tier 2 (Patchright)
- Rate limits at 70% of platform maximums
- Profile persistence per account
- Session duration: 60-90 minutes

### Aggressive (Fresh Disposable Accounts)
```bash
BROWSER_USE_HUMANIZE=0      # speed over stealth
```
- Tier 1 (Playwright) for friendly sites, Tier 2 for protected
- Rate limits at configured defaults
- Profile persistence optional
- Accept higher ban rate

---

## Risk Matrix

| Scenario | Risk | Notes |
|----------|------|-------|
| Public data scraping, no login | LOW | Fingerprint + rate limiting sufficient |
| Logged-in read-only, valuable account | LOW-MEDIUM | Full stealth + conservative rates |
| Logged-in write actions (likes, follows), valuable | MEDIUM | Behavioral detection active |
| Logged-in bulk messaging, valuable | HIGH | Content analysis + velocity detection |
| Multi-account management, disposable | MEDIUM | With proper per-account isolation |
| Any automation on Meta, valuable account | HIGH | Aggressive enforcement, poor false positive handling |
| LinkedIn outreach, valuable account | MEDIUM | Well-understood limits, graduated warnings |
| Reddit multi-account | HIGH | Account correlation is primary detection vector |

---

## Timeline: Key Changes 2025-2026

| Date | Event |
|------|-------|
| Feb 2025 | puppeteer-stealth discontinued; Cloudflare detects its patterns |
| Spring 2025 | Instagram mass ban wave — accounts vanishing overnight |
| Mid 2025 | F5 confirms 84% of sites cannot detect residential proxies |
| Late 2025 | CDP serialization detection widely adopted by anti-bot vendors |
| Dec 2025 | Meta deploys new enforcement system — Facebook/Instagram ban spike |
| 2026 | Cloudflare per-customer ML models; DataDome behavioral-only detection; JA4 standard |

---

## Warning Signs (Stop Immediately)

1. CAPTCHA appears where it didn't before
2. Rate limit / 429 responses increase in frequency
3. Account verification request (email, phone, ID)
4. "Unusual activity detected" notification
5. Temporary restriction on specific features
6. Profile visibility or reach drops suddenly
7. Login requires additional verification from new location

**Response**: Stop all automation for 48-72 hours. Resume at 50% volume. If warnings repeat, reassess approach entirely.
