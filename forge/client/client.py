"""ForgeClient: the desktop-side facade for the Forge link (A81).

One object owns the whole client stack (config, signed transport,
connection manager, hybrid router, local executor) and exposes the
small API the desktop backend needs:

- :meth:`submit` — estimate -> route -> execute LOCAL (bounded ops) or
  submit SERVER (signed request);
- :meth:`snapshot` — everything the Server tab renders: connection
  status, tasks, queue, approvals, current stage/model/worker, events,
  verification result, and the client decision log;
- :meth:`restore` — reconnect and pull the full server state so
  *reopening the desktop restores the server task view* (requirement 9);
- approvals / pause / resume / cancel / retry proxies.

The client keeps no server-side state of its own: the server DB is the
single source of truth, so any number of reopenings converge on the
same view. Local task results live in a bounded in-memory list.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Callable, Optional

from forge.client.config import ClientConfig
from forge.client.connection import ConnectionManager
from forge.client.local_exec import LocalExecutor
from forge.client.resources import ResourceSnapshot, probe
from forge.client.router import ExecutionDecision, ServerStatus, decide
from forge.client.transport import LinkTransport
from forge.link.errors import AuthError, LinkError, LocalExecutionRefused
from forge.link.protocol import PROTOCOL_VERSION

#: Bounds for client-side UI state.
MAX_LOG_LINES = 500
MAX_LOCAL_TASKS = 50
MAX_EVENTS = 200

_TERMINAL_STATUSES = frozenset({"SUCCEEDED", "FAILED", "CANCELLED",
                                 "ROLLED_BACK"})


class ForgeClient:
    """Lightweight Forge Desktop client (no models, no pipeline)."""

    def __init__(self, config: ClientConfig, *,
                 transport: Optional[LinkTransport] = None,
                 connection: Optional[ConnectionManager] = None,
                 local_executor: Optional[LocalExecutor] = None,
                 resource_probe: Optional[Callable[[], ResourceSnapshot]] = None,
                 clock: Optional[Callable[[], float]] = None,
                 sleeper: Optional[Callable[[float]], None] = None) -> None:
        self.config = config
        self.clock = clock or time.time
        self._transport = transport or LinkTransport(config)
        self.connection = connection or ConnectionManager(
            self._transport, config.reconnect, clock=time.monotonic,
            sleeper=sleeper)
        self.local_executor = local_executor or LocalExecutor(config.local)
        self._probe = resource_probe or probe
        self._lock = threading.RLock()
        self._logs: deque = deque(maxlen=MAX_LOG_LINES)
        self._local_tasks: list[dict[str, Any]] = []
        self._local_seq = 0
        self._server_status = ServerStatus(reachable=False)
        self._last_snapshot: dict[str, Any] = {}
        self.connection.listeners.append(self._on_connection_event)
        self._log("info", f"Forge client {config.client_id or '(unset)'} "
                  f"protocol {PROTOCOL_VERSION}, mode={config.mode}")

    # -- logging -----------------------------------------------------------------

    def _log(self, level: str, message: str) -> None:
        stamp = time.strftime("%H:%M:%S", time.localtime(self.clock()))
        with self._lock:
            self._logs.append(f"[{stamp}] {level.upper()}: {message}")

    def logs(self, limit: int = MAX_LOG_LINES) -> list[str]:
        with self._lock:
            return list(self._logs)[-limit:]

    def _on_connection_event(self, state: str, detail: str) -> None:
        self._log("link", f"{state}" + (f" — {detail}" if detail else ""))

    # -- connection -----------------------------------------------------------------

    def connect(self, *, with_heartbeat: bool = True) -> bool:
        """Connect now (blocking, bounded by the reconnect policy)."""
        ok = self.connection.ensure_connected()
        if ok:
            self._update_server_status()
            if with_heartbeat:
                self.connection.start_heartbeat(
                    self.config.heartbeat_seconds,
                    poll=self._heartbeat_poll)
        return ok

    def disconnect(self) -> None:
        self.connection.disconnect()
        self._server_status = ServerStatus(reachable=False)

    def _heartbeat_poll(self) -> bool:
        info = self._transport.get("/api/v1/link/info")
        self._update_server_status(info)
        return True

    def _update_server_status(self, info: Optional[dict] = None) -> None:
        try:
            info = info or self._transport.get("/api/v1/link/info")
            self._server_status = ServerStatus(
                reachable=True,
                model_ready=bool(info.get("model_ready")),
                workers=int(info.get("workers", 0)))
        except (LinkError, ValueError):
            self._server_status = ServerStatus(reachable=False)

    @property
    def server_status(self) -> ServerStatus:
        return self._server_status

    def status(self) -> dict[str, Any]:
        return {
            "state": self.connection.state_name,
            "detail": self.connection.last_error,
            "server_url": self.config.server_url,
            "client_id": self.config.client_id,
            "project_id": self.config.project_id,
            "mode": self.config.mode,
            "server_reachable": self._server_status.reachable,
            "session_expires_at": self._transport.expires_at,
        }

    # -- routing + submission -------------------------------------------------------

    def decide(self, requirement: str) -> ExecutionDecision:
        """Public for tests/UI: what would happen for this requirement?"""
        return decide(self.config.mode, requirement, self.config.local,
                      self._server_status,
                      self._resources_snapshot(requirement))

    def _resources_snapshot(self, requirement: str) -> ResourceSnapshot:
        del requirement  # resources do not depend on the task text
        try:
            return self._probe()
        except Exception as exc:  # noqa: BLE001 - probe is advisory
            # Fail closed: report zero free resources so the router
            # refuses LOCAL for capacity reasons instead of crashing.
            self._log("warn", f"resource probe failed ({exc}); "
                              "failing closed for LOCAL execution")
            return ResourceSnapshot(cpus=0, total_ram_mb=0, free_ram_mb=0,
                                    source="unavailable")

    def submit(self, requirement: str, *,
               operation: str = "repo_summary", root: str = "") \
            -> dict[str, Any]:
        """Route a requirement and execute it.

        SERVER submissions run the full engineering pipeline remotely.
        LOCAL submissions run only the bounded light operations
        (:mod:`forge.client.local_exec`) — the router's LOCAL decision
        is the only gate that permits them.
        """
        decision = self.decide(requirement)
        self._log("route", f"{decision.execution} — {decision.reason}")
        if decision.is_refused:
            # Explicit refusal (policy/mode contract): surfaced, never
            # silently re-routed.
            raise LocalExecutionRefused(decision.reason)
        if decision.execution == "LOCAL":
            return self._submit_local(requirement, decision, operation,
                                      root)
        return self._submit_server(requirement, decision)

    def _submit_local(self, requirement: str, decision: ExecutionDecision,
                      operation: str, root: str) -> dict[str, Any]:
        if not root:
            self._log("error", "LOCAL execution needs a project folder")
            raise LocalExecutionRefused(
                "LOCAL execution requires a local project folder")
        started = time.time()
        try:
            result = self.local_executor.run(operation, root)
            status = "SUCCEEDED"
            error = ""
        except LocalExecutionRefused as exc:
            result, status, error = {}, "FAILED", str(exc)
            self._log("error", f"LOCAL refused: {exc}")
        except Exception as exc:  # noqa: BLE001 - surfaced, never hidden
            result, status, error = {}, "FAILED", f"local error: {exc}"
            self._log("error", f"LOCAL crashed: {exc}")
        local_id = self._next_local_id()
        task = {
            "id": local_id,
            "project_id": self.config.project_id,
            "requirement": requirement[:400],
            "status": status,
            "stage": "local-exec" if not error else "local-refused",
            "mode": "local",
            "actor": f"client:{self.config.client_id}",
            "model": "", "provider": "local-light",
            "error": error,
            "rollback": False, "files": [], "version": 1,
            "created_at": started, "started_at": started,
            "finished_at": time.time(),
            "execution": "LOCAL",
            "decision": decision.reason,
            "result": result,
        }
        with self._lock:
            self._local_tasks.append(task)
            del self._local_tasks[:-MAX_LOCAL_TASKS]
        return {"task": task, "execution": "LOCAL"}

    def _submit_server(self, requirement: str, decision: ExecutionDecision) \
            -> dict[str, Any]:
        if not self._server_status.reachable and not self.connection \
                .connect():
            self._log("error", "server unreachable; task not submitted")
            raise LinkError("server unreachable; task not submitted",
                            code="SERVER_UNREACHABLE")
        payload = {
            "requirement": requirement,
            "mode": "",  # server decides the authoritative mode
            "execution": "SERVER",
            "decision": decision.reason,
        }
        result = self._transport.post("/api/v1/link/tasks", payload)
        self._server_status = ServerStatus(reachable=True)
        self._log("task", f"server task {result.get('task', {}).get('id')}"
                  f" accepted (effective_mode="
                  f"{result.get('effective_mode', '?')})")
        return {"task": dict(result.get("task", {})),
                "execution": "SERVER",
                "requested_mode": result.get("requested_mode", ""),
                "effective_mode": result.get("effective_mode", "")}

    def _next_local_id(self) -> str:
        with self._lock:
            self._local_seq += 1
            return f"local-{self._local_seq:06d}"

    # -- snapshot (rendered by the desktop Server tab) -----------------------------

    def snapshot(self, *, selected_task: str = "", full: bool = False) \
            -> dict[str, Any]:
        """One bounded payload for the UI. Never raises: failures land
        in ``connection.detail`` and the log ring."""
        status = self.status()
        tasks: list[dict[str, Any]] = []
        approvals: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []
        cursor = 0
        queue_depth = 0
        server_info: dict[str, Any] = {}
        if self._server_status.reachable or self.connection.state_name \
                == "CONNECTED":
            try:
                since = 0 if (full or selected_task
                              and selected_task != self._last_snapshot.get(
                                  "selected_task")) \
                    else int(self._last_snapshot.get("event_cursor", 0))
                state = self._transport.get(
                    "/api/v1/link/state",
                    params={"since": since, "task": selected_task})
                tasks = list(state.get("tasks", []))
                approvals = list(state.get("approvals", []))
                events = list(state.get("events", []))[:MAX_EVENTS]
                cursor = int(state.get("event_cursor", 0))
                queue_depth = int(state.get("queued", 0))
                server_info = dict(state.get("server_info", {}))
                self._server_status = ServerStatus(
                    reachable=True,
                    model_ready=bool(server_info.get("model_ready")),
                    workers=int(server_info.get("workers", 0)))
                status["state"] = self.connection.state_name
            except AuthError as exc:
                # Session expired/superseded mid-poll: this is exactly
                # what the auto-reconnect exists for. Try once, here,
                # synchronously; if it fails the state machine takes
                # over and the next poll retries. Never re-raise from a
                # UI refresh.
                self._log("warn", f"session rejected during refresh: {exc}")
                self.connection.reset()
                if self.connection.connect():
                    return self.snapshot(selected_task=selected_task,
                                         full=full)
                status = self.status()
                status["state"] = self.connection.state_name
                status["detail"] = self.connection.last_error
            except LinkError as exc:
                self._log("error", f"refresh failed: {exc}")
                self._server_status = ServerStatus(reachable=False)
                status["state"] = "RECONNECTING"
                status["detail"] = str(exc)
        with self._lock:
            local = [dict(t) for t in self._local_tasks]
        merged = sorted(local + tasks,
                        key=lambda t: (-t.get("created_at", 0.0), t["id"]))
        verification: dict[str, Any] = {}
        # Only fetch verification while the server is actually reachable
        # (a dead server must not cost a doomed request on every poll).
        if selected_task and not selected_task.startswith("local-") \
                and self._server_status.reachable:
            try:
                verification = self._transport.get(
                    f"/api/v1/link/tasks/{selected_task}/verification")
            except AuthError:
                verification = {"error": "authentication expired"}
            except LinkError as exc:
                verification = {"error": str(exc)}
        snapshot = {
            "connection": status,
            "server_info": server_info or self._server_info_guess(),
            "tasks": merged,
            "queue": {"depth": queue_depth},
            "approvals": approvals,
            "events": events,
            "event_cursor": cursor,
            "selected_task": selected_task,
            "verification": verification,
            "logs": self.logs(120),
        }
        self._last_snapshot = snapshot
        return snapshot

    def _server_info_guess(self) -> dict[str, Any]:
        return {"workers": self._server_status.workers,
                "model_ready": self._server_status.model_ready,
                "models": [], "server": "forge-server"}

    # -- restore (requirement 9) ---------------------------------------------------

    def restore(self) -> dict[str, Any]:
        """Reconnect and pull the full server state (reopen path).

        Returns the same snapshot shape as :meth:`snapshot` with the
        full task history, or a disconnected snapshot when the server
        cannot be reached yet (the UI keeps polling; nothing is lost —
        the server kept running the tasks).
        """
        self._log("info", "restoring server task state")
        if not self.connection.connect():
            self._log("warn", f"restore: not connected "
                      f"({self.connection.last_error})")
            return self.snapshot(full=True)
        self._update_server_status()
        self.connection.start_heartbeat(
            self.config.heartbeat_seconds, poll=self._heartbeat_poll)
        return self.snapshot(full=True)

    # -- task + approval proxies --------------------------------------------------

    def decide_approval(self, approval_id: str, approved: bool) \
            -> dict[str, Any]:
        result = self._transport.post(
            f"/api/v1/link/approvals/{approval_id}/decide",
            {"approved": bool(approved)})
        self._log("approval",
                  f"{approval_id}: {'approved' if approved else 'denied'}")
        return result

    def mutate_task(self, task_id: str, operation: str) -> dict[str, Any]:
        if operation not in ("pause", "resume", "cancel", "retry"):
            raise LinkError(f"unknown operation {operation!r}",
                            code="INVALID_REQUEST")
        result = self._transport.post(
            f"/api/v1/link/tasks/{task_id}/{operation}", {})
        self._log("task", f"{task_id}: {operation}")
        return result
