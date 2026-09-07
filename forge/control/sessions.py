"""Persistent, project-scoped Forge sessions (A34, local-development auth).

A session binds an actor identity to exactly one project. The browser
presents the session token (HttpOnly cookie or ``Authorization: Bearer``);
the server hashes it and looks it up — the token itself is never stored.

This is explicitly a LOCAL-DEVELOPMENT authentication foundation: there
are no passwords and no external identity providers yet. Sessions carry
the actor name supplied at creation for audit attribution. Production
deployments must front this with real authentication; the
:program:`forge serve` banner and ``/api/v1/health`` say so.
"""
from __future__ import annotations

import hashlib
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any

from forge.control.db import Database

#: Actors are audit identities, not free text: fail closed on anything odd.
_ACTOR_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,63}")

#: Session lifetimes are bounded; absolute expiry, no silent extension.
DEFAULT_SESSION_TTL_SECONDS = 12 * 3600


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Session:
    id: str
    actor: str
    project_id: str
    profile: str
    status: str
    created_at: float
    updated_at: float
    expires_at: float
    active_task: str = ""
    metadata: str = "{}"

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at

    @property
    def active(self) -> bool:
        return self.status == "active" and not self.expired

    def to_dict(self, *, include_active_task: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "session_id": self.id,
            "actor": self.actor,
            "project_id": self.project_id,
            "profile": self.profile,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
        }
        if include_active_task:
            payload["active_task"] = self.active_task
        return payload


class SessionStore:
    """SQLite-backed session storage."""

    def __init__(self, db: Database) -> None:
        self._db = db
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                token_hash TEXT NOT NULL UNIQUE,
                actor TEXT NOT NULL,
                project_id TEXT NOT NULL,
                profile TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                active_task TEXT NOT NULL DEFAULT '',
                metadata TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_token "
            "ON sessions(token_hash)"
        )

    @staticmethod
    def validate_actor(actor: Any) -> str:
        if not isinstance(actor, str):
            raise ValueError("actor must be a string")
        candidate = actor.strip()
        if not candidate or _ACTOR_RE.fullmatch(candidate) is None:
            raise ValueError(
                "actor must be 1-64 chars: letters, digits, and ._-@")
        return candidate

    def create(self, actor: str, project_id: str, *,
               profile: str = "assisted",
               ttl_seconds: float = DEFAULT_SESSION_TTL_SECONDS,
               metadata: str = "{}") -> tuple[Session, str]:
        actor = self.validate_actor(actor)
        if ttl_seconds <= 0 or ttl_seconds > 7 * 24 * 3600:
            raise ValueError("ttl_seconds out of range")
        now = time.time()
        token = secrets.token_urlsafe(32)
        session = Session(
            id=secrets.token_hex(8), actor=actor, project_id=project_id,
            profile=profile, status="active", created_at=now, updated_at=now,
            expires_at=now + ttl_seconds, metadata=metadata or "{}")
        self._db.execute(
            "INSERT INTO sessions (id, token_hash, actor, project_id, "
            "profile, status, created_at, updated_at, expires_at, "
            "active_task, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session.id, hash_token(token), session.actor,
             session.project_id, session.profile, session.status,
             session.created_at, session.updated_at, session.expires_at,
             session.active_task, session.metadata))
        return session, token

    def _row_to_session(self, row: Any) -> Session:
        return Session(
            id=row["id"], actor=row["actor"],
            project_id=row["project_id"], profile=row["profile"],
            status=row["status"], created_at=row["created_at"],
            updated_at=row["updated_at"], expires_at=row["expires_at"],
            active_task=row["active_task"] or "",
            metadata=row["metadata"] or "{}")

    def get(self, session_id: str) -> Session | None:
        row = self._db.query_one(
            "SELECT * FROM sessions WHERE id = ?", (session_id,))
        return self._row_to_session(row) if row is not None else None

    def get_by_token(self, token: str) -> Session | None:
        if not isinstance(token, str) or not token:
            return None
        row = self._db.query_one(
            "SELECT * FROM sessions WHERE token_hash = ?",
            (hash_token(token),))
        if row is None:
            return None
        return self._row_to_session(row)

    def touch(self, session_id: str) -> None:
        self._db.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?",
            (time.time(), session_id))

    def set_active_task(self, session_id: str, task_id: str) -> None:
        self._db.execute(
            "UPDATE sessions SET active_task = ?, updated_at = ? "
            "WHERE id = ?",
            (task_id, time.time(), session_id))

    def revoke(self, session_id: str) -> bool:
        cursor = self._db.execute(
            "UPDATE sessions SET status = 'revoked', updated_at = ? "
            "WHERE id = ? AND status = 'active'",
            (time.time(), session_id))
        return cursor.rowcount > 0

    def prune(self, now: float | None = None) -> int:
        """Mark expired sessions; returns the number pruned."""
        moment = time.time() if now is None else now
        cursor = self._db.execute(
            "UPDATE sessions SET status = 'expired' "
            "WHERE status = 'active' AND expires_at <= ?",
            (moment,))
        return cursor.rowcount

    def count_active(self) -> int:
        row = self._db.query_one(
            "SELECT COUNT(*) AS n FROM sessions WHERE status = 'active'")
        return int(row["n"]) if row else 0
