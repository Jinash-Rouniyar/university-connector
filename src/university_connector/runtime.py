from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from urllib.parse import quote

from university_connector.auth import (
    AccessKey,
    AuthManager,
    PersistentBrowser,
    ReauthRequired,
    SessionStore,
)
from university_connector.config import Settings
from university_connector.lms import DownloadedFile, LMSClient, LMSError, create_lms_client, validate_session
from university_connector.universities import University, UniversityRegistry
from university_connector.universities.registry import RegistryError

log = logging.getLogger(__name__)


def _safe_filename(name: str) -> str:
    name = Path(name).name.strip() or "file"
    return re.sub(r"[^\w.\- ()]+", "_", name)[:200]


class Runtime:
    """Process-wide wiring shared by the web portal, the MCP tools, and the CLI."""

    def __init__(self, settings: Settings | None = None, registry: UniversityRegistry | None = None):
        self.settings = settings or Settings()
        self.settings.ensure_dirs()
        self.registry = registry or UniversityRegistry.load()
        self.store = SessionStore(self.settings)
        self.access_key = AccessKey(self.settings)
        self.browser = PersistentBrowser(self.settings, self.store, validate_session)
        self.auth = AuthManager(
            self.settings, self.registry, self.store, self.browser, self.access_key
        )
        self._clients: dict[str, LMSClient] = {}

    @property
    def portal_url(self) -> str:
        return f"{self.settings.base_url}/?key={quote(self.access_key.value)}"

    def resolve_university(self, university_id: str | None) -> University:
        """Pick the university a tool call means when the agent didn't say."""
        if university_id:
            return self.registry.get(university_id)
        connected = self.auth.connected_university_ids()
        if len(connected) == 1:
            return self.registry.get(connected[0])
        # An expired school still counts, so the agent gets its reconnect link.
        signed_in_before = [u.id for u in self.registry.all() if self.store.load(u.id)]
        if not connected and len(signed_in_before) == 1:
            return self.registry.get(signed_in_before[0])
        if len(self.registry) == 1:
            return self.registry.all()[0]
        options = ", ".join(u.id for u in self.registry.all())
        raise RegistryError(f"Specify university_id; options: {options}")

    def client(self, university_id: str) -> LMSClient:
        if university_id not in self._clients:
            self._clients[university_id] = create_lms_client(
                self.registry.get(university_id), self.auth
            )
        return self._clients[university_id]

    def save_download(
        self, university_id: str, course_id: int, assignment_id: int, file: DownloadedFile
    ) -> dict:
        relative = f"{university_id}/{course_id}/{assignment_id}/{_safe_filename(file.filename)}"
        path = self.settings.downloads_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(file.content)
        signature = self.access_key.sign(relative)
        return {
            "filename": path.name,
            "size_bytes": len(file.content),
            "content_type": file.content_type,
            "local_path": str(path),
            "download_url": f"{self.settings.base_url}/files/{quote(relative)}?sig={signature}",
        }

    def resolve_download(self, relative: str, signature: str | None) -> Path | None:
        if not self.access_key.verify_signature(relative, signature):
            return None
        root = self.settings.downloads_dir.resolve()
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            return None
        return path

    def log_submission(self, record: dict) -> None:
        with self.settings.submissions_log.open("a") as fh:
            fh.write(json.dumps({"logged_at": time.time(), **record}, default=str) + "\n")

    async def keepalive_once(self) -> None:
        """Touch each connected LMS so its inactivity timer never fires.

        A failed touch triggers the client's silent refresh; only if that fails too does the
        university flip to ``expired`` and wait for the user to reconnect.
        """
        for university_id in self.auth.connected_university_ids():
            try:
                await self.client(university_id).whoami()
            except ReauthRequired as exc:
                log.info("Keepalive: %s needs the user to reconnect (%s)", university_id, exc.reason)
            except (LMSError, NotImplementedError) as exc:
                log.warning("Keepalive for %s failed: %s", university_id, exc)
            except Exception:
                log.exception("Keepalive for %s crashed", university_id)

    async def keepalive_forever(self) -> None:
        interval = max(self.settings.keepalive_minutes, 1) * 60
        while True:
            await self.keepalive_once()
            await asyncio.sleep(interval)

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()
