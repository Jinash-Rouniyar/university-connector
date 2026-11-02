from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


class LMSError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class Course:
    id: int
    name: str
    code: str | None = None
    url: str | None = None


@dataclass
class AttachmentInfo:
    file_id: int
    filename: str
    size: int | None = None


@dataclass
class Assignment:
    id: int
    course_id: int
    course_name: str
    name: str
    due_at: datetime | None
    instructions_text: str = ""
    attachments: list[AttachmentInfo] = field(default_factory=list)
    submitted: bool = False
    last_submitted_at: datetime | None = None
    submission_kind: str = "file"
    url: str | None = None


@dataclass
class DownloadedFile:
    filename: str
    content: bytes
    content_type: str | None = None


@dataclass
class SubmissionReceipt:
    course_id: int
    assignment_id: int
    submission_id: int | None
    submitted_at: datetime | None
    files: list[str]
    comment: str = ""


class LMSClient(ABC):
    """What every LMS engine must offer the MCP tools. IDs are the LMS's own numeric IDs."""

    @abstractmethod
    async def whoami(self) -> dict: ...

    @abstractmethod
    async def list_courses(self) -> list[Course]: ...

    @abstractmethod
    async def list_assignments(self, course: Course) -> list[Assignment]: ...

    async def load_submission_status(self, assignments: list[Assignment]) -> None:
        """Fill ``submitted``/``last_submitted_at``; engines override when it costs extra calls."""
        return None

    async def list_due_assignments(
        self, days_ahead: int = 14, course_filter: str | None = None, include_submitted: bool = False
    ) -> list[Assignment]:
        from datetime import timedelta, timezone

        now = datetime.now(timezone.utc)
        horizon = now + timedelta(days=days_ahead)
        due: list[Assignment] = []
        for course in await self.list_courses():
            if course_filter and course_filter.lower() not in f"{course.name} {course.code}".lower():
                continue
            for assignment in await self.list_assignments(course):
                if assignment.due_at is not None and now <= assignment.due_at <= horizon:
                    due.append(assignment)
        await self.load_submission_status(due)
        if not include_submitted:
            due = [a for a in due if not a.submitted]
        return sorted(due, key=lambda a: a.due_at)

    @abstractmethod
    async def get_assignment(self, course_id: int, assignment_id: int) -> Assignment: ...

    @abstractmethod
    async def download_assignment_attachment(
        self, course_id: int, assignment_id: int, file_id: int
    ) -> DownloadedFile: ...

    @abstractmethod
    async def submit_assignment(
        self, course_id: int, assignment_id: int, file_path: Path, comment: str = ""
    ) -> SubmissionReceipt: ...

    async def aclose(self) -> None:
        return None
