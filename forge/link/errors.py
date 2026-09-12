"""Typed link failures shared by client and server (A81).

Every failure carries a stable machine ``code`` so the desktop UI can
react (e.g. ``AUTH_FAILED`` means "fix credentials", never "retry").
"""
from __future__ import annotations


class LinkError(RuntimeError):
    """Base class for link failures."""

    code = "LINK_ERROR"

    def __init__(self, message: str = "", *, code: str = "") -> None:
        super().__init__(message or self.code)
        if code:
            self.code = code


class TransportError(LinkError):
    """Could not reach the server (offline, DNS, refused, timeout)."""

    code = "TRANSPORT_ERROR"


class AuthError(LinkError):
    """Server rejected the credentials or the signature."""

    code = "AUTH_FAILED"


class AuthExpired(LinkError):
    """Link session expired or was superseded; re-handshake required."""

    code = "AUTH_EXPIRED"


class RequestError(LinkError):
    """Server answered with a 4xx (bad request, not found, denied)."""

    code = "REQUEST_ERROR"


class ServerError(LinkError):
    """Server answered with a 5xx or an unparseable response."""

    code = "SERVER_ERROR"


class LocalExecutionRefused(LinkError):
    """The lightweight client refused a LOCAL execution (fail closed)."""

    code = "LOCAL_REFUSED"
