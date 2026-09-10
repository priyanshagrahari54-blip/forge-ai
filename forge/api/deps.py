"""API dependencies: auth, CSRF, pagination, rate limits (A34).

- Authentication: session token via ``Authorization: Bearer`` or the
  HttpOnly ``forge_session`` cookie. Unknown/expired/revoked tokens fail
  closed with 401.
- CSRF: cookie-authenticated mutations must carry
  ``X-Requested-With: forge-cockpit``. Bearer-authenticated requests need
  no CSRF token (no ambient credentials).
- Rate limits: in-memory token buckets, bounded key space.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

from fastapi import Depends, Request

from forge.control.control_plane import ControlError, ControlPlane
from forge.control.sessions import Session

SESSION_COOKIE = "forge_session"
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "forge-cockpit"


class AuthRequired(ControlError):
    code = "AUTH_REQUIRED"
    status = 401

    def __init__(self) -> None:
        super().__init__("Authentication required.")


class CSRFRequired(ControlError):
    code = "CSRF_REQUIRED"
    status = 403

    def __init__(self) -> None:
        super().__init__(
            "Cookie-authenticated mutations must carry the "
            "X-Requested-With: forge-cockpit header.")


class RateLimited(ControlError):
    code = "RATE_LIMITED"
    status = 429

    def __init__(self) -> None:
        super().__init__("Rate limit exceeded; retry shortly.")


@dataclass(frozen=True)
class Authed:
    session: Session
    via_cookie: bool


def get_plane(request: Request) -> ControlPlane:
    return request.app.state.plane


def _bearer_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    return ""


async def authed(request: Request,
                 plane: ControlPlane = Depends(get_plane)) -> Authed:
    token = _bearer_token(request)
    via_cookie = False
    if not token:
        token = request.cookies.get(SESSION_COOKIE, "")
        via_cookie = bool(token)
    session = plane.sessions.get_by_token(token) if token else None
    if session is None or not session.active:
        raise AuthRequired()
    plane.sessions.touch(session.id)
    # Re-read so callers see fresh timestamps (cheap indexed lookup).
    fresh = plane.sessions.get(session.id)
    return Authed(session=fresh or session, via_cookie=via_cookie)


async def authed_mutation(request: Request,
                          authed_value: Authed = Depends(authed)) -> Authed:
    if authed_value.via_cookie:
        if request.headers.get(CSRF_HEADER, "") != CSRF_VALUE:
            raise CSRFRequired()
    return authed_value


@dataclass
class Bucket:
    tokens: float
    updated: float


class RateLimiter:
    """Token-bucket limiter with a bounded key space."""

    #: (max tokens, refill per second) per group.
    GROUPS = {
        "default": (120.0, 2.0),
        "sessions": (20.0, 20.0 / 60.0),
        "task_create": (10.0, 10.0 / 60.0),
        "approvals": (30.0, 30.0 / 60.0),
        "events": (120.0, 2.0),
        "stream": (10.0, 10.0 / 60.0),
        "voice": (30.0, 30.0 / 60.0),
        "memory": (60.0, 60.0 / 60.0),
    }
    MAX_KEYS = 10000

    def __init__(self) -> None:
        self._buckets: OrderedDict[str, Bucket] = OrderedDict()
        self._lock = threading.Lock()

    def check(self, group: str, key: str) -> None:
        capacity, refill = self.GROUPS.get(group, self.GROUPS["default"])
        composite = f"{group}:{key}"
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(composite)
            if bucket is None:
                if len(self._buckets) >= self.MAX_KEYS:
                    self._buckets.popitem(last=False)
                self._buckets[composite] = Bucket(capacity - 1.0, now)
                return
            tokens = min(capacity,
                         bucket.tokens + (now - bucket.updated) * refill)
            if tokens < 1.0:
                self._buckets[composite] = Bucket(tokens, now)
                raise RateLimited()
            self._buckets[composite] = Bucket(tokens - 1.0, now)


def rate_limit(group: str):
    async def dependency(request: Request) -> None:
        limiter: RateLimiter = request.app.state.limiter
        client = request.client.host if request.client else "unknown"
        limiter.check(group, client)

    return Depends(dependency)


def pagination_params(limit: int = 50, offset: int = 0) -> dict[str, int]:
    return {"limit": max(1, min(200, limit)),
            "offset": max(0, min(100000, offset))}
