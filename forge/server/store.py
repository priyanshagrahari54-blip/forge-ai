"""SQLite persistence for the Forge Server desktop-client link (A81).

Tables live in the same database file as the control plane. Stored
credentials are **verifiers only** (salted SHA-256 of the client secret,
see :mod:`forge.link.protocol`) — the plaintext secret exists solely on
the client and only until the operator dismisses the registration
dialog.

Also persisted here: handshake challenge state (bounded per client) and
the single-use nonce replay cache for request signatures.
"""
from __future__ import annotations

import threading
import time

from forge.control.db import Database

#: Maximum replay-cache entries retained per client (bounded state).
REPLAY_CACHE_PER_CLIENT = 512
#: Maximum outstanding (unconsumed) handshakes per client id.
MAX_OUTSTANDING_HANDSHAKES_PER_CLIENT = 2
#: Handshake challenge validity window (seconds).
CHALLENGE_TTL_SECONDS = 120.0

_VALID_MODES = ("safe", "assisted", "autonomous")


class LinkStoreError(RuntimeError):
    """Client-management failure with an operator-facing message."""


class LinkStore:
    """Thread-safe storage for link clients, handshakes, and nonces."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._lock = threading.RLock()
        with self._lock:
            db.execute("""
                CREATE TABLE IF NOT EXISTS link_clients (
                    client_id   TEXT PRIMARY KEY,
                    name        TEXT NOT NULL DEFAULT '',
                    project_id  TEXT NOT NULL,
                    salt        TEXT NOT NULL,
                    verifier    TEXT NOT NULL,
                    max_mode    TEXT NOT NULL DEFAULT 'assisted',
                    status      TEXT NOT NULL DEFAULT 'active',
                    created_at  REAL NOT NULL,
                    updated_at  REAL NOT NULL,
                    last_seen   REAL NOT NULL DEFAULT 0
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS link_handshakes (
                    client_id    TEXT NOT NULL,
                    nonce_client TEXT NOT NULL,
                    nonce_server TEXT NOT NULL,
                    created_at   REAL NOT NULL,
                    PRIMARY KEY (client_id, nonce_client)
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS link_sessions (
                    client_id     TEXT PRIMARY KEY,
                    nonce_client  TEXT NOT NULL,
                    nonce_server  TEXT NOT NULL,
                    session_id    TEXT NOT NULL,
                    started_at    REAL NOT NULL,
                    expires_at    REAL NOT NULL
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS link_seen_nonces (
                    client_id  TEXT NOT NULL,
                    nonce      TEXT NOT NULL,
                    seen_at    REAL NOT NULL,
                    PRIMARY KEY (client_id, nonce)
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS link_task_origins (
                    task_id    TEXT PRIMARY KEY,
                    client_id  TEXT NOT NULL,
                    execution  TEXT NOT NULL,
                    decision   TEXT NOT NULL DEFAULT ''
                )
            """)

    # -- clients -------------------------------------------------------------

    def create_client(self, client_id: str, project_id: str, salt: str,
                      verifier: str, *, name: str = "",
                      max_mode: str = "assisted",
                      now: float | None = None) -> None:
        if max_mode not in _VALID_MODES:
            raise LinkStoreError(f"invalid max_mode: {max_mode!r}")
        stamp = time.time() if now is None else now
        with self._lock:
            existing = self._db.query_one(
                "SELECT client_id FROM link_clients WHERE client_id = ?",
                (client_id,))
            if existing is not None:
                raise LinkStoreError(f"client already registered: {client_id}")
            self._db.execute(
                "INSERT INTO link_clients (client_id, name, project_id, salt,"
                " verifier, max_mode, status, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)",
                (client_id, name, project_id, salt, verifier, max_mode,
                 stamp, stamp))

    def rotate_client(self, client_id: str, salt: str, verifier: str,
                      *, now: float | None = None) -> None:
        stamp = time.time() if now is None else now
        with self._lock:
            cursor = self._db.execute(
                "UPDATE link_clients SET salt = ?, verifier = ?, status = "
                "'active', updated_at = ? WHERE client_id = ?",
                (salt, verifier, stamp, client_id))
            if cursor.rowcount != 1:
                raise LinkStoreError(f"unknown client: {client_id}")
            # A rotation invalidates every live session and challenge.
            self._db.execute(
                "DELETE FROM link_sessions WHERE client_id = ?", (client_id,))
            self._db.execute(
                "DELETE FROM link_handshakes WHERE client_id = ?",
                (client_id,))

    def revoke_client(self, client_id: str, *,
                      now: float | None = None) -> None:
        stamp = time.time() if now is None else now
        with self._lock:
            cursor = self._db.execute(
                "UPDATE link_clients SET status = 'revoked', updated_at = ?"
                " WHERE client_id = ?", (stamp, client_id))
            if cursor.rowcount != 1:
                raise LinkStoreError(f"unknown client: {client_id}")
            self._db.execute(
                "DELETE FROM link_sessions WHERE client_id = ?", (client_id,))

    def get_client(self, client_id: str):
        with self._lock:
            row = self._db.query_one(
                "SELECT * FROM link_clients WHERE client_id = ?", (client_id,))
        return dict(row) if row is not None else None

    def list_clients(self) -> list[dict]:
        with self._lock:
            rows = self._db.query(
                "SELECT client_id, name, project_id, max_mode, status,"
                " created_at, updated_at, last_seen FROM link_clients"
                " ORDER BY client_id")
        return [dict(row) for row in rows]

    def touch_client(self, client_id: str, *, now: float) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE link_clients SET last_seen = ? WHERE client_id = ?",
                (now, client_id))

    def set_task_origin(self, task_id: str, client_id: str, execution: str,
                        decision: str = "", *, now: float | None = None,
                        max_rows: int = 5000) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO link_task_origins"
                " (task_id, client_id, execution, decision)"
                " VALUES (?, ?, ?, ?)",
                (task_id, client_id, execution, decision))
            extra = self._db.query(
                "SELECT task_id FROM link_task_origins"
                " ORDER BY rowid DESC LIMIT -1 OFFSET ?", (max_rows,))
            for row in extra:
                self._db.execute(
                    "DELETE FROM link_task_origins WHERE task_id = ?",
                    (row["task_id"],))

    def get_task_origin(self, task_id: str) -> dict | None:
        with self._lock:
            row = self._db.query_one(
                "SELECT * FROM link_task_origins WHERE task_id = ?",
                (task_id,))
        return dict(row) if row is not None else None

    # -- handshakes ----------------------------------------------------------

    def store_challenge(self, client_id: str, nonce_client: str,
                        nonce_server: str, *, now: float) -> None:
        with self._lock:
            outstanding = self._db.query(
                "SELECT nonce_client FROM link_handshakes WHERE client_id = ?"
                " ORDER BY created_at", (client_id,))
            if len(outstanding) >= MAX_OUTSTANDING_HANDSHAKES_PER_CLIENT:
                # Replace the oldest: bounded state, still allows one retry.
                self._db.execute(
                    "DELETE FROM link_handshakes WHERE client_id = ? AND"
                    " nonce_client = ?",
                    (client_id, outstanding[0]["nonce_client"]))
            self._db.execute(
                "INSERT OR REPLACE INTO link_handshakes"
                " (client_id, nonce_client, nonce_server, created_at)"
                " VALUES (?, ?, ?, ?)",
                (client_id, nonce_client, nonce_server, now))

    def take_challenge(self, client_id: str, nonce_client: str, *,
                       now: float) -> str | None:
        """Consume the stored server nonce (single use) if still fresh."""
        with self._lock:
            self._db.execute(
                "DELETE FROM link_handshakes WHERE created_at < ?",
                (now - CHALLENGE_TTL_SECONDS,))
            row = self._db.query_one(
                "SELECT nonce_server FROM link_handshakes WHERE client_id = ?"
                " AND nonce_client = ? AND created_at >= ?",
                (client_id, nonce_client, now - CHALLENGE_TTL_SECONDS))
            if row is None:
                return None
            self._db.execute(
                "DELETE FROM link_handshakes WHERE client_id = ? AND"
                " nonce_client = ?", (client_id, nonce_client))
            return row["nonce_server"]

    # -- link sessions -------------------------------------------------------

    def store_session(self, client_id: str, nonce_client: str,
                      nonce_server: str, session_id: str, *,
                      started_at: float, expires_at: float) -> None:
        with self._lock:
            # One active link session per client: a new handshake
            # supersedes (and thereby revokes) the previous one.
            self._db.execute(
                "INSERT OR REPLACE INTO link_sessions"
                " (client_id, nonce_client, nonce_server, session_id,"
                "  started_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
                (client_id, nonce_client, nonce_server, session_id,
                 started_at, expires_at))

    def get_session(self, client_id: str, *, now: float):
        with self._lock:
            row = self._db.query_one(
                "SELECT * FROM link_sessions WHERE client_id = ?"
                " AND expires_at > ?", (client_id, now))
        return dict(row) if row is not None else None

    def drop_session(self, client_id: str) -> None:
        with self._lock:
            self._db.execute(
                "DELETE FROM link_sessions WHERE client_id = ?", (client_id,))

    # -- replay cache ----------------------------------------------------------

    def nonce_seen(self, client_id: str, nonce: str, *,
                   now: float, window: float) -> bool:
        """Record ``nonce``; return True if it was already seen (replay)."""
        with self._lock:
            seen = self._db.query_one(
                "SELECT 1 FROM link_seen_nonces WHERE client_id = ? AND"
                " nonce = ?", (client_id, nonce))
            if seen is not None:
                return True
            self._db.execute(
                "INSERT INTO link_seen_nonces (client_id, nonce, seen_at)"
                " VALUES (?, ?, ?)", (client_id, nonce, now))
            self._db.execute(
                "DELETE FROM link_seen_nonces WHERE client_id = ? AND"
                " rowid NOT IN (SELECT rowid FROM link_seen_nonces WHERE"
                " client_id = ? ORDER BY seen_at DESC LIMIT ?)",
                (client_id, client_id, REPLAY_CACHE_PER_CLIENT))
            return False
