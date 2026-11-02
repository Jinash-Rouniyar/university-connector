"""Instructure Canvas engine, using the Canvas REST API with the user's browser session cookies.

Untested against a live school so far; built from the Canvas API docs and covered by
mock-server tests.
"""

from __future__ import annotations

import html
import json
import mimetypes
import re
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

import httpx

from university_connector.auth.persistent_browser import ReauthRequired
from university_connector.auth.session_store import SessionData
from university_connector.lms.base import (
    Assignment,
    AttachmentInfo,
    Course,
    DownloadedFile,
    LMSClient,
    LMSError,
    SubmissionReceipt,
)
from university_connector.lms.brightspace import SessionSource, parse_d2l_datetime
from university_connector.universities import University

# Session-authenticated Canvas JSON is prefixed with this to defeat JSON hijacking.
_JSON_PREFIX = "while(1);"
_SUBMITTED_STATES = {"submitted", "graded", "pending_review"}
_FILE_LINK = re.compile(
    r"<a\b[^>]*?href=\"[^\"]*?/files/(\d+)[^\"]*\"[^>]*>(.*?)</a>", re.IGNORECASE | re.DOTALL
)
_MAX_REDIRECTS = 5

parse_canvas_datetime = parse_d2l_datetime


def _json(response: httpx.Response) -> Any:
    text = response.text
    if text.startswith(_JSON_PREFIX):
        text = text[len(_JSON_PREFIX):]
    return json.loads(text)


