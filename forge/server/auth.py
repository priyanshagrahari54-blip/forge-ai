"""Authentication for the Forge Server (A81).

Three credential kinds, checked in order, all fail closed:

1. **Session tokens** (``fss_...``) — persistent, hashed at rest, TTL
   bounded; issued by ``POST /api/v1/auth/sessions`` in exchange for an
   API key. This is what a reconnecting Forge Desktop client presents.
2. **API keys** (``fsk_...``) — long-lived machine credentials created
   by an admin; stored hashed (SHA-256), never echoed after creation.
3. **Bootstrap token** — a single startup token (configured or
   generated) with admin scope, so a fresh local server is usable
   immediately. Like everything else here it is compared in constant
   time.

Repeated failures from one credential are rate-limited in bounded
memory: brute force gets throttled, not answered.

There are no passwords and no external identity providers. This is a
local-development authentication foundation; production deployments
must front the server with real authentication (the health report
carries the warning while ``local_dev_mode`` is on).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from forge.server.authorization import ROLE_SCOPES
from forge.server.errors import (
    AuthenticationRequired,
    InvalidRequest,
    RateLimited,
)
from forge.server.sessions import SessionManager
from forge.server.storage import Database

#: API key prefix (Forge Server Key). Tokens: Forge Server Session.
API_KEY_PREFIX = "fsk_"

#: Failed-auth throttle: this many failures inside the window per
#: credential hash locks the credential out for the rest of the window.
MAX_FAILURES = 5
FAILURE_WINDOW_SECONDS = 300.0
_MAX_TRACKED = 1024


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


#: Challenge/response: how long an issued challenge stays valid, and how
#: many are tracked before the oldest is evicted (bounded memory).
CHALLENGE_TTL_SECONDS = 120.0
CHALLENGE_MAX_TRACKED = 1024


def _challenge_nonce() -> str:
    return secrets.token_urlsafe(24)


@dataclass(frozen=True)
class Principal:
    """An authenticated caller: who, with which role and scopes."""

    name: str
    role: str
    scopes: "FrozenSet[str]" = field(default_factory=frozenset)
    session_id: str = ""
    via: str = "api_key"  # api_key | session | bootstrap

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes

    def to_dict(self) -> "Dict[str, Any]":
        return {
            "principal": self.name,
            "role": self.role,
            "scopes": sorted(self.scopes),
            "session_id": self.session_id,
            "via": self.via,
        }


class AuthManager:
    """API keys + bootstrap token + session lookup, with lockout."""

    def __init__(self, db: Database, sessions: SessionManager, *,
                 bootstrap_token: str = "") -> None:
        self._db = db
        self._sessions = sessions
        self._bootstrap_token = bootstrap_token or ""
        self._failures: "OrderedDict[str, Tuple[int, float]]" = OrderedDict()
        self._lock = threading.Lock()
        # Challenge/response nonces: single-use, time-bounded. The set is
        # what makes the session exchange replay-resistant: a captured
        # `POST /auth/sessions` cannot be resent because its challenge
        # has already been consumed (or expired).
        self._challenges: "OrderedDict[str, float]" = OrderedDict()

    # -- challenge / response -----------------------------------------------------

    def new_challenge(self) -> "Dict[str, Any]":
        """Issue a fresh single-use challenge for a session exchange."""
        now = time.time()
        with self._lock:
            # Drop expired entries opportunistically (bounded memory).
            expired = [nonce for nonce, issued in self._challenges.items()
                       if now - issued > CHALLENGE_TTL_SECONDS]
            for nonce in expired:
                del self._challenges[nonce]
            while len(self._challenges) >= CHALLENGE_MAX_TRACKED:
                self._challenges.popitem(last=False)
            nonce = _challenge_nonce()
            self._challenges[nonce] = now
        return {"nonce": nonce, "issued_at": now,
                "ttl_seconds": CHALLENGE_TTL_SECONDS}

    def consume_challenge(self, nonce: Any) -> bool:
        """Validate and consume a challenge. Returns True exactly once
        per valid, unexpired nonce; anything else is refused."""
        if not isinstance(nonce, str) or not nonce.strip():
            return False
        nonce = nonce.strip()
        now = time.time()
        with self._lock:
            issued = self._challenges.pop(nonce, None)
            if issued is None:
                return False
            if now - issued > CHALLENGE_TTL_SECONDS:
                return False
            return True

    # -- key management ---------------------------------------------------------

    def create_api_key(self, name: str, role: str = "operator") -> "Dict[str, Any]":
        """Create an API key; the raw value is returned exactly once."""
        if not isinstance(name, str) or not name.strip() or len(name) > 64:
            raise InvalidRequest("Key name must be 1-64 characters.")
        name = name.strip()
        if role not in ROLE_SCOPES:
            raise InvalidRequest(
                "Unknown role %r; want admin|operator|viewer." % role)
        existing = self._db.query_one(
            "SELECT key_hash FROM api_keys WHERE name = ? AND revoked = 0",
            (name,))
        if existing is not None:
            raise InvalidRequest("An active API key named %r exists." % name)
        raw = API_KEY_PREFIX + secrets.token_urlsafe(32)
        scopes = sorted(ROLE_SCOPES[role])
        self._db.execute(
            "INSERT INTO api_keys (key_hash, name, role, scopes, "
            "created_at, last_used_at, revoked) VALUES (?, ?, ?, ?, ?, "
            "NULL, 0) ON CONFLICT(key_hash) DO NOTHING",
            (hash_key(raw), name, role, json.dumps(scopes), time.time()))
        return {"name": name, "role": role, "scopes": scopes, "key": raw}

    def revoke_api_key(self, name: str) -> bool:
        cursor = self._db.execute(
            "UPDATE api_keys SET revoked = 1 WHERE name = ? AND revoked = 0",
            (name,))
        return cursor.rowcount > 0

    def list_api_keys(self) -> "List[Dict[str, Any]]":
        rows = self._db.query(
            "SELECT name, role, scopes, created_at, last_used_at, revoked "
            "FROM api_keys ORDER BY created_at ASC")
        return [{
            "name": row["name"], "role": row["role"],
            "scopes": json.loads(row["scopes"] or "[]"),
            "created_at": row["created_at"],
            "last_used_at": row["last_used_at"],
            "revoked": bool(row["revoked"]),
        } for row in rows]

    # -- authentication -----------------------------------------------------------

    def authenticate(self, token: Any) -> Principal:
        """Resolve a bearer token to a principal, or fail closed."""
        if not isinstance(token, str) or not token.strip():
            raise AuthenticationRequired("Authentication required.")
        token = token.strip()
        self._check_lockout(token)
        principal = self._authenticate(token)
        if principal is None:
            self._record_failure(token)
            raise AuthenticationRequired(
                "Unknown, expired, or revoked credentials.")
        return principal

    def _authenticate(self, token: str) -> Optional[Principal]:
        if token.startswith("fss_"):
            session = self._sessions.get_by_token(token)
            if session is not None and session.active:
                return Principal(
                    name=session.principal, role=session.role,
                    scopes=frozenset(session.scopes),
                    session_id=session.id, via="session")
            return None
        if token.startswith(API_KEY_PREFIX):
            row = self._db.query_one(
                "SELECT * FROM api_keys WHERE key_hash = ? AND revoked = 0",
                (hash_key(token),))
            if row is None:
                return None
            self._db.execute(
                "UPDATE api_keys SET last_used_at = ? WHERE key_hash = ?",
                (time.time(), hash_key(token)))
            try:
                scopes = frozenset(json.loads(row["scopes"] or "[]"))
            except ValueError:
                scopes = frozenset()
            return Principal(name=row["name"], role=row["role"],
                             scopes=scopes, via="api_key")
        if self._bootstrap_token and hmac.compare_digest(
                token, self._bootstrap_token):
            return Principal(name="bootstrap", role="admin",
                             scopes=frozenset(ROLE_SCOPES["admin"]),
                             via="bootstrap")
        return None

    # -- brute-force throttle ------------------------------------------------------

    def _check_lockout(self, token: str) -> None:
        digest = hash_key(token)[:16]
        now = time.time()
        with self._lock:
            entry = self._failures.get(digest)
            if entry is None:
                return
            count, first = entry
            if now - first > FAILURE_WINDOW_SECONDS:
                self._failures.pop(digest, None)
                return
            if count >= MAX_FAILURES:
                raise RateLimited(
                    "Too many failed authentication attempts; retry later.")

    def _record_failure(self, token: str) -> None:
        digest = hash_key(token)[:16]
        now = time.time()
        with self._lock:
            count, first = self._failures.get(digest, (0, now))
            if now - first > FAILURE_WINDOW_SECONDS:
                count, first = 0, now
            self._failures[digest] = (count + 1, first)
            self._failures.move_to_end(digest)
            while len(self._failures) > _MAX_TRACKED:
                self._failures.popitem(last=False)

    def reset_failures(self) -> None:
        with self._lock:
            self._failures.clear()
