from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import httpx
import pytest

from conftest import FakeSessions
from university_connector.auth import ReauthRequired, SessionData
from university_connector.lms import LMSError
from university_connector.lms.canvas import CanvasClient, html_to_text, validate_canvas_session

HOST = "gatech.instructure.com"
COURSE = 4242
NOW = datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canvas_session(value: str = "live") -> SessionData:
    return SessionData(
        cookies=[
            {"name": "canvas_session", "value": value, "domain": HOST, "path": "/"},
            {"name": "_csrf_token", "value": "csrf%2Btoken%3D%3D", "domain": HOST, "path": "/"},
            {"name": "shib_idp_session", "value": "idp", "domain": "idp.gatech.edu", "path": "/"},
        ]
    )


ASSIGNMENTS = [
    {
        "id": 1,
        "name": "Lab 4",
        "due_at": iso(NOW + timedelta(days=2)),
        "published": True,
        "submission_types": ["online_upload"],
        "allowed_extensions": ["pdf", "zip"],
        "html_url": f"https://{HOST}/courses/{COURSE}/assignments/1",
        "description": (
            "<p>Implement the parser.</p><p>Starter: "
            f'<a class="instructure_file_link" href="https://{HOST}/courses/{COURSE}/files/7001?wrap=1">'
            "starter.zip</a></p>"
        ),
        "submission": {"workflow_state": "unsubmitted", "submitted_at": None},
    },
    {
        "id": 2,
        "name": "Quiz-ish essay",
        "due_at": iso(NOW + timedelta(days=1)),
        "published": True,
        "submission_types": ["online_text_entry"],
        "submission": {"workflow_state": "submitted", "submitted_at": iso(NOW - timedelta(hours=1))},
    },
    {"id": 3, "name": "Unpublished", "due_at": iso(NOW + timedelta(days=1)), "published": False, "submission_types": ["online_upload"]},
]


def j(data, status: int = 200, **kwargs) -> httpx.Response:
    return httpx.Response(status, text="while(1);" + json.dumps(data), **kwargs)


class FakeCanvas:
    def __init__(self, live: str = "live"):
        self.live = live
        self.requests: list[httpx.Request] = []
        self.submitted: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url, path = request.url, request.url.path

        if url.host == "files.instructure-uploads.com":
            assert "cookie" not in request.headers, "Canvas cookies leaked to storage host"
            if path == "/upload":
                return httpx.Response(201, json={"id": 9001, "display_name": "lab4.pdf"})
            return httpx.Response(200, content=b"PK starter zip", headers={"content-type": "application/zip"})

        if f"canvas_session={self.live}" not in request.headers.get("cookie", ""):
            return httpx.Response(401, json={"status": "unauthenticated", "errors": [{"message": "user authorization required"}]})

        if path == "/api/v1/users/self":
            return j({"id": 55, "name": "Buzz", "short_name": "Buzz", "login_id": "gburdell3"})
        if path == "/api/v1/courses":
            if url.params.get("page") != "2":
                return j(
                    [{"id": COURSE, "name": "CS 2340 Objects and Design", "course_code": "CS-2340"}],
                    headers={"link": f'<https://{HOST}/api/v1/courses?page=2&per_page=100>; rel="next"'},
                )
            return j([
                {"id": 1, "access_restricted_by_date": True},
                {"id": 9, "name": "Old", "course_code": "OLD", "end_at": iso(NOW - timedelta(days=90))},
            ])
        if path == f"/api/v1/courses/{COURSE}":
            return j({"id": COURSE, "name": "CS 2340 Objects and Design"})
        if path == f"/api/v1/courses/{COURSE}/assignments":
            return j(ASSIGNMENTS)
        if path.startswith(f"/api/v1/courses/{COURSE}/assignments/"):
            rest = path.split("/assignments/")[1].split("/")
            assignment = next(a for a in ASSIGNMENTS if a["id"] == int(rest[0]))
            if len(rest) == 1:
                return j(assignment)
            if request.headers.get("x-csrf-token") != "csrf+token==":
                return j({"status": "unauthorized"}, 401)
            if rest[1:] == ["submissions", "self", "files"]:
                return j({
                    "upload_url": "https://files.instructure-uploads.com/upload",
                    "upload_params": {"key": "abc", "policy": "xyz"},
                    "file_param": "file",
                })
            if rest[1:] == ["submissions"]:
                form = parse_qs(request.content.decode())
                self.submitted.append(form)
                return j({"id": 31337, "submitted_at": iso(NOW), "attachments": [{"display_name": "lab4.pdf"}]})
        if path == f"/api/v1/courses/{COURSE}/files/7001":
            return j({"id": 7001, "display_name": "starter.zip", "url": f"https://{HOST}/files/7001/download?verifier=v"})
        if path == "/files/7001/download":
            return httpx.Response(302, headers={"location": "https://files.instructure-uploads.com/blob/7001"})
        return httpx.Response(404, text=f"unhandled {request.method} {url}")


