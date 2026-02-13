"""Fingerprint persistence for consistent browser identity per domain.

Detection systems flag randomization as suspicious. Authentic, consistent
fingerprints per domain are less detectable than varying fingerprints.

Extracted from ultimate-scraper fingerprint/manager.py — adapted to use
Config.PROFILE_DIR for default db_path instead of core.config.
"""

from __future__ import annotations

import sqlite3
import json
import random
import string
from pathlib import Path
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlparse

from config import Config


# Browser market share by region (2026 data)
BROWSER_MARKET_SHARE: dict[str, dict[str, float]] = {
    "us": {"chrome": 0.65, "safari": 0.20, "edge": 0.10, "firefox": 0.05},
    "uk": {"chrome": 0.60, "safari": 0.25, "edge": 0.10, "firefox": 0.05},
    "de": {"chrome": 0.50, "firefox": 0.25, "safari": 0.15, "edge": 0.10},
    "fr": {"chrome": 0.55, "firefox": 0.20, "safari": 0.15, "edge": 0.10},
    "jp": {"chrome": 0.70, "safari": 0.15, "edge": 0.10, "firefox": 0.05},
    "cn": {"chrome": 0.60, "edge": 0.25, "firefox": 0.10, "safari": 0.05},
    "au": {"chrome": 0.60, "safari": 0.25, "edge": 0.10, "firefox": 0.05},
    "br": {"chrome": 0.75, "edge": 0.15, "firefox": 0.07, "safari": 0.03},
    "in": {"chrome": 0.80, "edge": 0.10, "firefox": 0.07, "safari": 0.03},
}

# Browser versions available for impersonation
BROWSER_VERSIONS: dict[str, list[str]] = {
    "chrome": ["chrome141", "chrome142", "chrome143", "chrome144"],
    "firefox": ["firefox134", "firefox135", "firefox136"],
    "safari": ["safari17_5", "safari18"],
    "edge": ["edge139", "edge140", "edge141"],
}

# Platform strings by browser
PLATFORM_BY_BROWSER: dict[str, list[str]] = {
    "chrome": ["Win32", "Linux x86_64", "MacIntel"],
    "firefox": ["Win32", "Linux x86_64", "MacIntel"],
    "safari": ["MacIntel"],
    "edge": ["Win32"],
}

# Accept-Language by geo
ACCEPT_LANGUAGE_BY_GEO: dict[str, str] = {
    "us": "en-US,en;q=0.9",
    "uk": "en-GB,en;q=0.9",
    "de": "de-DE,de;q=0.9,en;q=0.8",
    "fr": "fr-FR,fr;q=0.9,en;q=0.8",
    "jp": "ja-JP,ja;q=0.9,en;q=0.8",
    "cn": "zh-CN,zh;q=0.9,en;q=0.8",
    "au": "en-AU,en;q=0.9",
    "br": "pt-BR,pt;q=0.9,en;q=0.8",
    "in": "en-IN,en;q=0.9,hi;q=0.8",
}


@dataclass
class FingerprintProfile:
    """Persistent fingerprint identity for a domain."""

    fingerprint_id: str
    domain: str
    browser: str
    browser_version: str
    impersonate: str
    user_agent: str
    accept_language: str
    platform: str
    geo: str = "us"
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    last_used_at: str = field(default_factory=lambda: datetime.now().isoformat())
    use_count: int = 0
    blocked_count: int = 0
    success_count: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> FingerprintProfile:
        return cls(**data)

    def touch(self) -> None:
        self.last_used_at = datetime.now().isoformat()
        self.use_count += 1

    def record_success(self) -> None:
        self.success_count += 1
        self.touch()

    def record_block(self) -> None:
        self.blocked_count += 1
        self.touch()

    @property
    def block_rate(self) -> float:
        total = self.success_count + self.blocked_count
        if total == 0:
            return 0.0
        return self.blocked_count / total


