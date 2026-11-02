from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from university_connector.config import Settings

log = logging.getLogger(__name__)

KEYRING_SERVICE = "university-connector"
KEYRING_ENCRYPTION_ENTRY = "session-encryption-key"


def _cookie_matches_host(cookie_domain: str, host: str) -> bool:
    domain = cookie_domain.lstrip(".").lower()
    host = host.lower()
    return host == domain or host.endswith("." + domain)


@dataclass
class SessionData:
    # Playwright cookie dicts for every domain in the login flow (LMS + IdP + MFA),
    # so a later silent refresh can replay the SSO session, not just the LMS one.
    cookies: list[dict[str, Any]]
    xsrf_token: str | None = None
    user_agent: str | None = None
    captured_at: float = field(default_factory=time.time)
    last_refreshed_at: float = field(default_factory=time.time)
    last_validated_at: float | None = None
    needs_reauth: bool = False

    def cookies_for_host(self, host: str) -> dict[str, str]:
        return {
            c["name"]: c["value"]
            for c in self.cookies
            if _cookie_matches_host(c.get("domain", ""), host)
        }

    def cookie_header(self, host: str) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookies_for_host(host).items())

    def has_cookies(self, host: str, names: list[str]) -> bool:
        present = self.cookies_for_host(host)
        return all(present.get(name) for name in names)

    def update_host_cookies(self, host: str, values: dict[str, str]) -> bool:
        """Merge cookie values the LMS rotated via Set-Cookie. Returns True if anything changed."""
        changed = False
        for cookie in self.cookies:
            name = cookie["name"]
            if name in values and _cookie_matches_host(cookie.get("domain", ""), host):
                if cookie["value"] != values[name]:
                    cookie["value"] = values[name]
                    changed = True
        return changed


class _KeyProvider:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._key: bytes | None = None

    def get(self) -> bytes:
        if self._key is None:
            self._key = self._load_or_create()
        return self._key

    def _load_or_create(self) -> bytes:
        if self._settings.use_keyring:
            try:
                import keyring

                existing = keyring.get_password(KEYRING_SERVICE, KEYRING_ENCRYPTION_ENTRY)
                if existing:
                    return existing.encode()
                key = Fernet.generate_key()
                keyring.set_password(KEYRING_SERVICE, KEYRING_ENCRYPTION_ENTRY, key.decode())
                return key
            except Exception as exc:  # no usable keyring backend (headless Linux, CI)
                log.warning("OS keyring unavailable (%s); using local key file", exc)

        key_file = self._settings.home / ".encryption-key"
        if key_file.exists():
            return key_file.read_bytes().strip()
        key = Fernet.generate_key()
        key_file.write_bytes(key)
        os.chmod(key_file, 0o600)
        return key


class SessionStore:
    def __init__(self, settings: Settings):
        self._settings = settings
        settings.ensure_dirs()
        self._keys = _KeyProvider(settings)

    def university_dir(self, university_id: str) -> Path:
        path = self._settings.universities_home / university_id
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o700)
        return path

    def profile_dir(self, university_id: str) -> Path:
        path = self.university_dir(university_id) / "browser-profile"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _session_file(self, university_id: str) -> Path:
        return self.university_dir(university_id) / "session.enc"

    def load(self, university_id: str) -> SessionData | None:
        path = self._session_file(university_id)
        if not path.exists():
            return None
        try:
            raw = Fernet(self._keys.get()).decrypt(path.read_bytes())
        except InvalidToken:
            log.warning("Could not decrypt session for %s; treating as signed out", university_id)
            return None
        return SessionData(**json.loads(raw))

    def save(self, university_id: str, data: SessionData) -> None:
        path = self._session_file(university_id)
        token = Fernet(self._keys.get()).encrypt(json.dumps(asdict(data)).encode())
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(token)
        os.chmod(tmp, 0o600)
        tmp.replace(path)

    def mark_needs_reauth(self, university_id: str) -> None:
        data = self.load(university_id)
        if data and not data.needs_reauth:
            data.needs_reauth = True
            self.save(university_id, data)

    def clear(self, university_id: str) -> None:
        self._session_file(university_id).unlink(missing_ok=True)
