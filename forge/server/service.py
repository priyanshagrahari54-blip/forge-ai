"""LinkService: the Forge Server authority for desktop clients (A81).

Every desktop-client request flows through this service. It owns:

- **registration** — the operator registers a desktop (e.g. the G560)
  against one server-side project and receives a one-time secret;
  only the salted verifier is persisted;
- **handshake** — challenge/response proof of secret possession with
  single-use challenges and one active link session per client;
- **per-request verification** — HMAC signatures over
  method|path|body|timestamp|nonce with a bounded freshness window and a
  replay cache; verification yields the bound ControlPlane session;
- **authoritative authorization** — the server decides the effective
  permission mode: the client's requested mode is clamped to the
  client's registered ``max_mode`` (server-side policy is never loosened
  by a client);
- **task operations** — submit/list/status/logs/events/verification/
  report/pause/resume/cancel/retry, all delegated to the existing
  :class:`~forge.control.control_plane.ControlPlane` (whose worker pool
  keeps running when the client disconnects), plus the client-facing
  state snapshot used for restore-on-reopen.

No method here accepts shell commands or arbitrary payloads: the surface
is a fixed vocabulary of typed operations.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from forge.control.control_plane import (
    ControlPlane,
    InvalidRequest,
    TaskNotFound,
)
from forge.control.db import Database
from forge.control.sessions import Session
from forge.link import protocol
from forge.link.errors import AuthError, AuthExpired, RequestError
from forge.server.store import LinkStore

#: Link sessions live at most this long; a new handshake re-derives.
LINK_SESSION_TTL_SECONDS = 12 * 3600.0

#: Execution-origin labels recorded per task for the desktop UI.
EXECUTION_LOCAL = "LOCAL"
EXECUTION_SERVER = "SERVER"
EXECUTION_HYBRID = "HYBRID"

_VALID_MODES = ("safe", "assisted", "autonomous")


@dataclass
class LinkServerConfig:
    #: Maximum lifetime of one handshake-derived link session.
    session_ttl: float = LINK_SESSION_TTL_SECONDS
    #: Signature freshness window (both directions of server time).
    timestamp_window: float = protocol.TIMESTAMP_WINDOW_SECONDS
    #: Bounded recent-task window returned by the state snapshot.
    snapshot_task_limit: int = 50


class LinkService:
    """Server-side authority for the desktop-client link."""

    def __init__(self, plane: ControlPlane,
                 config: LinkServerConfig | None = None) -> None:
        self.plane = plane
        self.config = config or LinkServerConfig()
        # Own connection to the shared cockpit database (WAL, thread-safe).
        self.store = LinkStore(Database(plane.config.db_path))


    # -- client management (operator, on the server) ------------------------

    def register_client(self, client_id: str, project_id: str, *,
                        name: str = "", max_mode: str = "assisted",
                        secret: str = "") -> dict[str, str]:
        """Register a desktop client; returns the one-time secret.

        ``secret`` may be supplied (deterministic tests); otherwise a
        fresh urlsafe secret is generated. Only the verifier is stored.
        """
        protocol.require_client_id(client_id)
        secret = protocol.require_secret(secret) if secret \
            else protocol.new_secret()
        salt = protocol.new_salt()
        verifier = protocol.derive_verifier(secret, salt)
        try:
            self.store.create_client(client_id, project_id, salt, verifier,
                                     name=name, max_mode=max_mode)
        except Exception as exc:
            raise RequestError(str(exc), code="CLIENT_EXISTS") from None
        # NOTE: the plaintext secret is returned exactly once and never
        # logged or persisted server-side.
        return {"client_id": client_id, "project_id": project_id,
                "max_mode": max_mode, "secret": secret}

    def rotate_client_secret(self, client_id: str,
                             secret: str = "") -> dict[str, str]:
        client = self.store.get_client(client_id)
        if client is None:
            raise RequestError(f"unknown client: {client_id}",
                               code="CLIENT_NOT_FOUND")
        secret = protocol.require_secret(secret) if secret \
            else protocol.new_secret()
        salt = protocol.new_salt()
        verifier = protocol.derive_verifier(secret, salt)
        self.store.rotate_client(client_id, salt, verifier)
        return {"client_id": client_id, "secret": secret}

    def revoke_client(self, client_id: str) -> dict[str, str]:
        try:
            self.store.revoke_client(client_id)
        except Exception as exc:
            raise RequestError(str(exc), code="CLIENT_NOT_FOUND") from None
        return {"client_id": client_id, "status": "revoked"}

    def list_clients(self) -> list[dict[str, Any]]:
        return self.store.list_clients()

    # -- handshake ------------------------------------------------------------

    def challenge(self, client_id: str, nonce_client: str, *,
                  now: float | None = None) -> dict[str, Any]:
        """Step 1: issue a server nonce (+ public salt) for the client's
        nonce.

        The salt is public registration data (like a password-database
        salt): sending it lets the client derive its verifier without a
        second out-of-band copy. Unknown or revoked clients get the
        *same* reply shape with an unusable random nonce + random salt,
        so client enumeration gains nothing.
        """
        now = time.time() if now is None else now
        nonce_server = protocol.new_nonce()
        decoy_salt = protocol.new_salt()
        nonce_client = _clean_nonce(nonce_client)
        client = self.store.get_client(client_id) if protocol \
            .valid_client_id(client_id) else None
        if (client is None or client.get("status") != "active"
                or nonce_client is None):
            # Decoy challenge: indistinguishable, never verifiable.
            return {"server_nonce": nonce_server, "salt": decoy_salt,
                    "ttl": 0.0, "server_time": now}
        self.store.store_challenge(client_id, nonce_client, nonce_server,
                                   now=now)
        return {"server_nonce": nonce_server, "salt": client["salt"],
                "ttl": 120.0, "server_time": now}

    def handshake(self, client_id: str, nonce_client: str, proof: str, *,
                  now: float | None = None) -> dict[str, Any]:
        """Step 2: verify the proof, bind one ControlPlane session.

        Returns the session expiry and a first server-info payload. All
        failures are :class:`AuthError` (bad proof) — never a hint about
        *why* beyond that.
        """
        now = time.time() if now is None else now
        client = self.store.get_client(client_id) \
            if protocol.valid_client_id(client_id) else None
        if client is None or client.get("status") != "active":
            raise AuthError("authentication failed")
        nonce_client = _clean_nonce(nonce_client)
        if nonce_client is None or not isinstance(proof, str) or not proof:
            raise AuthError("authentication failed")
        nonce_server = self.store.take_challenge(
            client_id, nonce_client, now=now)
        if nonce_server is None:
            raise AuthError("authentication failed")
        expected = protocol.handshake_proof(
            client["verifier"], client_id, nonce_client, nonce_server)
        if not protocol.constant_time_equals(expected, proof):
            raise AuthError("authentication failed")
        project_id = client["project_id"]
        try:
            session, _token = self.plane.create_session(
                f"link-{client_id}", project_id,
                profile=self._authoritative_mode("", client))
        except InvalidRequest as exc:
            raise RequestError(str(exc), code="INVALID_REQUEST") from None
        expires_at = min(now + self.config.session_ttl,
                         session.expires_at)
        self.store.store_session(
            client_id, nonce_client, nonce_server, session.id,
            started_at=now, expires_at=expires_at)
        self.store.touch_client(client_id, now=now)
        return {
            "client_id": client_id,
            "project_id": project_id,
            "session_expires_at": expires_at,
            "server_time": now,
            "server_info": self.server_info(),
        }

    # -- per-request verification ----------------------------------------------

    def verify_request(self, client_id: str, timestamp: str, nonce: str,
                       signature: str, method: str, path: str,
                       body: bytes, *,
                       now: float | None = None) -> Session:
        """Verify a signed request; returns the bound ControlPlane session.

        Failures raise :class:`AuthError` / :class:`AuthExpired` and
        never leak which check failed (constant message).
        """
        now = time.time() if now is None else now
        client = self.store.get_client(client_id) \
            if protocol.valid_client_id(client_id) else None
        if client is None or client.get("status") != "active":
            raise AuthError("authentication required")
        link_session = self.store.get_session(client_id, now=now)
        if link_session is None:
            raise AuthExpired("link session expired")
        if not protocol.valid_nonce(nonce) or not protocol \
                .timestamp_fresh(timestamp, now,
                                 window=self.config.timestamp_window):
            raise AuthError("authentication required")
        key = protocol.session_key(client["verifier"], client_id,
                                   link_session["nonce_client"],
                                   link_session["nonce_server"])
        expected = protocol.sign_request(key, method, path, body,
                                         timestamp, nonce)
        if not protocol.constant_time_equals(expected, signature or ""):
            raise AuthError("authentication required")
        # Only a signature-valid request may claim its nonce (an
        # eavesdropper must not be able to poison a nonce to deny
        # service to the real client).
        if self.store.nonce_seen(client_id, nonce, now=now,
                                 window=self.config.timestamp_window * 4):
            raise AuthError("authentication required")
        session = self.plane.sessions.get(link_session["session_id"])
        if session is None or not session.active:
            raise AuthExpired("link session expired")
        self.store.touch_client(client_id, now=now)
        return session

    # -- task operations (delegated to the ControlPlane) ------------------------

    def submit_task(self, session: Session, client_id: str, requirement: str,
                    *, mode: str = "", execution: str = EXECUTION_SERVER,
                    decision: str = "") -> dict[str, Any]:
        """Submit an engineering task. Server authorization is authoritative:
        the effective mode is clamped to the client's registered max_mode."""
        client = self.store.get_client(client_id)
        if client is None or client.get("status") != "active":
            raise AuthError("authentication required")
        effective = self._authoritative_mode(mode, client)
        try:
            run = self.plane.submit_task(session, requirement,
                                         mode=effective)
        except InvalidRequest as exc:
            raise RequestError(str(exc), code="INVALID_REQUEST") from None
        self.store.set_task_origin(run.id, client_id, execution, decision)
        return {"task": _run_dict(run), "requested_mode": mode,
                "effective_mode": effective, "execution": execution}

    def list_tasks(self, session: Session, *, status: str = "",
                   limit: int = 50) -> list[dict[str, Any]]:
        runs, _total = self.plane.list_tasks(session, status=status,
                                             limit=min(200, max(1, limit)))
        return [_with_origin(_run_dict(run), self.store)
                for run in runs]

    def get_task(self, session: Session, task_id: str) -> dict[str, Any]:
        return _with_origin(_run_dict(self.plane.get_task(session, task_id)),
                            self.store)

    def get_task_logs(self, session: Session, task_id: str) -> dict[str, Any]:
        try:
            return dict(self.plane.get_task_logs(session, task_id))
        except TaskNotFound:
            raise RequestError(f"unknown task: {task_id}",
                               code="TASK_NOT_FOUND") from None

    def get_task_events(self, session: Session, task_id: str, *,
                        after: int = 0) -> tuple[list[dict[str, Any]], int]:
        try:
            return self.plane.get_task_events(session, task_id,
                                              after=max(0, int(after)))
        except TaskNotFound:
            raise RequestError(f"unknown task: {task_id}",
                               code="TASK_NOT_FOUND") from None

    def get_verification(self, session: Session,
                         task_id: str) -> dict[str, Any]:
        try:
            return dict(self.plane.get_verification(session, task_id))
        except TaskNotFound:
            raise RequestError(f"unknown task: {task_id}",
                               code="TASK_NOT_FOUND") from None

    def get_task_report(self, session: Session,
                        task_id: str) -> dict[str, Any]:
        try:
            return dict(self.plane.get_task_report(session, task_id))
        except TaskNotFound:
            raise RequestError(f"unknown task: {task_id}",
                               code="TASK_NOT_FOUND") from None

    def list_approvals(self, session: Session) -> list[dict[str, Any]]:
        return list(self.plane.list_approvals(session))

    def decide_approval(self, session: Session, approval_id: str,
                        approved: bool) -> dict[str, Any]:
        try:
            if approved:
                return dict(self.plane.approve_request(session, approval_id))
            return dict(self.plane.deny_request(session, approval_id))
        except Exception as exc:  # control-plane error taxonomy
            code = getattr(exc, "code", "APPROVAL_ERROR")
            raise RequestError(str(exc), code=code) from None

    def mutate_task(self, session: Session, operation: str, task_id: str) \
            -> dict[str, Any]:
        plane = self.plane
        try:
            if operation == "pause":
                run = plane.pause_task(session, task_id)
            elif operation == "resume":
                run = plane.resume_task(session, task_id)
            elif operation == "cancel":
                run = plane.cancel_task(session, task_id)
            elif operation == "retry":
                run = plane.retry_task(session, task_id)
            else:
                raise RequestError(f"unknown operation: {operation!r}",
                                   code="INVALID_REQUEST")
        except TaskNotFound:
            raise RequestError(f"unknown task: {task_id}",
                               code="TASK_NOT_FOUND") from None
        except Exception as exc:
            code = getattr(exc, "code", "TASK_ERROR")
            raise RequestError(str(exc), code=code) from None
        return {"task": _with_origin(_run_dict(run), self.store)}

    # -- client-facing state snapshot (restore-on-reopen) ---------------------

    def state_snapshot(self, session: Session, *, since_seq: int = 0,
                       selected_task: str = "") -> dict[str, Any]:
        """Everything a (re)opening desktop needs in one bounded payload."""
        tasks = self.list_tasks(session,
                                limit=self.config.snapshot_task_limit)
        active = [t for t in tasks
                  if t["status"] in ("QUEUED", "RUNNING", "PAUSED",
                                     "WAITING_APPROVAL")]
        queued = 0
        project = self.plane.projects.get(session.project_id)
        if project is not None:
            queue = self.plane._queues.get(project.id)  # noqa: SLF001
            if queue is not None:
                queued = len(queue.queued())
        approvals = self.list_approvals(session)
        events: list[dict[str, Any]] = []
        latest = since_seq
        focus = selected_task or (active[0]["id"] if active else "")
        if focus:
            try:
                events, latest = self.get_task_events(
                    session, focus, after=since_seq)
            except RequestError:
                events, latest = [], since_seq
        return {
            "tasks": tasks,
            "active_tasks": active,
            "queued": queued,
            "approvals": approvals,
            "events": events,
            "event_cursor": latest,
            "selected_task": focus,
            "server_info": self.server_info(),
        }

    # -- server info ------------------------------------------------------------

    def server_info(self) -> dict[str, Any]:
        """Bounded, secret-free description of the server for the UI and
        for the client's HYBRID policy (model availability, workers)."""
        plane = self.plane
        readiness: dict[str, Any] = {}
        try:
            readiness = plane.model_readiness(probe_network=False) \
                if hasattr(plane, "model_readiness") else {}
        except Exception:
            readiness = {}
        usable = readiness.get("usable_models") or []
        workers = int(getattr(plane.config, "max_workers", 1))
        active = plane.running
        return {
            "server": "forge-server",
            "link_protocol": protocol.PROTOCOL_VERSION,
            "workers": workers,
            "workers_running": bool(active),
            "models": [str(m) for m in usable][:8],
            "model_ready": bool(usable),
        }

    # -- authoritative mode ---------------------------------------------------

    def _authoritative_mode(self, requested: str, client: dict[str, Any]) \
            -> str:
        """Clamp the client's requested mode to the server-side ceiling.

        The client can only *tighten* (request a safer mode); it can
        never loosen the server's decision. Empty request -> ceiling.
        """
        ceiling = client.get("max_mode") or "assisted"
        if ceiling not in _VALID_MODES:
            ceiling = "assisted"
        requested = (requested or "").strip().lower()
        if not requested:
            return ceiling
        if requested not in _VALID_MODES:
            raise RequestError(f"Unknown mode: {requested!r}",
                               code="INVALID_REQUEST")
        return requested if _MODE_ORDER[requested] <= _MODE_ORDER[ceiling] \
            else ceiling


_MODE_ORDER = {"safe": 0, "assisted": 1, "autonomous": 2}


def _clean_nonce(value: Any) -> str | None:
    if not isinstance(value, str) or not protocol.valid_nonce(value):
        return None
    return value


def _run_dict(run) -> dict[str, Any]:
    """Run -> bounded plain dict (same shape as the cockpit task views)."""
    try:
        files = list(run.files())
    except Exception:
        files = []
    return {
        "id": run.id,
        "project_id": run.project_id,
        "requirement": run.requirement,
        "status": run.status.value,
        "stage": run.stage,
        "mode": run.mode,
        "actor": run.actor,
        "model": run.model,
        "provider": run.provider,
        "error": run.error,
        "rollback": run.rollback,
        "files": files,
        "version": run.version,
        "created_at": run.created_at,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
    }



def _with_origin(task: dict[str, Any], store: LinkStore) -> dict[str, Any]:
    origin = store.get_task_origin(task.get("id", ""))
    task["execution"] = (origin or {}).get("execution", "")
    task["decision"] = (origin or {}).get("decision", "")
    return task
