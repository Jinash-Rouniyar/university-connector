from __future__ import annotations

import base64
import binascii
import functools
import tempfile
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastmcp import FastMCP

from university_connector.auth import ReauthRequired
from university_connector.lms import Assignment, LMSError
from university_connector.runtime import Runtime
from university_connector.universities.registry import RegistryError

MAX_SUBMISSION_BYTES = 200 * 1024 * 1024

INSTRUCTIONS = """\
Tools for the user's university learning management system (LMS): list courses, find what is
due, download assignment files, and submit finished work.

- If a tool returns error "reauth_required", send the user the reconnect_url (or call
  start_login) and wait for them to sign in; never ask for their password in chat.
- submit_assignment is irreversible from the student's side. Call it first with confirm=false to
  get a preview, show that preview to the user, and only call again with confirm=true after the
  user explicitly approves.
- university_id may be omitted when only one university is connected.
"""

READ_ONLY = {"readOnlyHint": True, "openWorldHint": True}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _assignment_dict(assignment: Assignment, include_instructions: bool = False) -> dict:
    now = datetime.now(timezone.utc)
    data: dict[str, Any] = {
        "course_id": assignment.course_id,
        "course_name": assignment.course_name,
        "assignment_id": assignment.id,
        "name": assignment.name,
        "due_at": _iso(assignment.due_at),
        "due_local": assignment.due_at.astimezone().strftime("%a %b %d, %I:%M %p %Z")
        if assignment.due_at
        else None,
        "hours_until_due": round((assignment.due_at - now).total_seconds() / 3600, 1)
        if assignment.due_at
        else None,
        "submitted": assignment.submitted,
        "last_submitted_at": _iso(assignment.last_submitted_at),
        "submission_kind": assignment.submission_kind,
        "attachments": [
            {"file_id": a.file_id, "filename": a.filename, "size_bytes": a.size}
            for a in assignment.attachments
        ],
        "url": assignment.url,
    }
    if include_instructions:
        data["instructions"] = assignment.instructions_text
    return data


