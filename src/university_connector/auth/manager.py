from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass
from typing import Literal
from urllib.parse import urlencode

from university_connector.auth.access import AccessKey
from university_connector.auth.persistent_browser import (
    AuthError,
    PersistentBrowser,
    ReauthRequired,
)
from university_connector.auth.session_store import SessionData, SessionStore
from university_connector.config import Settings
from university_connector.universities import UniversityRegistry

log = logging.getLogger(__name__)

ConnectionState = Literal["connected", "expired", "not_connected", "connecting"]


@dataclass
class LoginAttempt:
    status: Literal["in_progress", "succeeded", "failed"]
    started_at: float
    finished_at: float | None = None
    message: str = ""


class AuthManager:
    def __init__(
        self,
        settings: Settings,
        registry: UniversityRegistry,
        store: SessionStore,
        browser: PersistentBrowser,
        access_key: AccessKey,
    ):
        self.settings = settings
        self.registry = registry
        self.store = store
        self.browser = browser
        self.access_key = access_key
        # One browser per profile at a time: Chromium refuses to share a user-data dir.
        self._locks: dict[str, asyncio.Lock] = {}
        self._attempts: dict[str, LoginAttempt] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    def _lock(self, university_id: str) -> asyncio.Lock:
        return self._locks.setdefault(university_id, asyncio.Lock())

    def reconnect_url(self, university_id: str) -> str:
        query = urlencode({"university": university_id, "key": self.access_key.value})
        return f"{self.settings.base_url}/auth/reconnect?{query}"

    def get_session(self, university_id: str) -> SessionData:
        university = self.registry.get(university_id)
        session = self.store.load(university_id)
        if session is None:
            raise ReauthRequired(university_id, f"Not connected to {university.name} yet")
        if session.needs_reauth:
            raise ReauthRequired(university_id, f"{university.name} session expired")
        return session

    def save_session(self, university_id: str, session: SessionData) -> None:
        self.store.save(university_id, session)

    def state(self, university_id: str) -> ConnectionState:
        attempt = self._attempts.get(university_id)
        if attempt and attempt.status == "in_progress":
            return "connecting"
        session = self.store.load(university_id)
        if session is None:
            return "not_connected"
        return "expired" if session.needs_reauth else "connected"

    def status(self, university_id: str) -> dict:
        university = self.registry.get(university_id)
        session = self.store.load(university_id)
        attempt = self._attempts.get(university_id)
        return {
            "university_id": university_id,
            "name": university.name,
            "lms_type": university.lms_type,
            "state": self.state(university_id),
            "connected_since": session.captured_at if session else None,
            "last_refreshed_at": session.last_refreshed_at if session else None,
            "last_validated_at": session.last_validated_at if session else None,
            "login_attempt": asdict(attempt) if attempt else None,
            "reconnect_url": self.reconnect_url(university_id),
        }

    def connected_university_ids(self) -> list[str]:
        return [u.id for u in self.registry.all() if self.state(u.id) == "connected"]

    async def login(self, university_id: str) -> SessionData:
        """Open a visible browser and wait for the user to finish signing in."""
        university = self.registry.get(university_id)
        async with self._lock(university_id):
            self._attempts[university_id] = LoginAttempt("in_progress", time.time())
            try:
                session = await self.browser.login_interactive(university)
            except Exception as exc:
                self._attempts[university_id] = LoginAttempt(
                    "failed", self._attempts[university_id].started_at, time.time(), str(exc)
                )
                raise
            self._attempts[university_id] = LoginAttempt(
                "succeeded",
                self._attempts[university_id].started_at,
                time.time(),
                f"Connected to {university.name}",
            )
            log.info("Interactive login succeeded for %s", university_id)
            return session

    def start_login(self, university_id: str) -> LoginAttempt:
        """Kick off ``login`` in the background (used by the web portal)."""
        self.registry.get(university_id)
        task = self._tasks.get(university_id)
        if task and not task.done():
            return self._attempts[university_id]
        self._attempts[university_id] = LoginAttempt("in_progress", time.time())

        async def runner() -> None:
            try:
                await self.login(university_id)
            except AuthError as exc:
                log.info("Login for %s did not finish: %s", university_id, exc)
            except Exception:
                log.exception("Login for %s crashed", university_id)

        self._tasks[university_id] = asyncio.create_task(runner())
        return self._attempts[university_id]

    async def refresh(self, university_id: str) -> SessionData:
        """Silently mint fresh LMS cookies; raises ReauthRequired if a human is needed."""
        university = self.registry.get(university_id)
        lock = self._lock(university_id)
        if lock.locked():
            if self.state(university_id) == "connecting":
                raise ReauthRequired(
                    university_id, f"Sign-in to {university.name} is in progress; try again shortly"
                )
            # Another refresh owns the profile; reuse its result instead of racing it.
            async with lock:
                return self.get_session(university_id)
        async with lock:
            if self.store.load(university_id) is None:
                raise ReauthRequired(university_id, f"Not connected to {university.name} yet")
            try:
                session = await self.browser.silent_refresh(university)
            except ReauthRequired:
                self.store.mark_needs_reauth(university_id)
                log.info("Silent refresh failed for %s; user must reconnect", university_id)
                raise
            except Exception as exc:
                log.exception("Silent refresh crashed for %s", university_id)
                raise ReauthRequired(university_id, f"Silent refresh failed: {exc}") from exc
            log.info("Silent refresh succeeded for %s", university_id)
            return session

    def disconnect(self, university_id: str) -> None:
        self.registry.get(university_id)
        self.store.clear(university_id)
        self._attempts.pop(university_id, None)
