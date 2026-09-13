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
from typing import Any, Dict, Iterator, List, Optional


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

    # -- models (Session 11: typed registry views; no local models here) --------
    #
    # A thin client holds no weights and runs no inference of its own. These
    # calls only *read* the server's model registry and *ask* the server to
    # verify/load/unload; every verdict comes back from the server, which owns
    # policy, resources and audit.

    def list_models(self, backend: str = "", capability: str = "",
                    usable_only: bool = False,
                    discover: bool = False) -> Dict[str, Any]:
        """List registered models (optionally triggering discovery)."""
        return self._request(
            "GET", "/models",
            params={"backend": backend, "capability": capability,
                    "usable_only": "1" if usable_only else "",
                    "discover": "1" if discover else ""})

    def model_status(self, model_id: str = "") -> Dict[str, Any]:
        """Residency/verification detail for one model (or the whole cache)."""
        return self._request("GET", "/models/status",
                             params={"model_id": model_id})

    def verify_models(self, model_id: str = "",
                      backend: str = "") -> Dict[str, Any]:
        """Ask the server to verify a model (or every registered model).

        Verification is real: fingerprint, backend health and an identity
        probe generation. A model that fails stays ``unverified`` and is never
        selected — the server does not promote CONFIGURED to READY.
        """
        return self._request("POST", "/models/verify",
                             payload={"model_id": model_id,
                                      "backend_id": backend})

    def load_model(self, model_id: str,
                   timeout: Optional[float] = None) -> Dict[str, Any]:
        return self._request("POST", "/models/load",
                             payload={"model_id": model_id,
                                      "timeout": timeout})

    def unload_model(self, model_id: str,
                     force: bool = False) -> Dict[str, Any]:
        return self._request("POST", "/models/unload",
                             payload={"model_id": model_id,
                                      "force": bool(force)})

    # -- inference (typed requests only) ----------------------------------------

    def generate(self, prompt: str, *, context: str = "", capability: str = "",
                 model: str = "", backend: str = "", task: str = "",
                 max_output_tokens: Optional[int] = None,
                 temperature: Optional[float] = None,
                 timeout: Optional[float] = None, task_id: str = "",
                 attempt_id: str = "", trace_id: str = "",
                 classification: str = "",
                 allow_deterministic: bool = True) -> Dict[str, Any]:
        """One bounded generation on the server.

        The response is honest about provenance: ``neural`` says whether a
        model produced the text, ``model_id``/``backend_id`` say which one,
        ``state`` says why it ended, and ``routing`` explains the selection.
        A refusal (``needs_model`` / ``resource_denied`` / ``policy_denied``)
        comes back as ``success=False`` with no invented text.
        """
        payload = self._inference_payload(
            prompt, context=context, capability=capability, model=model,
            backend=backend, task=task, max_output_tokens=max_output_tokens,
            temperature=temperature, timeout=timeout, task_id=task_id,
            attempt_id=attempt_id, trace_id=trace_id,
            classification=classification,
            allow_deterministic=allow_deterministic)
        return self._request("POST", "/inference/generate", payload=payload)

    def start_stream(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
        """Start a server-side stream; returns ``stream_id`` + first events."""
        payload = self._inference_payload(prompt, **kwargs)
        return self._request("POST", "/inference/stream", payload=payload)

    def stream_events(self, stream_id: str, after: int = 0,
                      wait: float = 0.0) -> Dict[str, Any]:
        """Long-poll a stream from a cursor (exact replay, no duplicates)."""
        return self._request(
            "GET", "/inference/streams/" + urllib.parse.quote(stream_id)
            + "/events",
            params={"after": int(after), "wait": self._poll_wait(wait)})

    def iter_stream(self, stream_id: str, *, after: int = 0, wait: float = 2.0,
                    deadline_seconds: float = 300.0,
                    max_events: int = 100000
                    ) -> Iterator[Dict[str, Any]]:
        """Yield stream events from a cursor until the stream is done.

        Bounded on every axis: a wall-clock deadline, an event count, and the
        server's own terminal event. Reconnection is safe — pass the last
        ``after`` cursor and replay continues exactly where it stopped.
        """
        import time as _time

        cursor = int(after)
        emitted = 0
        limit = _time.monotonic() + max(0.0, float(deadline_seconds))
        while emitted < int(max_events):
            if _time.monotonic() >= limit:
                raise ForgeServerClientError(
                    "STREAM_DEADLINE",
                    "stream %s did not finish within %.0fs (cursor %d)"
                    % (stream_id, float(deadline_seconds), cursor))
            batch = self.stream_events(stream_id, after=cursor, wait=wait)
            for event in batch.get("events", []):
                emitted += 1
                cursor = int(batch.get("after", cursor))
                yield event
            cursor = int(batch.get("after", cursor))
            if batch.get("done"):
                return

    def stream(self, prompt: str, *, on_delta: Optional[Any] = None,
               wait: float = 2.0, deadline_seconds: float = 300.0,
               **kwargs: Any) -> Dict[str, Any]:
        """Run a streamed generation and return the aggregated outcome.

        ``on_delta(text, event)`` is called for every chunk, so a thin client
        can render progress. The returned dict always reports the truth:
        ``complete`` is only true when the stream reached its terminal event
        without truncation or error.
        """
        started = self.start_stream(prompt, **kwargs)
        stream_id = str(started.get("stream_id", ""))
        parts: List[str] = []
        count = 0

        def _consume(batch: List[Dict[str, Any]]) -> int:
            seen = 0
            for event in batch:
                seen += 1
                delta = str(event.get("delta", "") or "")
                if delta:
                    parts.append(delta)
                    if on_delta is not None:
                        try:
                            on_delta(delta, event)
                        except Exception:
                            pass  # a renderer must never break the stream
            return seen

        count += _consume(list(started.get("events", [])))
        cursor = int(started.get("after", 0))
        done = bool(started.get("done"))
        final: Dict[str, Any] = {}
        while not done:
            batch = self.stream_events(stream_id, after=cursor, wait=wait)
            count += _consume(list(batch.get("events", [])))
            cursor = int(batch.get("after", cursor))
            done = bool(batch.get("done"))
            final = batch
            if count > 100000:
                raise ForgeServerClientError(
                    "STREAM_TOO_LARGE",
                    "stream %s exceeded the client event bound" % stream_id)
        text = "".join(parts)
        #: The final snapshot (sent when the stream is done) supersedes the
        #: one captured at start, which cannot know how the stream ended.
        stream_state = dict(final.get("stream") or started.get("stream") or {})
        #: The server attaches the final (text-free) result summary once the
        #: stream is done, so a thin client can report real provenance:
        #: which model answered, whether it was neural, and why it ended.
        summary = dict(final.get("result") or {})
        body = {
            "stream_id": stream_id,
            "request_id": final.get("request_id",
                                    started.get("request_id", "")),
            "model_id": final.get("model_id", started.get("model_id", "")),
            "backend_id": final.get("backend_id",
                                    started.get("backend_id", "")),
            "text": text,
            "chars": len(text),
            "events": count,
            "state": final.get("state", ""),
            "error": final.get("error", ""),
            "error_code": final.get("error_code", ""),
            "done": True,
            "complete": bool(stream_state.get("complete"))
            if stream_state else not bool(final.get("error_code")),
            "dropped_events": int(final.get("dropped_events", 0) or 0),
            "stream": stream_state,
        }
        for key, value in summary.items():
            if key not in ("text", "observations"):
                body.setdefault(key, value)
        #: Deltas were already handed to ``on_delta``; the caller decides
        #: whether to print the assembled text again.
        body["echoed"] = bool(parts) and on_delta is not None
        return body

    def cancel_inference(self, request_id: str,
                         reason: str = "operator") -> Dict[str, Any]:
        return self._request("POST", "/inference/cancel",
                             payload={"request_id": request_id,
                                      "reason": reason})

    def inference_status(self) -> Dict[str, Any]:
        return self._request("GET", "/inference/status")

    # -- inference helpers -------------------------------------------------------

    @staticmethod
    def _inference_payload(prompt: str, *, context: str = "",
                           capability: str = "", model: str = "",
                           backend: str = "", task: str = "",
                           max_output_tokens: Optional[int] = None,
                           temperature: Optional[float] = None,
                           timeout: Optional[float] = None, task_id: str = "",
                           attempt_id: str = "", trace_id: str = "",
                           classification: str = "",
                           hardware_profile: str = "",
                           network_policy: str = "",
                           allow_deterministic: bool = True,
                           **ignored: Any) -> Dict[str, Any]:
        """Build a typed request body (unknown kwargs are refused, not sent)."""
        if ignored:
            raise ForgeServerClientError(
                "BAD_REQUEST",
                "unsupported inference parameter(s): %s"
                % ", ".join(sorted(str(key) for key in ignored)[:8]))
        payload: Dict[str, Any] = {"prompt": str(prompt or "")}
        optional = {
            "context": context, "capability": capability, "model": model,
            "backend": backend, "task": task, "task_id": task_id,
            "attempt_id": attempt_id, "trace_id": trace_id,
            "classification": classification,
            "hardware_profile": hardware_profile,
            "network_policy": network_policy,
            "max_output_tokens": max_output_tokens,
            "temperature": temperature, "timeout": timeout,
        }
        for key, value in optional.items():
            if value not in (None, ""):
                payload[key] = value
        payload["allow_deterministic"] = bool(allow_deterministic)
        return payload

    def _poll_wait(self, wait: float) -> float:
        """Keep a long poll inside the client's own socket timeout."""
        ceiling = max(0.0, float(self.timeout) - 1.0)
        return max(0.0, min(float(wait or 0.0), ceiling))
