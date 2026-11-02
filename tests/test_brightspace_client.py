from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from conftest import FakeSessions, make_session
from university_connector.auth import ReauthRequired
from university_connector.lms import LMSError
from university_connector.lms.brightspace import (
    BrightspaceClient,
    _build_multipart_mixed,
    validate_brightspace_session,
)

LP, LE = "1.49", "1.85"
COURSE = 1001


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


NOW = datetime.now(timezone.utc)

FOLDERS = [
    {
        "Id": 11,
        "Name": "Homework 3",
        "DueDate": iso(NOW + timedelta(days=2)),
        "IsHidden": False,
        "SubmissionType": 0,
        "CustomInstructions": {"Text": "Solve problems 1-5.", "Html": "<p>Solve problems 1-5.</p>"},
        "Attachments": [{"FileId": 501, "FileName": "hw3.pdf", "Size": 1234}],
    },
    {"Id": 12, "Name": "Already done", "DueDate": iso(NOW + timedelta(days=1)), "IsHidden": False, "SubmissionType": 0},
    {"Id": 13, "Name": "Far future", "DueDate": iso(NOW + timedelta(days=40)), "IsHidden": False, "SubmissionType": 0},
    {"Id": 14, "Name": "Hidden", "DueDate": iso(NOW + timedelta(days=1)), "IsHidden": True, "SubmissionType": 0},
    {"Id": 15, "Name": "Paper only", "DueDate": iso(NOW + timedelta(days=3)), "IsHidden": False, "SubmissionType": 2},
]


class FakeD2L:
    def __init__(self, live_session: str = "live"):
        self.live_session = live_session
        self.requests: list[httpx.Request] = []
        self.submissions: dict[int, list[dict]] = {
            12: [{"Id": 900, "SubmissionDate": iso(NOW - timedelta(hours=3)), "Files": [{"FileName": "done.pdf"}]}]
        }

    def authed(self, request: httpx.Request) -> bool:
        return f"d2lSessionVal={self.live_session}" in request.headers.get("cookie", "")

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/d2l/api/versions/":
            return httpx.Response(200, json=[
                {"ProductCode": "lp", "LatestVersion": LP},
                {"ProductCode": "le", "LatestVersion": LE},
            ])
        if not self.authed(request):
            return httpx.Response(302, headers={"location": "/d2l/login?sessionExpired=1"})
        if path == f"/d2l/api/lp/{LP}/users/whoami":
            return httpx.Response(200, json={"Identifier": "42", "FirstName": "Jina", "LastName": "S", "UniqueName": "js1"})
        if path == f"/d2l/api/lp/{LP}/enrollments/myenrollments/":
            if request.url.params.get("bookmark") != "p2":
                return httpx.Response(200, json={
                    "PagingInfo": {"Bookmark": "p2", "HasMoreItems": True},
                    "Items": [{"OrgUnit": {"Id": COURSE, "Name": "CSC 4350 Software Engineering", "Code": "CSC4350"}, "Access": {"IsActive": True, "CanAccess": True}}],
                })
            return httpx.Response(200, json={
                "PagingInfo": {"Bookmark": None, "HasMoreItems": False},
                "Items": [{"OrgUnit": {"Id": 999, "Name": "Old Course", "Code": "OLD"}, "Access": {"IsActive": True, "CanAccess": True, "EndDate": iso(NOW - timedelta(days=60))}}],
            })
        prefix = f"/d2l/api/le/{LE}/{COURSE}/dropbox/folders/"
        if path == prefix:
            return httpx.Response(200, json=FOLDERS)
        if path.startswith(prefix):
            rest = path[len(prefix):].strip("/").split("/")
            folder_id = int(rest[0])
            folder = next(f for f in FOLDERS if f["Id"] == folder_id)
            if len(rest) == 1:
                return httpx.Response(200, json=folder)
            if rest[1:] == ["attachments", "501"]:
                return httpx.Response(200, content=b"%PDF-1.7 fake", headers={
                    "content-type": "application/pdf",
                    "content-disposition": 'attachment; filename="hw3.pdf"',
                })
            if rest[1:] == ["submissions", "mysubmissions"]:
                if request.method == "POST":
                    if request.headers.get("x-csrf-token") != "xsrf-123":
                        return httpx.Response(403, json={"error": "xsrf"})
                    self.submissions.setdefault(folder_id, []).append({
                        "Id": 777, "SubmissionDate": iso(NOW), "Files": [{"FileName": "answers.pdf"}],
                    })
                    return httpx.Response(200)
                return httpx.Response(200, json=[{"Submissions": self.submissions.get(folder_id, [])}])
        return httpx.Response(404, text=f"unhandled {path}")


@pytest.fixture
def d2l() -> FakeD2L:
    return FakeD2L()


def client_for(gsu, d2l, sessions) -> BrightspaceClient:
    return BrightspaceClient(gsu, sessions, transport=httpx.MockTransport(d2l))


async def test_list_courses_paginates_and_drops_ended_courses(gsu, d2l):
    client = client_for(gsu, d2l, FakeSessions(make_session()))
    courses = await client.list_courses()
    assert [c.id for c in courses] == [COURSE]
    assert courses[0].url == f"{gsu.base_url}/d2l/home/{COURSE}"


