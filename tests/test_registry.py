from __future__ import annotations

import json

import pytest

from conftest import REPO_ROOT
from university_connector.universities.registry import RegistryError, UniversityRegistry


def write(tmp_path, name: str, **overrides) -> None:
    preset = {
        "id": "us_ga_example",
        "name": "Example University",
        "lms_type": "brightspace",
        "base_url": "https://example.brightspace.com",
        "login_url": "https://example.brightspace.com/d2l/login",
        "success_url_contains": "/d2l/home",
        "session_cookies": ["d2lSessionVal"],
    }
    preset.update(overrides)
    (tmp_path / name).write_text(json.dumps(preset))


def test_bundled_presets_are_valid():
    registry = UniversityRegistry.load([REPO_ROOT / "universities"])
    gsu = registry.get("us_ga_gastate")
    assert gsu.lms_type == "brightspace"
    assert gsu.host == "gastate.view.usg.edu"
    assert "initiate-login" in gsu.start_url


def test_templates_are_not_loaded_as_universities():
    registry = UniversityRegistry.load([REPO_ROOT / "universities"])
    assert all("<" not in u.id for u in registry.all())


def test_start_url_falls_back_to_login_url(tmp_path):
    write(tmp_path, "us_ga_example.json")
    university = UniversityRegistry.load([tmp_path]).get("us_ga_example")
    assert university.start_url == university.login_url


def test_filename_must_match_id(tmp_path):
    write(tmp_path, "wrong.json")
    with pytest.raises(RegistryError, match="must match its id"):
        UniversityRegistry.load([tmp_path])


@pytest.mark.parametrize(
    "overrides",
    [
        {"base_url": "http://insecure.example.com"},
        {"lms_type": "moodle"},
        {"session_cookies": []},
        {"id": "Not Valid"},
    ],
)
def test_invalid_presets_are_rejected(tmp_path, overrides):
    write(tmp_path, f"{overrides.get('id', 'us_ga_example')}.json", **overrides)
    with pytest.raises(RegistryError):
        UniversityRegistry.load([tmp_path])


def test_unknown_university_lists_options(tmp_path):
    write(tmp_path, "us_ga_example.json")
    with pytest.raises(RegistryError, match="us_ga_example"):
        UniversityRegistry.load([tmp_path]).get("us_xx_missing")
