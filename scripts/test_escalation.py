#!/usr/bin/env python3
"""Unit tests for the block-assessment / escalation-recommendation logic
(detection.recommendation_for_protection + assess_block + is_blocked wrapper).

Standalone: `python test_escalation.py`. No browser, no network."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import detection as d

_PASS = _FAIL = 0
def chk(name, cond, detail=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1; print(f"  [PASS] {name}  {detail}")
    else:
        _FAIL += 1; print(f"  [FAIL] {name}  {detail}")


# ---- recommendation_for_protection: pure mapping -------------------------
print("--- recommendation_for_protection: tier/proxy/sticky mapping ---")
EXPECT = {
    # protection: (tier, needs_proxy, needs_sticky)
    "datadome":      (3, True,  True),
    "perimeterx":    (3, True,  True),
    "akamai":        (3, True,  True),
    "cloudflare_uam":(3, True,  False),
    "cloudflare":    (2, True,  False),
    "captcha":       (2, False, False),
    "generic":       (2, False, False),
}
for prot, (tier, proxy, sticky) in EXPECT.items():
    r = d.recommendation_for_protection(prot)
    chk(f"{prot} → tier", r["recommended_tier"] == tier, f"got {r['recommended_tier']} want {tier}")
    chk(f"{prot} → needs_proxy", r["needs_proxy"] == proxy, f"got {r['needs_proxy']}")
    chk(f"{prot} → needs_sticky", r["needs_sticky"] == sticky, f"got {r['needs_sticky']}")
    chk(f"{prot} → has reason", bool(r["escalation_reason"]), f"reason={r['escalation_reason'][:40]!r}")

# None / unknown
chk("None → tier 1, no proxy", d.recommendation_for_protection(None) ==
    {"recommended_tier": 1, "needs_proxy": False, "needs_sticky": False, "escalation_reason": ""})
unk = d.recommendation_for_protection("totally-new-vendor")
chk("unknown protection → tier 2 fallback", unk["recommended_tier"] == 2 and not unk["needs_proxy"],
    f"{unk}")

# ---- mirror invariant: matches ModeDetector.detect()'s mapping -----------
print("--- mirror invariant vs ModeDetector.detect() ---")
async def _mirror():
    det = d.ModeDetector()
    # cloudflare html → detect() says tier 2 + proxy; our pure map must agree
    prof = await det.detect("https://unknown-site.test/", html="cdn-cgi/challenge-platform")
    rec = d.recommendation_for_protection(prof.antibot)
    chk("detect(cloudflare).tier == recommendation tier",
        prof.recommended_tier == rec["recommended_tier"],
        f"detect={prof.recommended_tier} rec={rec['recommended_tier']} antibot={prof.antibot}")
asyncio.run(_mirror())

# ---- url-aware RAISE (never lower) ---------------------------------------
print("--- domain-aware raise (never lower) ---")
# linkedin profile is datadome/tier3/sticky — a live 'generic' block on linkedin should RAISE
raised = d.recommendation_for_protection("generic", url="https://www.linkedin.com/feed/")
chk("linkedin + generic block raises to tier 3", raised["recommended_tier"] == 3, f"{raised}")
chk("linkedin raise sets needs_proxy", raised["needs_proxy"] is True)
chk("linkedin raise sets needs_sticky", raised["needs_sticky"] is True)
# unknown domain must NOT lower a tier-3 protection
nolower = d.recommendation_for_protection("datadome", url="https://example.com/")
chk("unknown domain does not lower datadome", nolower["recommended_tier"] == 3, f"{nolower}")
# url with no profile match leaves the base mapping intact
base = d.recommendation_for_protection("cloudflare", url="https://nomatch.test/")
chk("no-profile url keeps base tier", base["recommended_tier"] == 2, f"{base}")


# ---- assess_block + is_blocked over a fake page --------------------------
print("--- assess_block / is_blocked (fake page) ---")
class FakePage:
    def __init__(self, title="Home", url="https://example.com/", body=""):
        self._title, self.url, self._body = title, url, body
    async def title(self):
        return self._title
    async def evaluate(self, _js):
        return self._body

async def _page_tests():
    # Cloudflare challenge page
    p = FakePage(title="Just a moment...", url="https://shop.test/")
    a = await d.assess_block(p)
    chk("assess_block(cloudflare) protection", a and a["protection"] == "cloudflare", f"{a}")
    chk("assess_block(cloudflare) tier 2", a and a["recommended_tier"] == 2)
    chk("assess_block(cloudflare) needs_proxy", a and a["needs_proxy"] is True)
    chk("assess_block(cloudflare) reason present", a and bool(a["escalation_reason"]))
    chk("is_blocked wrapper returns string", await d.is_blocked(p) == "cloudflare")

    # Cloudflare UAM (under-attack): UAM-specific body marker → cloudflare_uam → Tier 3
    uam = FakePage(title="Just a moment...", url="https://shop.test/",
                   body="Checking your browser before accessing the site")
    au = await d.assess_block(uam)
    chk("UAM marker → cloudflare_uam", au and au["protection"] == "cloudflare_uam", f"{au}")
    chk("UAM → tier 3 (reachable now)", au and au["recommended_tier"] == 3)
    # plain 'just a moment' WITHOUT UAM markers → cloudflare → Tier 2 (NOT over-escalated)
    plain = FakePage(title="Just a moment...", url="https://shop.test/", body="please wait")
    ap = await d.assess_block(plain)
    chk("plain challenge stays cloudflare/tier2 (no over-escalation)",
        ap and ap["protection"] == "cloudflare" and ap["recommended_tier"] == 2, f"{ap}")

    # DataDome by title + url-aware raise on a known site
    p2 = FakePage(title="datadome", url="https://www.g2.com/x")  # g2 is datadome/tier3 in profiles
    a2 = await d.assess_block(p2)
    chk("assess_block(datadome) tier 3", a2 and a2["recommended_tier"] == 3, f"{a2}")
    chk("assess_block(datadome) sticky", a2 and a2["needs_sticky"] is True)

    # captcha in body
    p3 = FakePage(title="Verify", url="https://x.test/", body="Please complete the CAPTCHA to continue")
    a3 = await d.assess_block(p3)
    chk("assess_block(captcha) tier 2 no proxy", a3 and a3["protection"] == "captcha"
        and a3["recommended_tier"] == 2 and a3["needs_proxy"] is False, f"{a3}")

    # clean page → None
    clean = FakePage(title="Welcome", url="https://example.com/", body="hello world")
    chk("assess_block(clean) → None", await d.assess_block(clean) is None)
    chk("is_blocked(clean) → None", await d.is_blocked(clean) is None)

asyncio.run(_page_tests())

print(f"\n{_PASS} passed, {_FAIL} failed")
sys.exit(1 if _FAIL else 0)