def create_mcp(runtime: Runtime) -> FastMCP:
    mcp = FastMCP("University Connector", instructions=INSTRUCTIONS)

    def reauth_payload(exc: ReauthRequired) -> dict:
        return {
            "error": "reauth_required",
            "university_id": exc.university_id,
            "message": f"{exc.reason}. Ask the user to open reconnect_url and sign in.",
            "reconnect_url": runtime.auth.reconnect_url(exc.university_id),
        }

    def guarded(fn: Callable[..., Awaitable[dict]]) -> Callable[..., Awaitable[dict]]:
        """Turn expected failures into structured results the agent can act on."""

        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> dict:
            try:
                return await fn(*args, **kwargs)
            except ReauthRequired as exc:
                return reauth_payload(exc)
            except RegistryError as exc:
                return {"error": "unknown_university", "message": str(exc)}
            except NotImplementedError as exc:
                return {"error": "not_supported", "message": str(exc)}
            except LMSError as exc:
                return {"error": "lms_error", "status_code": exc.status_code, "message": str(exc)}

        return wrapper

    @mcp.tool(annotations=READ_ONLY)
    @guarded
    async def list_universities() -> dict:
        """List supported universities and whether each one is connected."""
        return {"universities": [runtime.auth.status(u.id) for u in runtime.registry.all()]}

    @mcp.tool(annotations=READ_ONLY)
    @guarded
    async def get_auth_status(university_id: str | None = None) -> dict:
        """Check whether the connector is signed in to a university's LMS."""
        university = runtime.resolve_university(university_id)
        return runtime.auth.status(university.id)

    @mcp.tool(annotations=READ_ONLY)
    @guarded
    async def get_reconnect_url(university_id: str | None = None) -> dict:
        """Get a link the user can open to sign in again. Opening it starts the sign-in window."""
        university = runtime.resolve_university(university_id)
        return {"university_id": university.id, "reconnect_url": runtime.auth.reconnect_url(university.id)}

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False})
    @guarded
    async def start_login(university_id: str | None = None) -> dict:
        """Open the university sign-in window on the computer running the connector.

        Use when the user is at that computer. The user completes CampusID/SSO and MFA in that
        window; poll get_auth_status until state is "connected".
        """
        university = runtime.resolve_university(university_id)
        runtime.auth.start_login(university.id)
        return runtime.auth.status(university.id)

    @mcp.tool(annotations=READ_ONLY)
    @guarded
    async def list_courses(university_id: str | None = None) -> dict:
        """List the user's current courses (course_id is needed by the other tools)."""
        university = runtime.resolve_university(university_id)
        courses = await runtime.client(university.id).list_courses()
        return {
            "university_id": university.id,
            "courses": [
                {"course_id": c.id, "name": c.name, "code": c.code, "url": c.url} for c in courses
            ],
        }

    @mcp.tool(annotations=READ_ONLY)
    @guarded
    async def get_due_assignments(
        university_id: str | None = None,
        days_ahead: int = 7,
        course_name: str | None = None,
        include_submitted: bool = False,
    ) -> dict:
        """List assignments due in the next `days_ahead` days, soonest first.

        course_name filters by a case-insensitive substring of the course name or code.
        Already-submitted work is hidden unless include_submitted is true.
        """
        university = runtime.resolve_university(university_id)
        assignments = await runtime.client(university.id).list_due_assignments(
            days_ahead=days_ahead, course_filter=course_name, include_submitted=include_submitted
        )
        return {
            "university_id": university.id,
            "days_ahead": days_ahead,
            "assignments": [_assignment_dict(a) for a in assignments],
        }

    @mcp.tool(annotations=READ_ONLY)
    @guarded
    async def get_assignment_details(
        course_id: int, assignment_id: int, university_id: str | None = None
    ) -> dict:
        """Get an assignment's full instructions, attachments, due date, and submission status."""
        university = runtime.resolve_university(university_id)
        assignment = await runtime.client(university.id).get_assignment(course_id, assignment_id)
        return {"university_id": university.id, **_assignment_dict(assignment, include_instructions=True)}

    @mcp.tool(annotations=READ_ONLY)
    @guarded
    async def download_assignment_files(
        course_id: int,
        assignment_id: int,
        university_id: str | None = None,
        file_id: int | None = None,
        include_content: bool = False,
    ) -> dict:
        """Download the instructor's attachments for an assignment (all, or just file_id).

        Each file comes back with a download_url the user can open on their phone. Set
        include_content=true to also receive small files (up to 5 MB) inline as base64.
        """
        university = runtime.resolve_university(university_id)
        client = runtime.client(university.id)
        assignment = await client.get_assignment(course_id, assignment_id)
        targets = [a for a in assignment.attachments if file_id is None or a.file_id == file_id]
        if not targets:
            return {
                "university_id": university.id,
                "assignment": assignment.name,
                "files": [],
                "message": "This assignment has no instructor attachments"
                if file_id is None
                else f"No attachment with file_id {file_id}",
            }
        files = []
        for attachment in targets:
            downloaded = await client.download_assignment_attachment(
                course_id, assignment_id, attachment.file_id
            )
            info = runtime.save_download(university.id, course_id, assignment_id, downloaded)
            limit = runtime.settings.inline_download_limit_bytes
            if include_content and len(downloaded.content) <= limit:
                info["content_base64"] = base64.b64encode(downloaded.content).decode()
            files.append(info)
        return {"university_id": university.id, "assignment": assignment.name, "files": files}

    async def _materialize_file(
        file_path: str | None, file_base64: str | None, filename: str | None, file_url: str | None
    ) -> Path:
        provided = [x for x in (file_path, file_base64, file_url) if x]
        if len(provided) != 1:
            raise LMSError("Provide exactly one of file_path, file_base64 (+ filename), or file_url")

        if file_path:
            path = Path(file_path).expanduser().resolve()
            home = runtime.settings.home.resolve()
            if path.is_relative_to(home) and not path.is_relative_to(
                runtime.settings.downloads_dir.resolve()
            ):
                raise LMSError("Refusing to submit connector credential files")
            if not path.is_file():
                raise LMSError(f"File not found: {file_path}")
            return path

        if file_base64:
            if not filename:
                raise LMSError("filename is required with file_base64")
            try:
                content = base64.b64decode(file_base64, validate=True)
            except binascii.Error as exc:
                raise LMSError(f"file_base64 is not valid base64: {exc}") from exc
        else:
            async with httpx.AsyncClient(follow_redirects=True, timeout=120) as http:
                response = await http.get(file_url)
            if response.status_code >= 400:
                raise LMSError(f"Could not fetch file_url (HTTP {response.status_code})")
            content = response.content
            filename = filename or Path(httpx.URL(file_url).path).name or "submission"

        if len(content) > MAX_SUBMISSION_BYTES:
            raise LMSError("File is larger than 200 MB")
        staging = Path(tempfile.mkdtemp(prefix="uc-submit-"))
        path = staging / Path(filename).name
        path.write_bytes(content)
        return path

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True})
    @guarded
    async def submit_assignment(
        course_id: int,
        assignment_id: int,
        confirm: bool = False,
        university_id: str | None = None,
        file_path: str | None = None,
        file_base64: str | None = None,
        filename: str | None = None,
        file_url: str | None = None,
        comment: str = "",
    ) -> dict:
        """Submit a file to an assignment as the user.

        Supply the file one way: file_path (on the connector's computer), file_base64 with
        filename, or file_url (https link the connector can download). With confirm=false this
        only returns a preview; show it to the user and call again with confirm=true once they
        explicitly approve. Submissions cannot be withdrawn by the student.
        """
        university = runtime.resolve_university(university_id)
        client = runtime.client(university.id)
        assignment = await client.get_assignment(course_id, assignment_id)
        path = await _materialize_file(file_path, file_base64, filename, file_url)
        preview = {
            "university_id": university.id,
            "course_name": assignment.course_name,
            "assignment": assignment.name,
            "due_at": _iso(assignment.due_at),
            "already_submitted": assignment.submitted,
            "file": path.name,
            "size_bytes": path.stat().st_size,
            "comment": comment,
        }
        if not confirm:
            return {
                "status": "needs_confirmation",
                "message": "Show this preview to the user; call again with confirm=true only "
                "after they approve.",
                "preview": preview,
            }

        receipt = await client.submit_assignment(course_id, assignment_id, path, comment)
        result = {
            "status": "submitted",
            **preview,
            "submission_id": receipt.submission_id,
            "submitted_at": _iso(receipt.submitted_at),
            "files": receipt.files,
            "assignment_url": assignment.url,
        }
        runtime.log_submission(result)
        return result

    return mcp