@pytest.fixture
def gatech(registry):
    return registry.get("us_ga_gatech")


@pytest.fixture
def canvas() -> FakeCanvas:
    return FakeCanvas()


def client_for(gatech, canvas, sessions) -> CanvasClient:
    return CanvasClient(gatech, sessions, transport=httpx.MockTransport(canvas))


async def test_list_courses_paginates_and_skips_restricted_and_ended(gatech, canvas):
    courses = await client_for(gatech, canvas, FakeSessions(canvas_session())).list_courses()
    assert [(c.id, c.code) for c in courses] == [(COURSE, "CS-2340")]
    assert courses[0].url == f"https://{HOST}/courses/{COURSE}"


async def test_due_assignments_use_included_submission_status(gatech, canvas):
    client = client_for(gatech, canvas, FakeSessions(canvas_session()))
    due = await client.list_due_assignments(days_ahead=7)
    assert [a.name for a in due] == ["Lab 4"]
    lab = due[0]
    assert lab.instructions_text.startswith("Implement the parser.")
    assert [(a.file_id, a.filename) for a in lab.attachments] == [(7001, "starter.zip")]
    assert lab.submission_kind == "file"

    everything = await client.list_due_assignments(days_ahead=7, include_submitted=True)
    essay = next(a for a in everything if a.name == "Quiz-ish essay")
    assert essay.submitted and essay.submission_kind == "text"
    assert "Unpublished" not in [a.name for a in everything]


async def test_download_follows_redirect_without_leaking_cookies(gatech, canvas):
    client = client_for(gatech, canvas, FakeSessions(canvas_session()))
    file = await client.download_assignment_attachment(COURSE, 1, 7001)
    assert file.filename == "starter.zip"
    assert file.content == b"PK starter zip"


async def test_submit_uploads_then_submits_with_csrf(gatech, canvas, tmp_path):
    answer = tmp_path / "lab4.pdf"
    answer.write_bytes(b"%PDF lab")
    client = client_for(gatech, canvas, FakeSessions(canvas_session()))
    receipt = await client.submit_assignment(COURSE, 1, answer, comment="done")

    upload = next(r for r in canvas.requests if r.url.path == "/upload")
    body = upload.content
    assert body.index(b'name="key"') < body.index(b'name="file"'), "storage needs params before the file"
    assert b"%PDF lab" in body

    form = canvas.submitted[-1]
    assert form["submission[submission_type]"] == ["online_upload"]
    assert form["submission[file_ids][]"] == ["9001"]
    assert form["comment[text_comment]"] == ["done"]
    assert receipt.submission_id == 31337 and receipt.files == ["lab4.pdf"]


async def test_submit_enforces_type_and_extension(gatech, canvas, tmp_path):
    client = client_for(gatech, canvas, FakeSessions(canvas_session()))
    wrong_ext = tmp_path / "lab4.docx"
    wrong_ext.write_text("x")
    with pytest.raises(LMSError, match="only accepts"):
        await client.submit_assignment(COURSE, 1, wrong_ext)
    with pytest.raises(LMSError, match="text submissions"):
        await client.submit_assignment(COURSE, 2, tmp_path / "lab4.docx")
    assert not any(r.method == "POST" for r in canvas.requests)


async def test_unauthenticated_triggers_refresh(gatech, canvas):
    sessions = FakeSessions(canvas_session("stale"), refreshed=canvas_session("live"))
    me = await client_for(gatech, canvas, sessions).whoami()
    assert me["username"] == "gburdell3"
    assert sessions.refresh_calls == 1


async def test_failed_refresh_raises_reauth(gatech, canvas):
    sessions = FakeSessions(canvas_session("stale"), refreshed=None)
    with pytest.raises(ReauthRequired):
        await client_for(gatech, canvas, sessions).list_courses()


async def test_permission_401_is_not_treated_as_expiry(gatech):
    def handler(request: httpx.Request) -> httpx.Response:
        return j({"status": "unauthorized", "errors": [{"message": "not allowed"}]}, 401)

    sessions = FakeSessions(canvas_session())
    client = CanvasClient(gatech, sessions, transport=httpx.MockTransport(handler))
    with pytest.raises(LMSError) as exc:
        await client.get_assignment(COURSE, 1)
    assert exc.value.status_code == 401
    assert sessions.refresh_calls == 0


async def test_validate_session(gatech, canvas):
    transport = httpx.MockTransport(canvas)
    assert await validate_canvas_session(gatech, canvas_session("live"), transport)
    assert not await validate_canvas_session(gatech, canvas_session("stale"), transport)


def test_html_to_text():
    assert html_to_text("<p>One &amp; two</p><ul><li>a</li><li>b</li></ul>") == "One & two\na\nb"
