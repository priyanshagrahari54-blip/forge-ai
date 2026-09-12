"""Deterministic link protocol shared by Forge Desktop and Forge Server.

Security model (A81) — verified by :mod:`tests.test_link_protocol`:

- **No plaintext credentials.** The server never stores the client
  secret: it stores a *verifier* (salted SHA-256). The client stores the
  secret locally with ``0600`` permissions. Neither the secret nor the
  verifier ever crosses the wire.
- **Authenticated handshake.** The client proves possession of the
  secret with ``HMAC-SHA256(verifier, transcript)`` over a server
  nonce + client nonce + client id. The verifier itself (a hash of the
  secret) never leaves the client; the server keeps only the stored
  verifier, which is exactly the material needed to check the HMAC.
  The plaintext secret cannot be recovered from a stolen server
  database (documented limitation: the verifier *can* be used to
  impersonate the client to *that same server*; see docs).
- **Authenticated requests.** After the handshake both sides derive the
  same session key; every request carries
  ``HMAC-SHA256(session_key, method|path|body_sha256|timestamp|nonce)``.
  Timestamps outside the replay window are rejected and every nonce is
  single-use (server-side replay cache).
- **No arbitrary remote execution.** The protocol carries only typed
  task submissions (requirement text + bounded enums) — never commands.
- **Constant-time comparisons** for every secret-bearing check.

Everything here is pure stdlib and deterministic (except :func:`new_nonce`
/:func:`new_salt`/:func:`new_secret`, which use ``secrets``).
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets

#: Protocol identifier, bound into every derived value. Bump to break
#: compatibility with older peers explicitly.
PROTOCOL_VERSION = "forge-link/1"

#: Signature timestamp tolerance on each side of server time (seconds).
TIMESTAMP_WINDOW_SECONDS = 120.0

#: Size bounds for protocol fields.
NONCE_BYTES = 16
SECRET_BYTES = 24
MAX_CLIENT_ID = 32
MAX_SIGNED_BODY_BYTES = 2 * 1024 * 1024

_CLIENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")
_NONCE_RE = re.compile(r"^[0-9a-f]{32}$")
_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")

_HEADER_CLIENT = "x-forge-client"
_HEADER_TIMESTAMP = "x-forge-timestamp"
_HEADER_NONCE = "x-forge-nonce"
_HEADER_SIGNATURE = "x-forge-signature"

#: Exported header names (single source of truth for both sides).
HEADER_CLIENT = "X-Forge-Client"
HEADER_TIMESTAMP = "X-Forge-Timestamp"
HEADER_NONCE = "X-Forge-Nonce"
HEADER_SIGNATURE = "X-Forge-Signature"


class ProtocolError(ValueError):
    """Malformed protocol input (fail closed before any crypto)."""


def valid_client_id(client_id: str) -> bool:
    """Client ids are short lowercase slugs: ``g560``, ``workstation-2``."""
    return bool(isinstance(client_id, str)
                and _CLIENT_ID_RE.match(client_id or ""))


def valid_nonce(nonce: str) -> bool:
    return bool(isinstance(nonce, str) and _NONCE_RE.match(nonce or ""))


def valid_salt(salt: str) -> bool:
    """Salts are hex strings of the same shape as nonces."""
    return valid_nonce(salt)


def valid_secret(secret: str) -> bool:
    return bool(isinstance(secret, str) and _SECRET_RE.match(secret or ""))


def require_client_id(client_id: str) -> str:
    if not valid_client_id(client_id):
        raise ProtocolError("invalid client id")
    return client_id


def require_nonce(nonce: str) -> str:
    if not valid_nonce(nonce):
        raise ProtocolError("invalid nonce")
    return nonce


def require_secret(secret: str) -> str:
    if not valid_secret(secret):
        raise ProtocolError(
            "invalid client secret format (expected 32-128 urlsafe chars)")
    return secret


# -- randomness ---------------------------------------------------------------

def new_nonce() -> str:
    return secrets.token_hex(NONCE_BYTES)


def new_salt() -> str:
    return secrets.token_hex(NONCE_BYTES)


def new_secret() -> str:
    """One-time client secret, shown once at registration."""
    return secrets.token_urlsafe(SECRET_BYTES)


# -- derivation ---------------------------------------------------------------

def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def derive_verifier(secret: str, salt: str) -> str:
    """Server-side stored credential: ``SHA-256(version|verifier|salt|secret)``.

    The secret is never stored anywhere; the salt defeats precomputed
    tables and is per client.
    """
    require_secret(secret)
    if not isinstance(salt, str) or not _NONCE_RE.match(salt or ""):
        raise ProtocolError("invalid salt")
    return _sha256_hex(f"{PROTOCOL_VERSION}|verifier|{salt}|{secret}")


def _mac(key_hex: str, message: str) -> str:
    return hmac.new(key_hex.encode("ascii"), message.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def handshake_transcript(client_id: str, nonce_client: str,
                         nonce_server: str) -> str:
    require_client_id(client_id)
    require_nonce(nonce_client)
    require_nonce(nonce_server)
    return "|".join([PROTOCOL_VERSION, "handshake",
                     client_id, nonce_client, nonce_server])


def handshake_proof(verifier: str, client_id: str, nonce_client: str,
                    nonce_server: str) -> str:
    """Client proof of secret possession (keyed by the *verifier*)."""
    return _mac(verifier, handshake_transcript(
        client_id, nonce_client, nonce_server))


def session_key(verifier: str, client_id: str, nonce_client: str,
                nonce_server: str) -> str:
    """Derived per-session MAC key (never transmitted, never stored)."""
    require_client_id(client_id)
    require_nonce(nonce_client)
    require_nonce(nonce_server)
    return _mac(verifier, "|".join([
        PROTOCOL_VERSION, "session", client_id, nonce_client, nonce_server]))


# -- request signatures -------------------------------------------------------

def _body_digest(body: bytes) -> str:
    if len(body) > MAX_SIGNED_BODY_BYTES:
        raise ProtocolError("signed body too large")
    return hashlib.sha256(body).hexdigest()


def canonical_request(method: str, path: str, body: bytes, timestamp: str,
                      nonce: str) -> str:
    """Exact bytes the signature covers. ``method`` is upper-cased;
    ``path`` is used as-is (the client and server must agree on it)."""
    if not isinstance(method, str) or not method:
        raise ProtocolError("invalid method")
    if not isinstance(path, str) or not path.startswith("/"):
        raise ProtocolError("invalid path")
    require_nonce(nonce)
    if not isinstance(timestamp, str) or not timestamp.isdigit() \
            or len(timestamp) > 16:
        raise ProtocolError("invalid timestamp")
    return "|".join([PROTOCOL_VERSION, "request", method.upper(), path,
                     _body_digest(body if body is not None else b""),
                     timestamp, nonce])


def sign_request(session_key_hex: str, method: str, path: str, body: bytes,
                 timestamp: str, nonce: str) -> str:
    return _mac(session_key_hex, canonical_request(
        method, path, body, timestamp, nonce))


def signature_headers(client_id: str, timestamp: str, nonce: str,
                      signature: str) -> dict[str, str]:
    return {
        HEADER_CLIENT: client_id,
        HEADER_TIMESTAMP: timestamp,
        HEADER_NONCE: nonce,
        HEADER_SIGNATURE: signature,
    }


def constant_time_equals(expected: str, actual: str) -> bool:
    if not isinstance(expected, str) or not isinstance(actual, str):
        return False
    return hmac.compare_digest(expected.encode("utf-8"),
                               actual.encode("utf-8"))


def timestamp_fresh(timestamp: str, now: float,
                    window: float = TIMESTAMP_WINDOW_SECONDS) -> bool:
    try:
        value = float(timestamp)
    except (TypeError, ValueError):
        return False
    return abs(now - value) <= window
