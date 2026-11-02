from __future__ import annotations

from pathlib import Path

import pytest

from university_connector.auth import ReauthRequired, SessionData
from university_connector.config import Settings
from university_connector.universities import University, UniversityRegistry

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        home=tmp_path / "uc-home",
        host="127.0.0.1",
        port=8787,
        public_url=None,
        extra_allowed_hosts=[],
        use_keyring=False,
    )


@pytest.fixture
def registry() -> UniversityRegistry:
    return UniversityRegistry.load([REPO_ROOT / "universities"])


@pytest.fixture
def gsu(registry: UniversityRegistry) -> University:
    return registry.get("us_ga_gastate")


def make_session(value: str = "live", xsrf: str | None = "xsrf-123") -> SessionData:
    return SessionData(
        cookies=[
            {"name": "d2lSessionVal", "value": value, "domain": "gastate.view.usg.edu", "path": "/"},
            {"name": "d2lSecureSessionVal", "value": f"secure-{value}", "domain": "gastate.view.usg.edu", "path": "/"},
            {"name": "shib_idp_session", "value": "idp", "domain": "idp.gsu.edu", "path": "/"},
        ],
        xsrf_token=xsrf,
    )


class FakeSessions:
    """Stands in for AuthManager: hands out a session and counts refreshes."""

    def __init__(self, session: SessionData | None, refreshed: SessionData | None = None):
        self.session = session
        self.refreshed = refreshed
        self.refresh_calls = 0
        self.saves = 0

    def get_session(self, university_id: str) -> SessionData:
        if self.session is None:
            raise ReauthRequired(university_id, "not connected")
        return self.session

    def save_session(self, university_id: str, session: SessionData) -> None:
        self.saves += 1
        self.session = session

    async def refresh(self, university_id: str) -> SessionData:
        self.refresh_calls += 1
        if self.refreshed is None:
            raise ReauthRequired(university_id, "silent refresh failed")
        self.session = self.refreshed
        return self.refreshed
