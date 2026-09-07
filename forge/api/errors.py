"""Structured API errors (A34).

Every failure renders as::

    {"error": {"code": "...", "message": "...", "request_id": "..."}}

Codes are stable. Responses never contain tracebacks, secrets, or
internal paths.
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from forge.control.control_plane import ControlError


def error_body(code: str, message: str, request_id: str,
               details: dict[str, Any] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "error": {"code": code, "message": message,
                  "request_id": request_id},
    }
    if details:
        # Details are server-generated tokens (ids, versions) — never raw
        # exceptions or paths.
        body["error"]["details"] = {
            key: value for key, value in details.items()
            if isinstance(value, (str, int, float, bool)) or value is None
        }
    return body


def _request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", "") or "")


async def _control_error(request: Request, exc: ControlError) -> JSONResponse:
    safe_details = {
        key: value for key, value in exc.details.items()
        if isinstance(value, (str, int, float, bool)) or value is None
    }
    return JSONResponse(
        status_code=exc.status,
        content=error_body(exc.code, str(exc) or exc.code,
                           _request_id(request), safe_details or None))


async def _http_error(request: Request,
                      exc: StarletteHTTPException) -> JSONResponse:
    code = "NOT_FOUND" if exc.status_code == 404 else "INVALID_REQUEST"
    if exc.status_code == 413:
        code = "INVALID_REQUEST"
    message = "Not found." if exc.status_code == 404 else (
        str(exc.detail) if isinstance(exc.detail, str) else "Request failed.")
    return JSONResponse(
        status_code=exc.status_code,
        content=error_body(code, message, _request_id(request)))


async def _validation_error(request: Request,
                            exc: RequestValidationError) -> JSONResponse:
    # Schema-driven messages only; drop input values (may echo secrets).
    problems = []
    for item in exc.errors():
        location = ".".join(str(part) for part in item.get("loc", ()))
        problems.append(f"{location}: {item.get('msg', 'invalid')}")
    return JSONResponse(
        status_code=400,
        content=error_body("INVALID_REQUEST",
                           "; ".join(problems[:5]) or "Invalid request.",
                           _request_id(request)))


async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
    del exc  # never reflected to the client
    return JSONResponse(
        status_code=500,
        content=error_body("INTERNAL_ERROR",
                           "An unexpected error occurred.",
                           _request_id(request)))


def install_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ControlError, _control_error)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, _http_error)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, _validation_error)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, _unhandled)