async def test_due_assignments_filters_window_hidden_and_submitted(gsu, d2l):
    client = client_for(gsu, d2l, FakeSessions(make_session()))
    due = await client.list_due_assignments(days_ahead=7)
    assert [a.name for a in due] == ["Homework 3", "Paper only"]
    hw = due[0]
    assert hw.course_name == "CSC 4350 Software Engineering"
    assert hw.instructions_text == "Solve problems 1-5."
    assert hw.attachments[0].filename == "hw3.pdf"

    with_done = await client.list_due_assignments(days_ahead=7, include_submitted=True)
    done = next(a for a in with_done if a.name == "Already done")
    assert done.submitted and done.last_submitted_at is not None


async def test_course_filter(gsu, d2l):
    client = client_for(gsu, d2l, FakeSessions(make_session()))
    assert await client.list_due_assignments(course_filter="csc4350")
    assert await client.list_due_assignments(course_filter="biology") == []


async def test_download_attachment(gsu, d2l):
    client = client_for(gsu, d2l, FakeSessions(make_session()))
    file = await client.download_assignment_attachment(COURSE, 11, 501)
    assert file.filename == "hw3.pdf"
    assert file.content.startswith(b"%PDF")


async def test_submit_sends_multipart_mixed_with_xsrf(gsu, d2l, tmp_path):
    answer = tmp_path / "answers.pdf"
    answer.write_bytes(b"%PDF my answers")
    client = client_for(gsu, d2l, FakeSessions(make_session()))
    receipt = await client.submit_assignment(COURSE, 11, answer, comment="Done!")

    post = next(r for r in d2l.requests if r.method == "POST")
    assert post.headers["content-type"].startswith("multipart/mixed; boundary=")
    body = post.content
    assert b'{"Text": "Done!", "Html": null}' in body
    assert b'filename="answers.pdf"' in body and b"%PDF my answers" in body
    assert body.index(b"application/json") < body.index(b"Content-Disposition")
    assert receipt.submission_id == 777 and receipt.files == ["answers.pdf"]


async def test_submit_rejects_non_file_assignments(gsu, d2l, tmp_path):
    answer = tmp_path / "a.txt"
    answer.write_text("x")
    client = client_for(gsu, d2l, FakeSessions(make_session()))
    with pytest.raises(LMSError, match="on_paper"):
        await client.submit_assignment(COURSE, 15, answer)
    assert not any(r.method == "POST" for r in d2l.requests)


async def test_expired_session_triggers_silent_refresh_and_retries(gsu, d2l):
    sessions = FakeSessions(make_session("stale"), refreshed=make_session("live"))
    client = client_for(gsu, d2l, sessions)
    me = await client.whoami()
    assert me["user_id"] == "42"
    assert sessions.refresh_calls == 1


async def test_failed_silent_refresh_raises_reauth(gsu, d2l):
    sessions = FakeSessions(make_session("stale"), refreshed=None)
    client = client_for(gsu, d2l, sessions)
    with pytest.raises(ReauthRequired):
        await client.list_courses()


async def test_permission_403_is_lms_error_not_reauth(gsu):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/d2l/api/versions/":
            return httpx.Response(200, json=[{"ProductCode": "lp", "LatestVersion": LP}, {"ProductCode": "le", "LatestVersion": LE}])
        if request.url.path.endswith("/users/whoami"):
            return httpx.Response(200, json={"Identifier": "42"})
        return httpx.Response(403, json={"error": "no permission"})

    sessions = FakeSessions(make_session())
    client = BrightspaceClient(gsu, sessions, transport=httpx.MockTransport(handler))
    with pytest.raises(LMSError) as exc:
        await client.list_assignments(type("C", (), {"id": COURSE, "name": "X"})())
    assert exc.value.status_code == 403
    assert sessions.refresh_calls == 0


async def test_rotated_cookies_are_persisted(gsu):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/d2l/api/versions/":
            return httpx.Response(200, json=[])
        return httpx.Response(
            200,
            json={"Identifier": "42"},
            headers={"set-cookie": "d2lSessionVal=rotated; Path=/; Secure; HttpOnly"},
        )

    sessions = FakeSessions(make_session())
    client = BrightspaceClient(gsu, sessions, transport=httpx.MockTransport(handler))
    await client.whoami()
    assert sessions.session.cookies_for_host(gsu.host)["d2lSessionVal"] == "rotated"
    assert sessions.saves >= 1


async def test_validate_session(gsu, d2l):
    transport = httpx.MockTransport(d2l)
    assert await validate_brightspace_session(gsu, make_session("live"), transport)
    assert not await validate_brightspace_session(gsu, make_session("stale"), transport)


def test_multipart_body_shape():
    body, content_type = _build_multipart_mixed("hi", 'we"ird.txt', b"data")
    boundary = content_type.split("boundary=")[1]
    assert body.startswith(f"--{boundary}\r\nContent-Type: application/json\r\n\r\n".encode())
    assert body.endswith(f"\r\n--{boundary}--\r\n".encode())
    assert json.loads(body.split(b"\r\n\r\n")[1].split(b"\r\n")[0]) == {"Text": "hi", "Html": None}
    assert b"filename=\"we'ird.txt\"" in body
