"""
Session & identity persistence.

Manages browser profiles with persistent cookies, storage state,
and metadata across invocations.

Dual-mode credential injection (browser-use v0.11.9 pattern):
  1. Tagged: <secret>key</secret> → resolved from credentials store
  2. Literal fallback: if fill value matches a credential key name, inject the secret

Phase 1: Basic profile CRUD, cookies/storage save/load, credential injection.
Phase 2 adds: fingerprint persistence (BrowserForge), proxy assignment.
Phase 3 adds: checkpoint/resume, multi-account, activity logging.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from config import Config, validate_profile_name, safe_profile_path


class SessionManager:
    """Manages persistent browser identity profiles."""

    def __init__(self, profile_dir: Path | None = None):
        self.profile_dir = profile_dir or Config.PROFILE_DIR
        self.profile_dir.mkdir(parents=True, exist_ok=True)

    # -----------------------------------------------------------------------
    # Profile CRUD
    # -----------------------------------------------------------------------

    def _safe_dir(self, name: str) -> tuple[Path | None, str | None]:
        """Validate and resolve profile directory. Returns (path, error)."""
        err = validate_profile_name(name)
        if err:
            return None, err
        resolved = safe_profile_path(self.profile_dir, name)
        if resolved is None:
            return None, f"Invalid profile path: {name}"
        return resolved, None

    def create_profile(self, name: str, domain: str, tier: int = 1) -> dict:
        """Create a new identity profile."""
        pdir, err = self._safe_dir(name)
        if err:
            return {"success": False, "error": err}
        if pdir.exists():
            return {"success": False, "error": f"Profile '{name}' already exists"}

        pdir.mkdir(parents=True)
        now = datetime.now(timezone.utc).isoformat()
        meta = {
            "name": name,
            "domain": domain,
            "tier": tier,
            "created": now,
            "last_used": now,
            "has_cookies": False,
            "has_storage": False,
            "has_fingerprint": False,
            "proxy": None,
        }
        (pdir / "meta.json").write_text(json.dumps(meta, indent=2))
        return {"success": True, "profile": meta}

    def load_profile(self, name: str) -> dict | None:
        """Load profile metadata."""
        pdir, err = self._safe_dir(name)
        if err:
            return None
        meta_path = pdir / "meta.json"
        if not meta_path.exists():
            return None
        meta = json.loads(meta_path.read_text())

        # Check what state files exist
        meta["has_cookies"] = (pdir / "cookies.json").exists()
        meta["has_storage"] = (pdir / "storage.json").exists()
        meta["has_fingerprint"] = (pdir / "fingerprint.json").exists()

        return meta

    def update_last_used(self, name: str) -> None:
        """Touch last_used timestamp."""
        pdir, err = self._safe_dir(name)
        if err or pdir is None:
            return
        meta = self.load_profile(name)
        if meta:
            meta["last_used"] = datetime.now(timezone.utc).isoformat()
            (pdir / "meta.json").write_text(json.dumps(meta, indent=2))

    def list_profiles(self) -> list[dict]:
        """List all profiles."""
        profiles = []
        for pdir in sorted(self.profile_dir.iterdir()):
            if pdir.is_dir() and (pdir / "meta.json").exists():
                meta = self.load_profile(pdir.name)
                if meta:
                    profiles.append(meta)
        return profiles

    def delete_profile(self, name: str) -> dict:
        """Delete a profile and all its data."""
        import shutil
        pdir, err = self._safe_dir(name)
        if err:
            return {"success": False, "error": err}
        if not pdir.exists():
            return {"success": False, "error": f"Profile '{name}' not found"}
        shutil.rmtree(pdir)
        return {"success": True}

    # -----------------------------------------------------------------------
    # Storage state (cookies + localStorage)
    # -----------------------------------------------------------------------

    def get_storage_state_path(self, name: str) -> Path | None:
        """Get path to storage state file if it exists."""
        pdir, err = self._safe_dir(name)
        if err or pdir is None:
            return None
        path = pdir / "storage.json"
        return path if path.exists() else None

    def save_storage_state(self, name: str, state: dict) -> None:
        """Save browser storage state to profile."""
        pdir, err = self._safe_dir(name)
        if err or pdir is None:
            return
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "storage.json").write_text(json.dumps(state, indent=2))
        self.update_last_used(name)

    def save_cookies(self, name: str, cookies: list[dict]) -> None:
        """Save cookies separately (useful for cookie-only persistence)."""
        pdir, err = self._safe_dir(name)
        if err or pdir is None:
            return
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "cookies.json").write_text(json.dumps(cookies, indent=2))
        self.update_last_used(name)

    def load_cookies(self, name: str) -> list[dict] | None:
        """Load saved cookies."""
        pdir, err = self._safe_dir(name)
        if err or pdir is None:
            return None
        path = pdir / "cookies.json"
        if path.exists():
            return json.loads(path.read_text())
        return None

    # -----------------------------------------------------------------------
    # Tier cache
    # -----------------------------------------------------------------------

    def save_tier(self, name: str, tier: int) -> None:
        """Cache the working tier for this profile's domain."""
        pdir, err = self._safe_dir(name)
        if err or pdir is None:
            return
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "tier.json").write_text(json.dumps({"tier": tier}))

    def load_tier(self, name: str) -> int | None:
        """Load cached tier."""
        pdir, err = self._safe_dir(name)
        if err or pdir is None:
            return None
        path = pdir / "tier.json"
        if path.exists():
            return json.loads(path.read_text()).get("tier")
        return None

    # -----------------------------------------------------------------------
    # Domain tier cache (global across profiles)
    # -----------------------------------------------------------------------

    @staticmethod
    def load_domain_tiers() -> dict[str, int]:
        """Load the global domain -> tier cache."""
        path = Config.DOMAIN_TIER_CACHE_PATH
        if path.exists():
            return json.loads(path.read_text())
        return {}

    @staticmethod
    def save_domain_tier(domain: str, tier: int) -> None:
        """Cache which tier works for a given domain."""
        path = Config.DOMAIN_TIER_CACHE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        cache = {}
        if path.exists():
            cache = json.loads(path.read_text())
        cache[domain] = tier
        path.write_text(json.dumps(cache, indent=2))

    @staticmethod
    def get_domain_tier(domain: str) -> int | None:
        """Get cached tier for a domain."""
        path = Config.DOMAIN_TIER_CACHE_PATH
        if path.exists():
            cache = json.loads(path.read_text())
            return cache.get(domain)
        return None


    # -----------------------------------------------------------------------
    # Fingerprint persistence
    # -----------------------------------------------------------------------

    def save_fingerprint(self, name: str, fp_data: dict) -> None:
        """Save fingerprint data for a profile."""
        pdir, err = self._safe_dir(name)
        if err or pdir is None:
            return
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "fingerprint.json").write_text(json.dumps(fp_data, indent=2))
        self.update_last_used(name)

    def load_fingerprint(self, name: str) -> dict | None:
        """Load fingerprint data for a profile."""
        pdir, err = self._safe_dir(name)
        if err or pdir is None:
            return None
        path = pdir / "fingerprint.json"
        if path.exists():
            return json.loads(path.read_text())
        return None

    # -----------------------------------------------------------------------
    # Credential injection (browser-use v0.11.9 dual-mode)
    # -----------------------------------------------------------------------

    def save_credentials(self, name: str, credentials: dict[str, str]) -> None:
        """Save credentials for a profile (encrypted at rest in Phase 2)."""
        pdir, err = self._safe_dir(name)
        if err or pdir is None:
            return
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "credentials.json").write_text(json.dumps(credentials, indent=2))

    def load_credentials(self, name: str) -> dict[str, str]:
        """Load credentials for a profile."""
        pdir, err = self._safe_dir(name)
        if err or pdir is None:
            return {}
        cred_path = pdir / "credentials.json"
        if cred_path.exists():
            return json.loads(cred_path.read_text())
        return {}

    def resolve_credential(self, profile_name: str, value: str) -> str:
        """Resolve a credential value using dual-mode injection.

        Mode 1 (tagged): <secret>key</secret> → looks up 'key' in credentials store
        Mode 2 (literal): if value matches a credential key name exactly, inject the secret

        Returns the resolved value (original if no match found).
        """
        creds = self.load_credentials(profile_name)
        if not creds:
            return value

        # Mode 1: tagged secrets — <secret>key</secret>
        _SECRET_RE = re.compile(r"<secret>(\w+)</secret>")
        match = _SECRET_RE.search(value)
        if match:
            key = match.group(1)
            if key in creds:
                return _SECRET_RE.sub(creds[key], value)
            return value

        # Mode 2: literal key name match
        if value in creds:
            return creds[value]

        return value


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    request = json.loads(sys.stdin.read())
    action = request.get("action", "list")
    mgr = SessionManager()

    if action == "create":
        result = mgr.create_profile(
            name=request["name"],
            domain=request["domain"],
            tier=request.get("tier", 1),
        )
    elif action == "load":
        profile = mgr.load_profile(request["name"])
        result = {"success": profile is not None, "profile": profile}
    elif action == "list":
        result = {"success": True, "profiles": mgr.list_profiles()}
    elif action == "delete":
        result = mgr.delete_profile(request["name"])
    else:
        result = {"success": False, "error": f"Unknown action: {action}"}

    print(json.dumps(result))
