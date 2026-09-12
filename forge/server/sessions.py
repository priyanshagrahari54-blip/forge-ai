"""Persistent client sessions (A81).

A session lets a Forge Desktop client disconnect and reconnect without
re-authenticating from scratch: the session token (presented as
``Authorization: Bearer``) is stored hashed, bound to a principal name,
role, and scope set, and survives server restarts. Only the hash is
persisted — a database leak never yields a usable token.

This is explicitly a local-development authentication foundation (no
passwords, no external identity provider). Production deployments must
front the Forge Server with real authentication; the run banner and the
health report say so.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from forge.server.errors import Conflict, InvalidRequest
from forge.server.storage import Database

#: Absolute session lifetime; no silent extension.
DEFAULT_SESSION_TTL_SECONDS = 12 * 3600

#: Token prefix keeps session tokens distinguishable in logs (never secrets).
SESSION_TOKEN_PREFIX = "fss_"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ServerSession:
    id: str
    principal: str
    role: str
    scopes: "tuple[str, ...]"
    status: str
    created_at: float
    updated_at: float
    expires_at: float
    last_seen_at: "Optional[float]" = None
    metadata: str = "{}"

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at

    @property
    def active(self) -> bool:
        return self.status == "active" and not self.expired

    def to_dict(self) -> "Dict[str, Any]":
        return {
            "session_id": self.id,
            "principal": self.principal,
            "role": self.role,
            "scopes": list(self.scopes),
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
            "last_seen_at": self.last_seen_at,
        }


class SessionManager:
    """SQLite-backed session storage with TTL and bounded count."""

    def __init__(self, db: Database, *,
                 max_sessions: int = 200,
                 ttl_seconds: float = DEFAULT_SESSION_TTL_SECONDS) -> None:
        self._db = db
        self.max_sessions = max(1, int(max_sessions))
        self.ttl_seconds = ttl_seconds
        self._lock = threading.Lock()

    def create(self, principal: str, role: str,
               scopes: "List[str] | tuple[str, ...]", *,
               ttl_seconds: Optional[float] = None,
               metadata: "Optional[Dict[str, Any]]" = None,
               ) -> "tuple[ServerSession, str]":
        """Create a session; returns it plus the one-time raw token."""
        if not principal or not isinstance(principal, str):
            raise InvalidRequest("Session needs a principal name.")
        ttl = self.ttl_seconds if ttl_seconds is None else float(ttl_seconds)
        if ttl <= 0 or ttl > 7 * 24 * 3600:
            raise InvalidRequest("ttl_seconds out of range.")
        with self._lock:
            self.prune()
            if self.count_active() >= self.max_sessions:
                # Evict the oldest session instead of refusing the client.
                oldest = self._db.query_one(
                    "SELECT id FROM sessions WHERE status = 'active' "
                    "ORDER BY created_at ASC LIMIT 1")
                if oldest is not None:
                    self.revoke(str(oldest["id"]))
            moment = time.time()
            token = SESSION_TOKEN_PREFIX + secrets.token_urlsafe(32)
            session = ServerSession(
                id=secrets.token_hex(8), principal=principal, role=role,
                scopes=tuple(scopes), status="active", created_at=moment,
                updated_at=moment, expires_at=moment + ttl,
                metadata=json.dumps(metadata or {}, default=str))
            self._db.execute(
                "INSERT INTO sessions (id, token_hash, principal, role, "
                "scopes, status, created_at, updated_at, expires_at, "
                "last_seen_at, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?, "
                "?, NULL, ?)",
                (session.id, hash_token(token), session.principal,
                 session.role, json.dumps(list(session.scopes)),
                 session.status, session.created_at, session.updated_at,
                 session.expires_at, session.metadata))
        return session, token

    def get(self, session_id: str) -> Optional[ServerSession]:
        row = self._db.query_one(
            "SELECT * FROM sessions WHERE id = ?", (session_id,))
        return self._row_to_session(row) if row is not None else None

    def get_by_token(self, token: str) -> Optional[ServerSession]:
        if not isinstance(token, str) or not token:
            return None
        row = self._db.query_one(
            "SELECT * FROM sessions WHERE token_hash = ?",
            (hash_token(token),))
        if row is None:
            return None
        session = self._row_to_session(row)
        if session.active:
            self.touch(session.id)
        return session

    def touch(self, session_id: str) -> None:
        self._db.execute(
            "UPDATE sessions SET last_seen_at = ?, updated_at = ? "
            "WHERE id = ?", (time.time(), time.time(), session_id))

    def revoke(self, session_id: str) -> bool:
        cursor = self._db.execute(
            "UPDATE sessions SET status = 'revoked', updated_at = ? "
            "WHERE id = ? AND status = 'active'",
            (time.time(), session_id))
        return cursor.rowcount > 0

    def prune(self, now: "Optional[float]" = None) -> int:
        moment = time.time() if now is None else now
        cursor = self._db.execute(
            "UPDATE sessions SET status = 'expired' "
            "WHERE status = 'active' AND expires_at <= ?", (moment,))
        return cursor.rowcount

    def count_active(self) -> int:
        row = self._db.query_one(
            "SELECT COUNT(*) AS n FROM sessions WHERE status = 'active'")
        return int(row["n"]) if row else 0

    def list_active(self, limit: int = 50) -> List[ServerSession]:
        rows = self._db.query(
            "SELECT * FROM sessions WHERE status = 'active' "
            "ORDER BY created_at DESC LIMIT ?",
            (max(1, min(200, int(limit))),))
        return [self._row_to_session(row) for row in rows]

    @staticmethod
    def _row_to_session(row: Any) -> ServerSession:
        try:
            scopes = tuple(json.loads(row["scopes"] or "[]"))
        except ValueError:
            scopes = ()
        return ServerSession(
            id=row["id"], principal=row["principal"], role=row["role"],
            scopes=scopes, status=row["status"],
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            expires_at=float(row["expires_at"]),
            last_seen_at=row["last_seen_at"],
            metadata=row["metadata"] or "{}")