class _TextExtractor(HTMLParser):
    _BREAKS = {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in self._BREAKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_to_text(markup: str | None) -> str:
    if not markup:
        return ""
    extractor = _TextExtractor()
    extractor.feed(markup)
    text = "".join(extractor.parts)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _file_links(description: str | None) -> list[AttachmentInfo]:
    seen: dict[int, AttachmentInfo] = {}
    for file_id, label in _FILE_LINK.findall(description or ""):
        name = html.unescape(re.sub(r"<[^>]+>", "", label)).strip()
        seen.setdefault(int(file_id), AttachmentInfo(int(file_id), name or f"file-{file_id}"))
    return list(seen.values())


def _submission_kind(types: list[str]) -> str:
    if "online_upload" in types:
        return "file_or_text" if "online_text_entry" in types else "file"
    if "online_text_entry" in types:
        return "text"
    return types[0] if types else "none"


def _is_login_redirect(response: httpx.Response) -> bool:
    return response.is_redirect and "/login" in response.headers.get("location", "")


def _is_auth_failure(response: httpx.Response) -> bool:
    """Canvas answers 401 both for "not signed in" and "not allowed"; only the former is expiry."""
    if _is_login_redirect(response):
        return True
    if response.status_code != 401:
        return False
    try:
        return _json(response).get("status") != "unauthorized"
    except (ValueError, AttributeError):
        return True


async def validate_canvas_session(
    university: University,
    session: SessionData,
    transport: httpx.AsyncBaseTransport | None = None,
) -> bool:
    """True when the cookies belong to a signed-in user (``/users/self`` succeeds)."""
    async with httpx.AsyncClient(
        base_url=university.base_url,
        headers={"Cookie": session.cookie_header(university.host), "Accept": "application/json"},
        timeout=20,
        transport=transport,
    ) as client:
        try:
            response = await client.get("/api/v1/users/self")
            return response.status_code == 200 and bool(_json(response).get("id"))
        except (httpx.HTTPError, ValueError, AttributeError):
            return False


class CanvasClient(LMSClient):
    def __init__(
        self,
        university: University,
        sessions: SessionSource,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.university = university
        self._sessions = sessions
        self._client = httpx.AsyncClient(
            base_url=university.base_url,
            timeout=httpx.Timeout(30, read=120),
            follow_redirects=False,
            transport=transport,
            headers={"Accept": "application/json"},
        )
        self._course_names: dict[int, str] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    def _is_canvas_host(self, url: str) -> bool:
        host = urlsplit(url).hostname
        return host is None or host == self.university.host

    async def _send(
        self, session: SessionData, method: str, url: str, **kwargs: Any
    ) -> httpx.Response:
        headers = dict(kwargs.pop("headers", {}) or {})
        if self._is_canvas_host(url):
            headers["Cookie"] = session.cookie_header(self.university.host)
            csrf = session.cookies_for_host(self.university.host).get("_csrf_token")
            if method != "GET" and csrf:
                headers["X-CSRF-Token"] = unquote(csrf)
        try:
            return await self._client.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise LMSError(f"Could not reach {self.university.name}: {exc}") from exc

    def _remember_rotated_cookies(self, session: SessionData, response: httpx.Response) -> None:
        rotated = {name: value for name, value in response.cookies.items() if value}
        changed = bool(rotated) and session.update_host_cookies(self.university.host, rotated)
        now = time.time()
        stale = session.last_validated_at is None or now - session.last_validated_at > 60
        if changed or stale:
            session.last_validated_at = now
            self._sessions.save_session(self.university.id, session)

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        session = self._sessions.get_session(self.university.id)
        response = await self._send(session, method, url, **kwargs)
        if _is_auth_failure(response):
            session = await self._sessions.refresh(self.university.id)
            response = await self._send(session, method, url, **kwargs)
            if _is_auth_failure(response):
                raise ReauthRequired(self.university.id, "LMS rejected the refreshed session")
        if response.status_code >= 400:
            raise LMSError(
                f"{method} {url} failed with HTTP {response.status_code}: {response.text[:300]}",
                response.status_code,
            )
        self._remember_rotated_cookies(session, response)
        return response

    async def _get_json(self, url: str, **params: Any) -> Any:
        return _json(await self._request("GET", url, params=params or None))

    async def _get_all(self, url: str, **params: Any) -> list:
        items: list = []
        next_url: str | None = url
        query: dict[str, Any] | None = {"per_page": 100, **params}
        while next_url:
            response = await self._request("GET", next_url, params=query)
            items.extend(_json(response))
            next_url = response.links.get("next", {}).get("url")
            query = None  # the next link already carries the query string
        return items

    async def whoami(self) -> dict:
        data = await self._get_json("/api/v1/users/self")
        return {
            "user_id": data.get("id"),
            "name": data.get("name"),
            "short_name": data.get("short_name"),
            "username": data.get("login_id"),
        }

    async def list_courses(self) -> list[Course]:
        now = datetime.now(timezone.utc)
        courses: list[Course] = []
        for item in await self._get_all("/api/v1/courses", enrollment_state="active"):
            if item.get("access_restricted_by_date") or "name" not in item:
                continue
            end = parse_canvas_datetime(item.get("end_at"))
            if end and end < now:
                continue
            course = Course(
                id=item["id"],
                name=item["name"],
                code=item.get("course_code"),
                url=f"{self.university.base_url}/courses/{item['id']}",
            )
            self._course_names[course.id] = course.name
            courses.append(course)
        return courses

    def _to_assignment(self, course_id: int, item: dict) -> Assignment:
        submission = item.get("submission") or {}
        submitted_at = parse_canvas_datetime(submission.get("submitted_at"))
        return Assignment(
            id=item["id"],
            course_id=course_id,
            course_name=self._course_names.get(course_id, str(course_id)),
            name=item.get("name", ""),
            due_at=parse_canvas_datetime(item.get("due_at"))
            or parse_canvas_datetime(item.get("lock_at")),
            instructions_text=html_to_text(item.get("description")),
            attachments=_file_links(item.get("description")),
            submitted=bool(submitted_at) and submission.get("workflow_state") in _SUBMITTED_STATES,
            last_submitted_at=submitted_at,
            submission_kind=_submission_kind(item.get("submission_types") or []),
            url=item.get("html_url"),
        )

    async def list_assignments(self, course: Course) -> list[Assignment]:
        self._course_names[course.id] = course.name
        items = await self._get_all(
            f"/api/v1/courses/{course.id}/assignments", **{"include[]": "submission"}
        )
        return [self._to_assignment(course.id, i) for i in items if i.get("published", True)]

    async def get_assignment(self, course_id: int, assignment_id: int) -> Assignment:
        if course_id not in self._course_names:
            course = await self._get_json(f"/api/v1/courses/{course_id}")
            self._course_names[course_id] = course.get("name", str(course_id))
        item = await self._get_json(
            f"/api/v1/courses/{course_id}/assignments/{assignment_id}",
            **{"include[]": "submission"},
        )
        return self._to_assignment(course_id, item)

    async def _file_metadata(self, course_id: int, file_id: int) -> dict:
        try:
            return await self._get_json(f"/api/v1/courses/{course_id}/files/{file_id}")
        except LMSError as exc:
            if exc.status_code not in (401, 403, 404):
                raise
            return await self._get_json(f"/api/v1/files/{file_id}")

    async def _download(self, url: str) -> httpx.Response:
        """Follow redirects by hand so Canvas cookies never leak to file-storage hosts."""
        session = self._sessions.get_session(self.university.id)
        for _ in range(_MAX_REDIRECTS + 1):
            response = await self._send(session, "GET", url, headers={"Accept": "*/*"})
            if not response.is_redirect:
                if response.status_code >= 400:
                    raise LMSError(f"Download failed with HTTP {response.status_code}", response.status_code)
                return response
            url = urljoin(str(response.url), response.headers["location"])
        raise LMSError("Download redirected too many times")

    async def download_assignment_attachment(
        self, course_id: int, assignment_id: int, file_id: int
    ) -> DownloadedFile:
        metadata = await self._file_metadata(course_id, file_id)
        if not metadata.get("url"):
            raise LMSError("Canvas did not return a download link for this file (it may be locked)")
        response = await self._download(metadata["url"])
        return DownloadedFile(
            metadata.get("display_name") or metadata.get("filename") or f"file-{file_id}",
            response.content,
            metadata.get("content-type") or response.headers.get("content-type"),
        )

    async def _upload_file(self, course_id: int, assignment_id: int, file_path: Path) -> int:
        content = file_path.read_bytes()
        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        ticket = _json(
            await self._request(
                "POST",
                f"/api/v1/courses/{course_id}/assignments/{assignment_id}/submissions/self/files",
                data={"name": file_path.name, "size": str(len(content)), "content_type": content_type},
            )
        )
        upload_url, params = ticket["upload_url"], ticket.get("upload_params") or {}
        file_param = ticket.get("file_param") or "file"

        session = self._sessions.get_session(self.university.id)
        uploaded = await self._send(
            session,
            "POST",
            upload_url,
            data={k: str(v) for k, v in params.items()},
            files={file_param: (file_path.name, content, content_type)},
        )
        if uploaded.is_redirect:
            confirm = urljoin(upload_url, uploaded.headers["location"])
            uploaded = await self._request("GET", confirm)
        if uploaded.status_code >= 400:
            raise LMSError(f"File upload failed with HTTP {uploaded.status_code}", uploaded.status_code)
        body = _json(uploaded)
        if "id" not in body and body.get("location"):
            body = await self._get_json(body["location"])
        return int(body["id"])

    async def submit_assignment(
        self, course_id: int, assignment_id: int, file_path: Path, comment: str = ""
    ) -> SubmissionReceipt:
        item = await self._get_json(f"/api/v1/courses/{course_id}/assignments/{assignment_id}")
        types = item.get("submission_types") or []
        if "online_upload" not in types:
            raise LMSError(
                f"Assignment '{item.get('name')}' takes {_submission_kind(types)} submissions, not files"
            )
        allowed = [e.lower().lstrip(".") for e in item.get("allowed_extensions") or []]
        extension = file_path.suffix.lower().lstrip(".")
        if allowed and extension not in allowed:
            raise LMSError(f"Assignment only accepts: {', '.join(allowed)} (got .{extension})")

        file_id = await self._upload_file(course_id, assignment_id, file_path)
        form: dict[str, Any] = {
            "submission[submission_type]": "online_upload",
            "submission[file_ids][]": [str(file_id)],
        }
        if comment:
            form["comment[text_comment]"] = comment
        submission = _json(
            await self._request(
                "POST", f"/api/v1/courses/{course_id}/assignments/{assignment_id}/submissions", data=form
            )
        )
        return SubmissionReceipt(
            course_id=course_id,
            assignment_id=assignment_id,
            submission_id=submission.get("id"),
            submitted_at=parse_canvas_datetime(submission.get("submitted_at")),
            files=[
                a.get("display_name") or a.get("filename", "")
                for a in submission.get("attachments") or []
            ]
            or [file_path.name],
            comment=comment,
        )
