"""Structured Forge Server errors (A81).

Every failure renders through the API as::

    {"error": {"code": "...", "message": "...", "request_id": "..."}}

Codes are stable across releases. Error details are server-generated
values (ids, versions, statuses) — never tracebacks, secrets, or raw
exception text from third parties.
"""
from __future__ import annotations

from typing import Any, Dict


class ServerError(Exception):
    """Base class: carries a stable code, HTTP status, and safe details."""

    code = "SERVER_ERROR"
    status = 500

    def __init__(self, message: str = "", **details: Any) -> None:
        super().__init__(message or self.code)
        self.details: Dict[str, Any] = dict(details)


class InvalidRequest(ServerError):
    code = "INVALID_REQUEST"
    status = 400


class AuthenticationRequired(ServerError):
    code = "AUTH_REQUIRED"
    status = 401


class PermissionDenied(ServerError):
    code = "PERMISSION_DENIED"
    status = 403


class PolicyDenied(PermissionDenied):
    """The active A33 permission profile forbids the operation."""

    code = "POLICY_DENIED"
    status = 403


class NotFound(ServerError):
    code = "NOT_FOUND"
    status = 404


class TaskNotFound(NotFound):
    code = "TASK_NOT_FOUND"
    status = 404


class ProjectNotFound(NotFound):
    code = "PROJECT_NOT_FOUND"
    status = 404


class ApprovalNotFound(NotFound):
    code = "APPROVAL_NOT_FOUND"
    status = 404


class SessionNotFound(NotFound):
    code = "SESSION_NOT_FOUND"
    status = 404


class Conflict(ServerError):
    code = "CONFLICT"
    status = 409


class VersionConflict(Conflict):
    """Optimistic-concurrency mismatch: the task changed underneath us."""

    code = "VERSION_CONFLICT"
    status = 409


class InvalidTransition(Conflict):
    """The requested lifecycle transition is not allowed from this status."""

    code = "INVALID_TRANSITION"
    status = 409


class ApprovalConflict(Conflict):
    code = "APPROVAL_CONFLICT"
    status = 409


class ApprovalExpired(Conflict):
    code = "APPROVAL_EXPIRED"
    status = 409


class RateLimited(ServerError):
    code = "RATE_LIMITED"
    status = 429
