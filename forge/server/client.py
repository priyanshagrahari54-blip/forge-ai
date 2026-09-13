"""Forge Server thin client (G560).

A TRUE thin client: it can authenticate, submit typed task requests,
watch progress, and decide approvals. It has NO execution plane of its
own — every capability is a typed call over the server API. There is
no arbitrary command field anywhere in this module; the server's
strict schemas reject unknown fields as a second line of defense.

Login uses the server's challenge/response exchange (single-use
nonce + TTL) so a captured session request cannot be replayed, then
holds a bounded-TTL session token.

Stdlib only (urllib); safe on Python 3.8 and Windows.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional


class ForgeServerClientError(Exception):
    """A typed failure from the server (or from reaching it)."""

    def __init__(self, code: str, message: str,
                 http_status: int = 0) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


API_PREFIX = "/api/v1"
DEFAULT_TIMEOUT = 10.0


def _normalize_base_url(base_url: str) -> str:
    url = (base_url or "").strip().rstrip("/")
    if not url:
        raise ForgeServerClientError(
            "BAD_URL", "A server base URL is required (http://host:port).")
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    if url.endswith(API_PREFIX):
        return url
    return url + API_PREFIX


class ForgeServerClient:
    """Typed, authenticated client for the Forge Server API.

    ``token`` may be an API key or a session token; ``login()``
    exchanges an API key for a session token via the challenge
    handshake.
    """

    def __init__(self, base_url: str, token: str = "",
                 timeout: float = DEFAULT_TIMEOUT) -> None:
        self.base_url = _normalize_base_url(base_url)
        self.token = (token or "").strip()
        self.timeout = float(timeout or DEFAULT_TIMEOUT)
        self.principal: Optional[Dict[str, Any]] = None

    # -- transport ----------------------------------------------------------

    def _request(self, method: str, path: str,
                 payload: Optional[Dict[str, Any]] = None,
                 params: Optional[Dict[str, Any]] = None) -> Any:
        url = self.base_url + path
        if params:
            clean = {key: value for key, value in params.items()
                     if value not in (None, "")}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        request = urllib.request.Request(url, data=data,
                                         headers=headers, method=method)
        try:
            with urllib.request.urlopen(request,
                                        timeout=self.timeout) as response:
                body = response.read().decode("utf-8", "replace")
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read().decode("utf-8", "replace")
                parsed = json.loads(raw) if raw else {}
            except ValueError:
                parsed = {}
            error = parsed.get("error", {}) if isinstance(parsed, dict) \
                else {}
            raise ForgeServerClientError(
                error.get("code", "HTTP_%d" % exc.code),
                error.get("message", raw[:300] or "HTTP %d" % exc.code),
                http_status=int(exc.code)) from None
        except urllib.error.URLError as exc:
            raise ForgeServerClientError(
                "UNREACHABLE",
                "Cannot reach Forge Server at %s: %s"
                % (self.base_url, getattr(exc, "reason", exc)),
                http_status=0) from None
        except (TimeoutError, OSError) as exc:
            raise ForgeServerClientError(
                "TIMEOUT",
                "Forge Server request timed out: %s" % exc,
                http_status=0) from None

    # -- authentication (challenge/response) -----------------------------------

    def challenge(self) -> Dict[str, Any]:
        """Request a fresh single-use challenge nonce."""
        result = self._request("GET", "/auth/challenge")
        return result or {}

    def login(self, api_key: str = "") -> Dict[str, Any]:
        """Exchange an API key for a session token via challenge/response.

        The challenge is single-use and time-bounded on the server, so
        replaying this exchange (or a captured one) is refused.
        """
        key = (api_key or self.token or "").strip()
        if not key:
            raise ForgeServerClientError(
                "NO_KEY", "An API key is required for login().")
        challenge = self.challenge()
        nonce = challenge.get("nonce", "")
        previous_token = self.token
        self.token = key
        try:
            result = self._request(
                "POST", "/auth/sessions",
                payload={"challenge_nonce": nonce})
        except ForgeServerClientError:
            self.token = previous_token
            raise
        token = result.get("token", "")
        if not token:
            raise ForgeServerClientError(
                "NO_TOKEN", "Server did not return a session token.")
        self.token = token
        self.principal = (result.get("session") or {}).get("principal") \
            or None
        return result

    def whoami(self) -> Dict[str, Any]:
        result = self._request("GET", "/auth/whoami")
        self.principal = result
        return result

    # -- status ------------------------------------------------------------------

    def ping(self) -> bool:
        try:
            result = self._request("GET", "/ping")
        except ForgeServerClientError:
            return False
        return bool(result.get("ok"))

    def status(self) -> Dict[str, Any]:
        return self._request("GET", "/status")

    def health(self) -> Dict[str, Any]:
        return self._request("GET", "/health")

    def list_projects(self) -> Dict[str, Any]:
        return self._request("GET", "/projects")

    # -- tasks (typed only; no command field exists) ------------------------------

    def submit_task(self, project_id: str, requirement: str,
                    mode: str = "", priority: int = 0,
                    max_retries: Optional[int] = None) -> Dict[str, Any]:
        """Submit one typed task. The server's strict schema forbids
        any field beyond these — there is no command to inject."""
        payload: Dict[str, Any] = {
            "project_id": project_id,
            "requirement": requirement,
        }
        if mode:
            payload["mode"] = mode
        if priority:
            payload["priority"] = priority
        if max_retries is not None:
            payload["max_retries"] = max_retries
        result = self._request("POST", "/tasks", payload=payload)
        return result.get("task", result)

    def list_tasks(self, project_id: str = "", status: str = "",
                   limit: int = 50) -> Dict[str, Any]:
        return self._request(
            "GET", "/tasks",
            params={"project_id": project_id, "status": status,
                    "limit": limit})

    def task(self, task_id: str) -> Dict[str, Any]:
        result = self._request("GET", "/tasks/" + urllib.parse.quote(task_id))
        return result.get("task", result)

    def task_events(self, task_id: str, after: int = 0,
                    limit: int = 200, wait: float = 0.0) -> Dict[str, Any]:
        return self._request(
            "GET", "/tasks/" + urllib.parse.quote(task_id) + "/events",
            params={"after": after, "limit": limit, "wait": wait})

    def cancel_task(self, task_id: str) -> Dict[str, Any]:
        result = self._request(
            "POST", "/tasks/" + urllib.parse.quote(task_id) + "/cancel",
            payload={})
        return result.get("task", result)

    # -- approvals -------------------------------------------------------------------

    def pending_approvals(self, project_id: str = "") -> List[Dict[str, Any]]:
        result = self._request("GET", "/approvals",
                               params={"project_id": project_id})
        return list(result.get("approvals", []))

    def decide_approval(self, approval_id: str, approved: bool
                        ) -> Dict[str, Any]:
        result = self._request(
            "POST", "/approvals/" + urllib.parse.quote(approval_id)
            + "/decide",
            payload={"approved": bool(approved)})
        return result.get("approval", result)