class FingerprintManager:
    """Manage persistent fingerprints per domain.

    SQLite-backed storage with automatic rotation when block rate
    exceeds threshold or fingerprint exceeds max age.
    """

    BLOCK_RATE_THRESHOLD = 0.3
    MAX_BLOCKS_BEFORE_ROTATE = 5
    MAX_AGE_DAYS = 30

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or (Config.PROFILE_DIR / "fingerprints.db")
        self._init_db()

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS fingerprints (
                fingerprint_id TEXT PRIMARY KEY,
                domain TEXT NOT NULL,
                browser TEXT NOT NULL,
                browser_version TEXT NOT NULL,
                impersonate TEXT NOT NULL,
                user_agent TEXT NOT NULL,
                accept_language TEXT NOT NULL,
                platform TEXT NOT NULL,
                geo TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_used_at TEXT NOT NULL,
                use_count INTEGER DEFAULT 0,
                blocked_count INTEGER DEFAULT 0,
                success_count INTEGER DEFAULT 0,
                UNIQUE(domain, browser)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_fingerprint_domain ON fingerprints(domain)
        """)
        conn.commit()
        conn.close()

    def get_for_domain(self, domain: str) -> Optional[FingerprintProfile]:
        """Get existing fingerprint for a domain."""
        domain = self._normalize_domain(domain)

        conn = sqlite3.connect(self.db_path)
        cursor = conn.execute(
            """
            SELECT fingerprint_id, domain, browser, browser_version, impersonate,
                   user_agent, accept_language, platform, geo, created_at,
                   last_used_at, use_count, blocked_count, success_count
            FROM fingerprints
            WHERE domain = ?
            ORDER BY last_used_at DESC
            LIMIT 1
            """,
            (domain,),
        )
        row = cursor.fetchone()
        conn.close()

        if row:
            return FingerprintProfile(
                fingerprint_id=row[0], domain=row[1], browser=row[2],
                browser_version=row[3], impersonate=row[4], user_agent=row[5],
                accept_language=row[6], platform=row[7], geo=row[8],
                created_at=row[9], last_used_at=row[10], use_count=row[11],
                blocked_count=row[12], success_count=row[13],
            )
        return None

    def get_or_create(self, domain: str, geo: str = "us") -> FingerprintProfile:
        """Get existing fingerprint for domain or create a new one."""
        domain = self._normalize_domain(domain)
        geo_key = geo.split("-")[0] if "-" in geo else geo

        existing = self.get_for_domain(domain)
        if existing:
            if self.should_rotate(existing.fingerprint_id):
                return self.rotate(existing.fingerprint_id)
            return existing

        return self._create_new(domain, geo_key)

    def _create_new(self, domain: str, geo: str) -> FingerprintProfile:
        browser = self._select_browser_weighted(geo)
        browser_version = random.choice(BROWSER_VERSIONS[browser])
        platform = random.choice(PLATFORM_BY_BROWSER[browser])
        accept_language = ACCEPT_LANGUAGE_BY_GEO.get(geo, "en-US,en;q=0.9")
        user_agent = self._generate_user_agent(browser, browser_version, platform)

        fingerprint = FingerprintProfile(
            fingerprint_id=self._generate_id(),
            domain=domain,
            browser=browser,
            browser_version=browser_version,
            impersonate=browser_version,
            user_agent=user_agent,
            accept_language=accept_language,
            platform=platform,
            geo=geo,
        )

        self.save(fingerprint)
        return fingerprint

    def save(self, fingerprint: FingerprintProfile) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """
            INSERT OR REPLACE INTO fingerprints
            (fingerprint_id, domain, browser, browser_version, impersonate,
             user_agent, accept_language, platform, geo, created_at,
             last_used_at, use_count, blocked_count, success_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fingerprint.fingerprint_id, fingerprint.domain,
                fingerprint.browser, fingerprint.browser_version,
                fingerprint.impersonate, fingerprint.user_agent,
                fingerprint.accept_language, fingerprint.platform,
                fingerprint.geo, fingerprint.created_at,
                fingerprint.last_used_at, fingerprint.use_count,
                fingerprint.blocked_count, fingerprint.success_count,
            ),
        )
        conn.commit()
        conn.close()

    def record_usage(self, fingerprint_id: str, success: bool) -> None:
        conn = sqlite3.connect(self.db_path)
        if success:
            conn.execute(
                """UPDATE fingerprints
                   SET success_count = success_count + 1, use_count = use_count + 1,
                       last_used_at = ?
                   WHERE fingerprint_id = ?""",
                (datetime.now().isoformat(), fingerprint_id),
            )
        else:
            conn.execute(
                """UPDATE fingerprints
                   SET blocked_count = blocked_count + 1, use_count = use_count + 1,
                       last_used_at = ?
                   WHERE fingerprint_id = ?""",
                (datetime.now().isoformat(), fingerprint_id),
            )
        conn.commit()
        conn.close()

    def should_rotate(self, fingerprint_id: str) -> bool:
        conn = sqlite3.connect(self.db_path)
        cursor = conn.execute(
            "SELECT blocked_count, success_count, created_at FROM fingerprints WHERE fingerprint_id = ?",
            (fingerprint_id,),
        )
        row = cursor.fetchone()
        conn.close()

        if not row:
            return True

        blocked_count, success_count, created_at = row

        try:
            created = datetime.fromisoformat(created_at)
            if (datetime.now() - created).days > self.MAX_AGE_DAYS:
                return True
        except (ValueError, TypeError):
            pass

        total = blocked_count + success_count
        if total >= 10 and blocked_count / total > self.BLOCK_RATE_THRESHOLD:
            return True

        if blocked_count >= self.MAX_BLOCKS_BEFORE_ROTATE and success_count == 0:
            return True

        return False

    def rotate(self, fingerprint_id: str) -> FingerprintProfile:
        conn = sqlite3.connect(self.db_path)
        cursor = conn.execute(
            "SELECT domain, geo FROM fingerprints WHERE fingerprint_id = ?",
            (fingerprint_id,),
        )
        row = cursor.fetchone()
        if not row:
            conn.close()
            raise ValueError(f"Fingerprint {fingerprint_id} not found")

        domain, geo = row
        conn.execute("DELETE FROM fingerprints WHERE fingerprint_id = ?", (fingerprint_id,))
        conn.commit()
        conn.close()

        return self._create_new(domain, geo)

    def delete(self, fingerprint_id: str) -> bool:
        conn = sqlite3.connect(self.db_path)
        cursor = conn.execute(
            "DELETE FROM fingerprints WHERE fingerprint_id = ?", (fingerprint_id,),
        )
        conn.commit()
        deleted = cursor.rowcount > 0
        conn.close()
        return deleted

    def list_fingerprints(self, domain: Optional[str] = None) -> list[dict]:
        conn = sqlite3.connect(self.db_path)
        if domain:
            domain = self._normalize_domain(domain)
            cursor = conn.execute(
                """SELECT fingerprint_id, domain, browser, browser_version, geo,
                          use_count, blocked_count, success_count, last_used_at
                   FROM fingerprints WHERE domain = ? ORDER BY last_used_at DESC""",
                (domain,),
            )
        else:
            cursor = conn.execute(
                """SELECT fingerprint_id, domain, browser, browser_version, geo,
                          use_count, blocked_count, success_count, last_used_at
                   FROM fingerprints ORDER BY last_used_at DESC""",
            )

        results = []
        for row in cursor.fetchall():
            results.append({
                "fingerprint_id": row[0], "domain": row[1],
                "browser": row[2], "browser_version": row[3],
                "geo": row[4], "use_count": row[5],
                "blocked_count": row[6], "success_count": row[7],
                "last_used_at": row[8],
            })
        conn.close()
        return results

    def cleanup_old(self, max_age_days: int = 30) -> int:
        cutoff = (datetime.now() - timedelta(days=max_age_days)).isoformat()
        conn = sqlite3.connect(self.db_path)
        cursor = conn.execute("DELETE FROM fingerprints WHERE last_used_at < ?", (cutoff,))
        conn.commit()
        deleted = cursor.rowcount
        conn.close()
        return deleted

    def _select_browser_weighted(self, geo: str) -> str:
        shares = BROWSER_MARKET_SHARE.get(geo, BROWSER_MARKET_SHARE["us"])
        browsers = list(shares.keys())
        weights = list(shares.values())
        return random.choices(browsers, weights=weights, k=1)[0]

    def _generate_user_agent(self, browser: str, version: str, platform: str) -> str:
        version_num = "".join(filter(str.isdigit, version))

        if browser == "chrome":
            if platform == "Win32":
                return f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{version_num}.0.0.0 Safari/537.36"
            elif platform == "MacIntel":
                return f"Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{version_num}.0.0.0 Safari/537.36"
            else:
                return f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{version_num}.0.0.0 Safari/537.36"
        elif browser == "firefox":
            if platform == "Win32":
                return f"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:{version_num}.0) Gecko/20100101 Firefox/{version_num}.0"
            elif platform == "MacIntel":
                return f"Mozilla/5.0 (Macintosh; Intel Mac OS X 14.5; rv:{version_num}.0) Gecko/20100101 Firefox/{version_num}.0"
            else:
                return f"Mozilla/5.0 (X11; Linux x86_64; rv:{version_num}.0) Gecko/20100101 Firefox/{version_num}.0"
        elif browser == "safari":
            return f"Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/{version_num}.0 Safari/605.1.15"
        elif browser == "edge":
            return f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{version_num}.0.0.0 Safari/537.36 Edg/{version_num}.0.0.0"

        return f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{version_num}.0.0.0 Safari/537.36"

    def _generate_id(self) -> str:
        return "".join(random.choices(string.ascii_lowercase + string.digits, k=12))

    def _normalize_domain(self, domain_or_url: str) -> str:
        if "://" in domain_or_url:
            parsed = urlparse(domain_or_url)
            domain = parsed.netloc
        else:
            domain = domain_or_url
        if domain.startswith("www."):
            domain = domain[4:]
        return domain.lower()
