from __future__ import annotations

import httpx
import pytest

from university_connector.auth.web_portal import create_app
from university_connector.lms import DownloadedFile
from university_connector.runtime import Runtime

UID = "us_ga_gastate"


@pytest.fixture
def runtime(settings, registry):
    return Runtime(settings, registry)


@pytest.fixture
async def http(runtime):
    app = create_app(runtime)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8787") as client:
        yield client


async def test_health_is_public(http):
    assert (await http.get("/healthz")).json() == {"ok": True}


async def test_everything_else_needs_the_key(http, runtime):
    assert (await http.get("/api/universities")).status_code == 401
    assert (await http.get("/", headers={"Accept": "text/html"})).status_code == 401
    assert (await http.post("/mcp")).status_code == 401

    bearer = {"Authorization": f"Bearer {runtime.access_key.value}"}
    assert (await http.get("/api/universities", headers=bearer)).status_code == 200


async def test_key_in_query_sets_cookie_for_later_requests(http, runtime):
    first = await http.get(f"/?key={runtime.access_key.value}")
    assert first.status_code == 200
    assert "Georgia State University" in first.text
    assert "uc_key" in first.headers["set-cookie"]

    statuses = await http.get("/api/universities")
    assert statuses.status_code == 200
    assert statuses.json()[0]["state"] == "not_connected"


async def test_unknown_university_is_404(http, runtime):
    response = await http.get(
        "/auth/status", params={"university": "us_xx_nowhere", "key": runtime.access_key.value}
    )
    assert response.status_code == 404


async def test_signed_file_links_work_without_key(http, runtime):
    info = runtime.save_download(UID, 1, 2, DownloadedFile("notes.pdf", b"%PDF hi"))
    path = info["download_url"].removeprefix(runtime.settings.base_url)
    response = await http.get(path)
    assert response.status_code == 200 and response.content == b"%PDF hi"

    tampered = path.replace("notes.pdf", "other.pdf")
    assert (await http.get(tampered)).status_code == 404


async def test_traversal_outside_downloads_is_refused(runtime):
    relative = "../access-key"
    assert runtime.resolve_download(relative, runtime.access_key.sign(relative)) is None
