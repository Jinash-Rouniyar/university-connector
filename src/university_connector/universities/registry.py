from __future__ import annotations

import json
import os
from importlib import resources
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, ValidationError, field_validator

LMSType = Literal["brightspace", "canvas"]


class University(BaseModel):
    id: str = Field(pattern=r"^[a-z]{2}_[a-z0-9]+_[a-z0-9_]+$")
    name: str
    lms_type: LMSType
    base_url: str
    login_url: str
    # Starts the school's SSO directly, skipping the LMS landing page. Silent refresh
    # relies on it: a live IdP session + remembered MFA device completes with no clicks.
    sso_login_url: str | None = None
    success_url_contains: str
    session_cookies: list[str] = Field(min_length=1)
    notes: str = ""

    @field_validator("base_url", "login_url", "sso_login_url")
    @classmethod
    def _require_https(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parts = urlsplit(value)
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError("must be an https:// URL with a host")
        return value.rstrip("/")

    @property
    def host(self) -> str:
        return urlsplit(self.base_url).hostname or ""

    @property
    def start_url(self) -> str:
        return self.sso_login_url or self.login_url


class RegistryError(Exception):
    pass


def _default_preset_dirs() -> list[Path]:
    dirs: list[Path] = []
    if env_dir := os.environ.get("UC_UNIVERSITIES_DIR"):
        dirs.append(Path(env_dir).expanduser())
    # Editable/source checkout: <repo>/universities
    repo_dir = Path(__file__).resolve().parents[3] / "universities"
    if repo_dir.is_dir():
        dirs.append(repo_dir)
    # Installed wheel: presets are force-included into the package.
    packaged = resources.files("university_connector") / "presets"
    if packaged.is_dir():
        dirs.append(Path(str(packaged)))
    return dirs


class UniversityRegistry:
    def __init__(self, universities: dict[str, University]):
        self._universities = universities

    @classmethod
    def load(cls, preset_dirs: list[Path] | None = None) -> UniversityRegistry:
        """Load every top-level ``*.json`` preset; the first directory wins on duplicate ids."""
        universities: dict[str, University] = {}
        for directory in preset_dirs if preset_dirs is not None else _default_preset_dirs():
            for path in sorted(directory.glob("*.json")):
                try:
                    university = University.model_validate(json.loads(path.read_text()))
                except (json.JSONDecodeError, ValidationError) as exc:
                    raise RegistryError(f"Invalid university preset {path}: {exc}") from exc
                if path.stem != university.id:
                    raise RegistryError(
                        f"Preset filename {path.name} must match its id ({university.id}.json)"
                    )
                universities.setdefault(university.id, university)
        return cls(universities)

    def all(self) -> list[University]:
        return sorted(self._universities.values(), key=lambda u: u.name)

    def get(self, university_id: str) -> University:
        try:
            return self._universities[university_id]
        except KeyError:
            known = ", ".join(sorted(self._universities)) or "none"
            raise RegistryError(
                f"Unknown university '{university_id}'. Known universities: {known}"
            ) from None

    def __contains__(self, university_id: str) -> bool:
        return university_id in self._universities

    def __len__(self) -> int:
        return len(self._universities)
