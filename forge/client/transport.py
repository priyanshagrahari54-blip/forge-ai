"""Signed HTTP transport for the Forge Desktop client (A81).

stdlib-only (``urllib.request``) so the lightweight client installs no
extra dependencies. Responsibilities:

- perform the A81 challenge/handshake and derive the session key;
- sign every request over method|path|body-sha256|timestamp|nonce;
- never log the secret, verifier, session key, or signature inputs;
- normalize failures into :mod:`forge.link.errors` types so callers can
  distinguish "server down" (retry) from "bad credentials" (don't).

The socket-level send is a single injectable callable
(``sender=``) — tests run the whole client stack against a real
FastAPI app without sockets.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable, Optional

from forge.link import protocol
from forge.link.errors import (
    AuthError,
    AuthExpired,
    RequestError,
    ServerError,
    TransportError,
)

#: Bound on responses we are willing to read.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024

#: A callable that performs one HTTP round trip.
Sender = Callable[[str, str, dict, bytes, float], "tuple[int, bytes]"]


def urllib_sender(method: str, url: str, headers: dict, body: bytes,
                  timeout: float) -> tuple[int, bytes]:
    """Default sender over ``urllib.request`` (the only network code)."""
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, data=body or None, method=method)
    for key, value in headers.items():
        request.add_header(key, value)
    if body and "Content-Type" not in headers:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read(MAX_RESPONSE_BYTES)
    except urllib.error.HTTPError as exc:
        try:
            payload = exc.read(MAX_RESPONSE_BYTES)
        except Exception:  # noqa: BLE001 - best-effort error body
            payload = b""
        return int(exc.code), payload
    except Exception as exc:  # noqa: BLE001 - normalized to TransportError
        raise TransportError(f"cannot reach server: {exc}") from None


class LinkTransport:
    """Signed client transport for one configured server."""

    def __init__(self, config, *, sender: Optional[Sender] = None,
                 now: Optional[Callable[[], float]] = None,
                 nonce: Optional[Callable[[], str]] = None) -> None:
        self.config = config
        self._sender = sender or urllib_sender
        self._now = now or time.time
        self._nonce = nonce or protocol.new_nonce
        self._verifier = ""
        self._session_key = ""
        self._nonce_client = ""
        self._nonce_server = ""
        self._expires_at = 0.0

    # -- state ----------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return bool(self._session_key) and self._now() < self._expires_at

    @property
    def expires_at(self) -> float:
        return self._expires_at

    def reset(self) -> None:
        """Drop session material (kept in memory only, never on disk)."""
        self._session_key = ""
        self._nonce_client = ""
        self._nonce_server = ""
        self._expires_at = 0.0

    # -- handshake ---------------------------------------------------------------

    def connect(self) -> dict[str, Any]:
        """Challenge + handshake; derives the session key. Returns info.

        The challenge reply carries the *public* registration salt, so
        the only out-of-band credential on the client is the one-time
        secret (stored 0600). The verifier derived from it never leaves
        this process.
        """
        secret = self.config.load_secret()
        client_id = self.config.client_id
        nonce_client = self._nonce()
        url = self.config.server_url + "/api/v1/link/challenge"
        status, payload = self._request_raw(
            "POST", url, {},
            json.dumps({"client_id": client_id,
                        "nonce": nonce_client}).encode("utf-8"))
        challenge = _parse(status, payload)
        nonce_server = str(challenge.get("server_nonce", ""))
        salt = str(challenge.get("salt", ""))
        if not protocol.valid_nonce(nonce_server) \
                or not protocol.valid_nonce(salt):
            raise AuthError("server challenge invalid")
        verifier = protocol.derive_verifier(secret, salt)
        proof = protocol.handshake_proof(verifier, client_id, nonce_client,
                                         nonce_server)
        url = self.config.server_url + "/api/v1/link/handshake"
        status, payload = self._request_raw(
            "POST", url, {},
            json.dumps({"client_id": client_id, "nonce": nonce_client,
                        "proof": proof}).encode("utf-8"))
        result = _parse(status, payload)
        self._verifier = verifier
        self._session_key = protocol.session_key(
            verifier, client_id, nonce_client, nonce_server)
        self._nonce_client = nonce_client
        self._nonce_server = nonce_server
        try:
            self._expires_at = float(result.get("session_expires_at", 0.0))
        except (TypeError, ValueError):
            self._expires_at = 0.0
        if self._expires_at <= self._now():
            self.reset()
            raise AuthError("server returned an expired session")
        return result

    # -- signed requests -------------------------------------------------------

    def request(self, method: str, path: str, body: Optional[dict] = None,
                *, reauth: bool = True) -> dict[str, Any]:
        """Signed request; auto-reconnects once on session expiry.

        The signature covers the full path *including* the query string;
        the server reconstructs it from ``request.url`` so parameters
        cannot be stripped or reordered in flight.
        """
        if not self.connected:
            if not reauth:
                raise AuthExpired("not connected")
            self.connect()
        payload = b"" if body is None else json.dumps(body).encode("utf-8")
        url = self.config.server_url + path
        status, raw = self._signed_raw(method, path, url, payload)
        if status in (401,) and reauth:
            # Session may have been superseded/expired server-side.
            self.reset()
            self.connect()
            status, raw = self._signed_raw(method, path, url, payload)
        return _parse(status, raw)

    def _signed_raw(self, method: str, path: str, url: str,
                    payload: bytes) -> tuple[int, bytes]:
        timestamp = str(int(self._now()))
        nonce = self._nonce()
        signature = protocol.sign_request(
            self._session_key, method, path, payload, timestamp, nonce)
        headers = protocol.signature_headers(
            self.config.client_id, timestamp, nonce, signature)
        return self._request_raw(method, url, headers, payload)

    def _request_raw(self, method: str, url: str, headers: dict,
                     payload: bytes) -> tuple[int, bytes]:
        headers = dict(headers)
        headers.setdefault("Accept", "application/json")
        try:
            return self._sender(method, url, headers, payload,
                                self.config.request_timeout)
        except TransportError:
            raise

    # -- convenience ---------------------------------------------------------------

    def get(self, path: str, params: Optional[dict] = None) \
            -> dict[str, Any]:
        if params:
            from urllib.parse import urlencode
            query = urlencode({k: v for k, v in params.items()
                               if v not in (None, "")})
            if query:
                path = f"{path}?{query}"
        return self.request("GET", path)

    def post(self, path: str, body: Optional[dict] = None) \
            -> dict[str, Any]:
        return self.request("POST", path, body or {})


def _parse(status: int, payload: bytes) -> dict[str, Any]:
    """HTTP status + bytes -> parsed dict or a typed error."""
    text = payload.decode("utf-8", errors="replace")
    data: Any = None
    if text:
        try:
            data = json.loads(text)
        except ValueError:
            data = None
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        error = data["error"]
        code = str(error.get("code", "LINK_ERROR"))
        message = str(error.get("message", ""))
        if status == 401:
            if code == "AUTH_EXPIRED":
                raise AuthExpired(message or "link session expired")
            raise AuthError(message or "authentication failed")
        raise RequestError(message or code, code=code)
    if 200 <= status < 300:
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise ServerError("unexpected response shape")
        return data
    if status == 401:
        raise AuthError("authentication failed")
    if 400 <= status < 500:
        raise RequestError(f"request rejected (HTTP {status})")
    raise ServerError(f"server error (HTTP {status})")
