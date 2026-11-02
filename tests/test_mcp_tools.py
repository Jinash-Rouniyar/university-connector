from __future__ import annotations

import base64
import json

import httpx
import pytest
from fastmcp import Client

from conftest import make_session
from test_brightspace_client import COURSE, FakeD2L
from university_connector.lms.brightspace import BrightspaceClient
from university_connector.mcp.server import create_mcp
from university_connector.runtime import Runtime

UID = "us_ga_gastate"


@pytest.fixture
def d2l() -> FakeD2L:
    return FakeD2L()


@pytest.fixture
def runtime(settings, registry, d2l) -> Runtime:
    runtime = Runtime(settings, registry)
    runtime._clients[UID] = BrightspaceClient(
        registry.get(UID), runtime.auth, transport=httpx.MockTransport(d2l)
    )
    return runtime


async def call(runtime: Runtime, tool: str, **arguments) -> dict:
    async with Client(create_mcp(runtime)) as client:
        result = await client.call_tool(tool, arguments)
        return json.loads(result.content[0].text)


async def test_not_connected_returns_reconnect_url(runtime):
    result = await call(runtime, "get_due_assignments", university_id=UID)
    assert result["error"] == "reauth_required"
    assert result["reconnect_url"].startswith("http://127.0.0.1:8787/auth/reconnect?university=us_ga_gastate&key=")
    assert runtime.access_key.value in result["reconnect_url"]


async def test_expired_flag_short_circuits_without_hitting_lms(runtime, d2l):
    session = make_session()
    session.needs_reauth = True
    runtime.store.save(UID, session)
    result = await call(runtime, "list_courses")
    assert result["error"] == "reauth_required"
    assert d2l.requests == []


async def test_due_assignments_when_connected(runtime):
    runtime.store.save(UID, make_session())
    result = await call(runtime, "get_due_assignments", days_ahead=7)
    names = [a["name"] for a in result["assignments"]]
    assert names == ["Homework 3", "Paper only"]
    assert result["assignments"][0]["hours_until_due"] > 0


async def test_download_returns_signed_link_that_resolves(runtime):
    runtime.store.save(UID, make_session())
    result = await call(
        runtime, "download_assignment_files", course_id=COURSE, assignment_id=11, include_content=True
    )
    file = result["files"][0]
    assert file["filename"] == "hw3.pdf"
    assert base64.b64decode(file["content_base64"]).startswith(b"%PDF")
    relative, sig = file["download_url"].split("/files/")[1].split("?sig=")
    assert runtime.resolve_download(relative, sig).read_bytes().startswith(b"%PDF")
    assert runtime.resolve_download(relative, "forged") is None


async def test_submit_requires_confirmation_then_submits_and_logs(runtime, d2l):
    runtime.store.save(UID, make_session())
    payload = base64.b64encode(b"%PDF answers").decode()
    args = dict(course_id=COURSE, assignment_id=11, file_base64=payload, filename="answers.pdf")

    preview = await call(runtime, "submit_assignment", **args)
    assert preview["status"] == "needs_confirmation"
    assert preview["preview"]["assignment"] == "Homework 3"
    assert not any(r.method == "POST" for r in d2l.requests)

    done = await call(runtime, "submit_assignment", confirm=True, comment="via agent", **args)
    assert done["status"] == "submitted"
    assert done["submission_id"] == 777
    log_lines = runtime.settings.submissions_log.read_text().splitlines()
    assert json.loads(log_lines[-1])["assignment"] == "Homework 3"


async def test_submit_refuses_connector_secrets(runtime):
    runtime.store.save(UID, make_session())
    secret = runtime.settings.home / "access-key"
    result = await call(
        runtime, "submit_assignment", course_id=COURSE, assignment_id=11, file_path=str(secret), confirm=True
    )
    assert result["error"] == "lms_error"
    assert "credential" in result["message"]


async def test_submit_needs_exactly_one_file_source(runtime):
    runtime.store.save(UID, make_session())
    result = await call(runtime, "submit_assignment", course_id=COURSE, assignment_id=11)
    assert result["error"] == "lms_error"
    assert "exactly one" in result["message"]


async def test_unknown_university(runtime):
    result = await call(runtime, "get_auth_status", university_id="us_xx_nowhere")
    assert result["error"] == "unknown_university"
