"""D2L Brightspace engine, using the Valence REST API with the user's browser session cookies."""

from __future__ import annotations

import asyncio
import json
import mimetypes
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

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
from university_connector.universities import University

FALLBACK_VERSIONS = {"lp": "1.45", "le": "1.82"}
COURSE_OFFERING_TYPE_ID = 3
# SUBMISSIONTYPE_T values from the Valence dropbox reference.
SUBMISSION_KINDS = {0: "file", 1: "text", 2: "on_paper", 3: "observed", 4: "file_or_text"}
FILE_SUBMISSION_TYPES = {0, 4}
_STATUS_CONCURRENCY = 6


class SessionSource(Protocol):
    def get_session(self, university_id: str) -> SessionData: ...
    def save_session(self, university_id: str, session: SessionData) -> None: ...
    async def refresh(self, university_id: str) -> SessionData: ...


def parse_d2l_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _cookie_header(session: SessionData, host: str) -> str:
    return "; ".join(f"{k}={v}" for k, v in session.cookies_for_host(host).items())


def _is_login_redirect(response: httpx.Response) -> bool:
    location = response.headers.get("location", "")
    return response.is_redirect and ("/d2l/login" in location or "/login" in location)


def _is_auth_failure(response: httpx.Response) -> bool:
    return response.status_code == 401 or _is_login_redirect(response)


async def _discover_versions(client: httpx.AsyncClient) -> dict[str, str]:
    versions = dict(FALLBACK_VERSIONS)
    try:
        response = await client.get("/d2l/api/versions/")
        if response.status_code == 200:
            for product in response.json():
                code = product.get("ProductCode")
                if code in versions and product.get("LatestVersion"):
                    versions[code] = product["LatestVersion"]
    except (httpx.HTTPError, ValueError):
        pass
    return versions


async def validate_brightspace_session(
    university: University,
    session: SessionData,
    transport: httpx.AsyncBaseTransport | None = None,
) -> bool:
    """True when the cookies belong to a signed-in user (``whoami`` succeeds)."""
    headers = {"Cookie": _cookie_header(session, university.host)}
    async with httpx.AsyncClient(
        base_url=university.base_url, headers=headers, timeout=20, transport=transport
    ) as client:
        versions = await _discover_versions(client)
        try:
            response = await client.get(f"/d2l/api/lp/{versions['lp']}/users/whoami")
        except httpx.HTTPError:
            return False
        if response.status_code != 200:
            return False
        try:
            return bool(response.json().get("Identifier"))
        except ValueError:
            return False


def _build_multipart_mixed(comment: str, filename: str, content: bytes) -> tuple[bytes, str]:
    boundary = f"uc{secrets.token_hex(16)}"
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    safe_name = filename.replace('"', "'").replace("\r", "").replace("\n", "")
    description = json.dumps({"Text": comment, "Html": None})
    body = b"".join(
        [
            f"--{boundary}\r\nContent-Type: application/json\r\n\r\n".encode(),
            description.encode(),
            f"\r\n--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name=""; filename="{safe_name}"\r\n'.encode(),
            f"Content-Type: {content_type}\r\n\r\n".encode(),
            content,
            f"\r\n--{boundary}--\r\n".encode(),
        ]
    )
    return body, f"multipart/mixed; boundary={boundary}"


def _filename_from_disposition(header: str | None) -> str | None:
    if not header:
        return None
    match = re.search(r"filename\*=UTF-8''([^;]+)", header, re.IGNORECASE)
    if match:
        from urllib.parse import unquote

        return unquote(match.group(1))
    match = re.search(r'filename="?([^";]+)"?', header, re.IGNORECASE)
    return match.group(1) if match else None


