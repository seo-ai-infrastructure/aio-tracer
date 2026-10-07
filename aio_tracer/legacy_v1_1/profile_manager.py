"""Per-client / per-city CloakBrowser profile management.

Each profile is a small JSON document in ``profiles/`` that pins:

* a deterministic fingerprint seed derived from ``sha256(f"{client_id}:{city}")``
  so the same client+city always presents the same hardware identity,
* the proxy that profile must egress through,
* whether CloakBrowser should auto-match timezone/locale to the proxy IP (geoip),
* the persistent ``user_data_dir`` (cookies, cache, localStorage) for the profile.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

from slugify import slugify

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_PROFILES_DIR = BASE_DIR / "profiles"

# CloakBrowser's own random seeds live in [10000, 99999]; stay inside that range.
_SEED_MIN = 10_000
_SEED_SPAN = 90_000


def fingerprint_hash(client_id: str, city: str) -> str:
    """Full sha256 hex digest of ``"{client_id}:{city}"`` (the canonical identity hash)."""
    return hashlib.sha256(f"{client_id}:{city}".encode("utf-8")).hexdigest()


def fingerprint_seed(client_id: str, city: str) -> int:
    """Deterministic integer seed for ``--fingerprint=SEED`` derived from the sha256 hash."""
    return _SEED_MIN + int(fingerprint_hash(client_id, city), 16) % _SEED_SPAN


def redact_proxy(proxy_url: Optional[str]) -> Optional[str]:
    """Hide proxy credentials for logs / reports."""
    if not proxy_url:
        return proxy_url
    parts = urlsplit(proxy_url)
    if parts.username or parts.password:
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        return urlunsplit((parts.scheme, f"***:***@{host}", parts.path, parts.query, parts.fragment))
    return proxy_url


@dataclass
class BrowserProfile:
    profile_id: str
    client_id: str
    city: str
    fingerprint_hash: str
    fingerprint_seed: int
    proxy_url: Optional[str]
    geoip: bool
    user_data_dir: str
    created_at: str
    updated_at: str
    locale: Optional[str] = None
    timezone: Optional[str] = None
    extra_args: list[str] = field(default_factory=list)

    @property
    def fingerprint_arg(self) -> str:
        return f"--fingerprint={self.fingerprint_seed}"

    def to_public_dict(self) -> dict:
        """Profile dict safe for reports (proxy credentials redacted)."""
        data = asdict(self)
        data["proxy_url"] = redact_proxy(self.proxy_url)
        return data


class ProfileManager:
    """Create, load, list and delete persisted browser profiles."""

    def __init__(self, profiles_dir: Path | str = DEFAULT_PROFILES_DIR):
        self.profiles_dir = Path(profiles_dir)
        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        self.user_data_root = self.profiles_dir / "user_data"
        self.user_data_root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def make_profile_id(client_id: str, city: str) -> str:
        pid = slugify(f"{client_id}-{city}")
        if not pid:
            raise ValueError(f"Cannot derive profile id from client={client_id!r} city={city!r}")
        return pid

    def _config_path(self, profile_id: str) -> Path:
        return self.profiles_dir / f"{profile_id}.json"

    def _write(self, profile: BrowserProfile) -> None:
        path = self._config_path(profile.profile_id)
        # Atomic write so a crash never leaves a half-written profile.
        fd, tmp = tempfile.mkstemp(dir=self.profiles_dir, prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(asdict(profile), fh, indent=2)
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        try:
            os.chmod(path, 0o600)  # may contain proxy credentials
        except OSError:
            pass

    def load(self, profile_id: str) -> Optional[BrowserProfile]:
        path = self._config_path(profile_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return BrowserProfile(**data)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError(f"Corrupt profile file {path}: {exc}") from exc

    def get_or_create(
        self,
        client_id: str,
        city: str,
        proxy_url: Optional[str] = None,
        geoip: bool = True,
        locale: Optional[str] = None,
        timezone_name: Optional[str] = None,
    ) -> BrowserProfile:
        """Load the profile for (client_id, city) or create it.

        If the profile exists and a different non-empty ``proxy_url`` is supplied, the
        proxy is updated (fingerprint seed never changes — it is derived from identity).
        """
        if not client_id or not city:
            raise ValueError("client_id and city are required")
        profile_id = self.make_profile_id(client_id, city)
        now = datetime.now(timezone.utc).isoformat()
        profile = self.load(profile_id)

        if profile is None:
            user_data_dir = self.user_data_root / profile_id
            user_data_dir.mkdir(parents=True, exist_ok=True)
            profile = BrowserProfile(
                profile_id=profile_id,
                client_id=client_id,
                city=city,
                fingerprint_hash=fingerprint_hash(client_id, city),
                fingerprint_seed=fingerprint_seed(client_id, city),
                proxy_url=proxy_url,
                geoip=geoip,
                user_data_dir=str(user_data_dir),
                created_at=now,
                updated_at=now,
                locale=locale,
                timezone=timezone_name,
            )
            self._write(profile)
            logger.info("Created profile %s (seed=%s, proxy=%s)", profile_id,
                        profile.fingerprint_seed, redact_proxy(proxy_url))
            return profile

        changed = False
        if proxy_url and proxy_url != profile.proxy_url:
            logger.info("Updating proxy for profile %s -> %s", profile_id, redact_proxy(proxy_url))
            profile.proxy_url = proxy_url
            changed = True
        if locale and locale != profile.locale:
            profile.locale, changed = locale, True
        if timezone_name and timezone_name != profile.timezone:
            profile.timezone, changed = timezone_name, True
        if profile.geoip != geoip:
            profile.geoip, changed = geoip, True
        Path(profile.user_data_dir).mkdir(parents=True, exist_ok=True)
        if changed:
            profile.updated_at = now
            self._write(profile)
        logger.debug("Loaded profile %s", profile_id)
        return profile

    def list_profiles(self) -> list[BrowserProfile]:
        profiles = []
        for path in sorted(self.profiles_dir.glob("*.json")):
            try:
                p = self.load(path.stem)
                if p:
                    profiles.append(p)
            except ValueError as exc:
                logger.warning("Skipping unreadable profile: %s", exc)
        return profiles

    def delete(self, profile_id: str, remove_user_data: bool = True) -> bool:
        profile = self.load(profile_id)
        if profile is None:
            logger.warning("Profile %s not found", profile_id)
            return False
        self._config_path(profile_id).unlink(missing_ok=True)
        if remove_user_data:
            udd = Path(profile.user_data_dir)
            # Only ever delete inside our own user_data root.
            if udd.exists() and self.user_data_root.resolve() in udd.resolve().parents:
                shutil.rmtree(udd, ignore_errors=True)
        logger.info("Deleted profile %s", profile_id)
        return True
