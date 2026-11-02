from __future__ import annotations

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
from university_connector.lms.brightspace import BrightspaceClient, validate_brightspace_session
from university_connector.lms.canvas import CanvasClient, validate_canvas_session
from university_connector.universities import University


def create_lms_client(university: University, sessions) -> LMSClient:
    if university.lms_type == "brightspace":
        return BrightspaceClient(university, sessions)
    if university.lms_type == "canvas":
        return CanvasClient(university, sessions)
    raise ValueError(f"Unsupported LMS type: {university.lms_type}")


async def validate_session(university: University, session: SessionData) -> bool:
    if university.lms_type == "brightspace":
        return await validate_brightspace_session(university, session)
    if university.lms_type == "canvas":
        return await validate_canvas_session(university, session)
    return False


__all__ = [
    "Assignment",
    "AttachmentInfo",
    "Course",
    "DownloadedFile",
    "LMSClient",
    "LMSError",
    "SubmissionReceipt",
    "create_lms_client",
    "validate_session",
]