class BrightspaceClient(LMSClient):
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
        self._versions: dict[str, str] | None = None
        self._course_names: dict[int, str] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _version(self, product: str) -> str:
        if self._versions is None:
            self._sessions.get_session(self.university.id)  # fail fast when signed out
            self._versions = await _discover_versions(self._client)
        return self._versions[product]

    async def _lp(self, path: str) -> str:
        return f"/d2l/api/lp/{await self._version('lp')}{path}"

    async def _le(self, path: str) -> str:
        return f"/d2l/api/le/{await self._version('le')}{path}"

    async def _send(
        self, session: SessionData, method: str, path: str, **kwargs: Any
    ) -> httpx.Response:
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["Cookie"] = _cookie_header(session, self.university.host)
        if method != "GET" and session.xsrf_token:
            headers["X-Csrf-Token"] = session.xsrf_token
        try:
            return await self._client.request(method, path, headers=headers, **kwargs)
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

    async def _session_is_dead(self, session: SessionData) -> bool:
        response = await self._send(session, "GET", await self._lp("/users/whoami"))
        return response.status_code in (401, 403) or _is_login_redirect(response)

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        session = self._sessions.get_session(self.university.id)
        response = await self._send(session, method, path, **kwargs)

        expired = _is_auth_failure(response) or (
            response.status_code == 403 and await self._session_is_dead(session)
        )
        if expired:
            session = await self._sessions.refresh(self.university.id)
            response = await self._send(session, method, path, **kwargs)
            if _is_auth_failure(response):
                raise ReauthRequired(self.university.id, "LMS rejected the refreshed session")

        if response.status_code >= 400:
            raise LMSError(
                f"{method} {path} failed with HTTP {response.status_code}: {response.text[:300]}",
                response.status_code,
            )
        self._remember_rotated_cookies(session, response)
        return response

    async def _get_json(self, path: str, **params: Any) -> Any:
        response = await self._request("GET", path, params=params or None)
        return response.json()

    async def whoami(self) -> dict:
        data = await self._get_json(await self._lp("/users/whoami"))
        return {
            "user_id": data.get("Identifier"),
            "first_name": data.get("FirstName"),
            "last_name": data.get("LastName"),
            "username": data.get("UniqueName"),
        }

    async def list_courses(self) -> list[Course]:
        path = await self._lp("/enrollments/myenrollments/")
        now = datetime.now(timezone.utc)
        courses: list[Course] = []
        bookmark: str | None = None
        while True:
            params: dict[str, Any] = {
                "orgUnitTypeId": COURSE_OFFERING_TYPE_ID,
                "isActive": "true",
                "canAccess": "true",
            }
            if bookmark:
                params["bookmark"] = bookmark
            page = await self._get_json(path, **params)
            for item in page.get("Items", []):
                org_unit, access = item.get("OrgUnit", {}), item.get("Access", {})
                if not access.get("IsActive", True) or not access.get("CanAccess", True):
                    continue
                end = parse_d2l_datetime(access.get("EndDate"))
                if end and end < now:
                    continue
                course = Course(
                    id=org_unit["Id"],
                    name=org_unit.get("Name", ""),
                    code=org_unit.get("Code"),
                    url=f"{self.university.base_url}/d2l/home/{org_unit['Id']}",
                )
                self._course_names[course.id] = course.name
                courses.append(course)
            paging = page.get("PagingInfo") or {}
            if not paging.get("HasMoreItems"):
                break
            bookmark = paging.get("Bookmark")
        return courses

    def _to_assignment(self, course_id: int, folder: dict) -> Assignment:
        instructions = folder.get("CustomInstructions") or {}
        availability = folder.get("Availability") or {}
        due_at = parse_d2l_datetime(folder.get("DueDate")) or parse_d2l_datetime(
            availability.get("EndDate")
        )
        return Assignment(
            id=folder["Id"],
            course_id=course_id,
            course_name=self._course_names.get(course_id, str(course_id)),
            name=folder.get("Name", ""),
            due_at=due_at,
            instructions_text=(instructions.get("Text") or "").strip(),
            attachments=[
                AttachmentInfo(a["FileId"], a.get("FileName", f"file-{a['FileId']}"), a.get("Size"))
                for a in folder.get("Attachments") or []
            ],
            submission_kind=SUBMISSION_KINDS.get(folder.get("SubmissionType"), "file"),
            url=(
                f"{self.university.base_url}/d2l/lms/dropbox/user/folder_submit_files.d2l"
                f"?db={folder['Id']}&ou={course_id}"
            ),
        )

    async def list_assignments(self, course: Course) -> list[Assignment]:
        self._course_names[course.id] = course.name
        folders = await self._get_json(await self._le(f"/{course.id}/dropbox/folders/"))
        return [self._to_assignment(course.id, f) for f in folders if not f.get("IsHidden")]

    async def get_assignment(self, course_id: int, assignment_id: int) -> Assignment:
        folder = await self._get_json(await self._le(f"/{course_id}/dropbox/folders/{assignment_id}"))
        assignment = self._to_assignment(course_id, folder)
        await self.load_submission_status([assignment])
        return assignment

    async def _my_submissions(self, course_id: int, assignment_id: int) -> list[dict]:
        data = await self._get_json(
            await self._le(f"/{course_id}/dropbox/folders/{assignment_id}/submissions/mysubmissions/")
        )
        entities = data if isinstance(data, list) else [data]
        return [s for entity in entities for s in (entity or {}).get("Submissions") or []]

    async def load_submission_status(self, assignments: list[Assignment]) -> None:
        semaphore = asyncio.Semaphore(_STATUS_CONCURRENCY)

        async def fill(assignment: Assignment) -> None:
            async with semaphore:
                submissions = await self._my_submissions(assignment.course_id, assignment.id)
            dates = [parse_d2l_datetime(s.get("SubmissionDate")) for s in submissions]
            dates = [d for d in dates if d]
            assignment.submitted = bool(submissions)
            assignment.last_submitted_at = max(dates) if dates else None

        await asyncio.gather(*(fill(a) for a in assignments))

    async def download_assignment_attachment(
        self, course_id: int, assignment_id: int, file_id: int
    ) -> DownloadedFile:
        response = await self._request(
            "GET",
            await self._le(f"/{course_id}/dropbox/folders/{assignment_id}/attachments/{file_id}"),
            headers={"Accept": "*/*"},
        )
        filename = _filename_from_disposition(response.headers.get("content-disposition"))
        if not filename:
            assignment = await self.get_assignment(course_id, assignment_id)
            match = next((a for a in assignment.attachments if a.file_id == file_id), None)
            filename = match.filename if match else f"attachment-{file_id}"
        return DownloadedFile(filename, response.content, response.headers.get("content-type"))

    async def _ensure_xsrf_token(self) -> None:
        session = self._sessions.get_session(self.university.id)
        if session.xsrf_token:
            return
        response = await self._send(session, "GET", "/d2l/lp/auth/xsrf-tokens")
        if response.status_code == 200:
            try:
                token = response.json().get("referrerToken")
            except ValueError:
                token = None
            if token:
                session.xsrf_token = token
                self._sessions.save_session(self.university.id, session)

    async def submit_assignment(
        self, course_id: int, assignment_id: int, file_path: Path, comment: str = ""
    ) -> SubmissionReceipt:
        folder = await self._get_json(await self._le(f"/{course_id}/dropbox/folders/{assignment_id}"))
        if folder.get("SubmissionType", 0) not in FILE_SUBMISSION_TYPES:
            kind = SUBMISSION_KINDS.get(folder.get("SubmissionType"), "unknown")
            raise LMSError(f"Assignment '{folder.get('Name')}' takes {kind} submissions, not files")

        body, content_type = _build_multipart_mixed(comment, file_path.name, file_path.read_bytes())
        await self._ensure_xsrf_token()
        await self._request(
            "POST",
            await self._le(f"/{course_id}/dropbox/folders/{assignment_id}/submissions/mysubmissions/"),
            content=body,
            headers={"Content-Type": content_type},
        )

        submissions = await self._my_submissions(course_id, assignment_id)
        latest = max(
            submissions,
            key=lambda s: parse_d2l_datetime(s.get("SubmissionDate"))
            or datetime.min.replace(tzinfo=timezone.utc),
            default=None,
        )
        return SubmissionReceipt(
            course_id=course_id,
            assignment_id=assignment_id,
            submission_id=latest.get("Id") if latest else None,
            submitted_at=parse_d2l_datetime(latest.get("SubmissionDate")) if latest else None,
            files=[f.get("FileName", "") for f in (latest or {}).get("Files") or []]
            or [file_path.name],
            comment=comment,
        )
