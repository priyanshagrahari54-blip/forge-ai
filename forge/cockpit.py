"""Browser cockpit backend interfaces (A33).

:class:`CockpitService` exposes the task, permission, approval, event, log,
model, and agent operations a future web UI needs — task submission, task
status, the approval queue, approval decisions with token minting, the audit
event stream, and run summaries. Storage is in-memory; runs execute
synchronously through the normal :class:`Supervisor
<forge.core.supervisor.Supervisor>` path, so cockpit-driven work passes the
exact same permission gates as every other caller.

No frontend is built here: only the clean backend interfaces the UI will
consume.
"""
from __future__ import annotations

import time
from typing import Any, Iterator

from forge.security.approvals import ApprovalRequest, ApprovalStore
from forge.security.audit import AuditLog


class CockpitService:
    """In-memory backend for task execution and permission operations."""

    def __init__(self, root: str = ".", *, policy=None,
                 store: ApprovalStore | None = None,
                 audit: AuditLog | None = None,
                 model_policy=None) -> None:
        self.root = root
        self.policy = policy
        self.store = store if store is not None else ApprovalStore()
        self.audit = audit if audit is not None else AuditLog()
        self.model_policy = model_policy
        self._tasks: dict[str, dict[str, Any]] = {}

    # -- tasks ------------------------------------------------------------------

    def submit_task(self, requirement: str, *, approved: bool = False,
                    mode: str = "assisted", fabric=None,
                    approval_token_id: str = "") -> dict[str, Any]:
        """Run a requirement through the supervisor and record the outcome."""
        from forge.core.supervisor import Supervisor
        from forge.security.permissions import OperationMode

        outcome = Supervisor("cockpit", self.root).run(
            requirement, approved=approved, fabric=fabric,
            mode=OperationMode(mode), policy=self.policy,
            approval_store=self.store, audit_log=self.audit,
            model_policy=self.model_policy,
            approval_token_id=approval_token_id)
        record = {
            "task_id": outcome["task"]["id"],
            "requirement": requirement,
            "status": "completed" if outcome["accepted"] else "failed",
            "submitted_at": time.time(),
            "outcome": outcome,
        }
        self._tasks[record["task_id"]] = record
        return {"task_id": record["task_id"], "status": record["status"]}

    def task_status(self, task_id: str) -> dict[str, Any] | None:
        """Return the recorded status and outcome for a task, if known."""
        record = self._tasks.get(task_id)
        if record is None:
            return None
        return {
            "task_id": record["task_id"],
            "requirement": record["requirement"],
            "status": record["status"],
            "stages": record["outcome"].get("stages", []),
            "accepted": record["outcome"].get("accepted", False),
            "error": record["outcome"].get("error", ""),
            "files": record["outcome"].get("files", []),
        }

    def list_tasks(self) -> list[dict[str, str]]:
        return [{"task_id": record["task_id"], "status": record["status"],
                 "requirement": record["requirement"]}
                for record in self._tasks.values()]

    # -- approvals ------------------------------------------------------------------

    def request_approval(self, **fields: Any) -> str:
        """File an approval request; returns its id."""
        return self.store.submit(ApprovalRequest(**fields)).id

    def permission_requests(self, status: str = "pending") -> list[dict[str, Any]]:
        """List approval requests (pending by default, or all)."""
        if status == "all":
            return [request.to_dict()
                    for request in self.store.all_requests()]
        return [request.to_dict() for request in self.store.pending()]

    def decide_approval(self, request_id: str, approved: bool,
                        decided_by: str) -> dict[str, Any]:
        """Record an approval decision; returns the updated request."""
        return self.store.decide(request_id, approved, decided_by).to_dict()

    def mint_token(self, request_id: str, decided_by: str, *,
                   ttl_seconds: float = 600.0, max_uses: int = 1,
                   fingerprint: str = "") -> dict[str, Any]:
        """Mint an approval token from an approved request."""
        return self.store.issue(
            request_id, decided_by, ttl_seconds=ttl_seconds,
            max_uses=max_uses, fingerprint=fingerprint).to_dict()

    # -- events / logs ------------------------------------------------------------------

    def event_stream(self, task_id: str | None = None,
                     ) -> Iterator[dict[str, Any]]:
        """Yield audit events (optionally for one task) as plain dicts."""
        events = (self.audit.query(task_id=task_id) if task_id is not None
                  else list(self.audit.events))
        for event in events:
            yield event.to_dict()

    def task_events(self, task_id: str) -> list[dict[str, Any]]:
        """Return the run report events for a task (empty when unknown)."""
        record = self._tasks.get(task_id)
        if record is None:
            return []
        return list(record["outcome"].get("report", {}).get("events", []))

    def task_logs(self, task_id: str) -> dict[str, Any]:
        """Return the stage/error summary for a task."""
        record = self._tasks.get(task_id)
        if record is None:
            return {}
        outcome = record["outcome"]
        return {
            "task_id": task_id,
            "stages": outcome.get("stages", []),
            "error": outcome.get("error", ""),
            "rollback": outcome.get("rollback", False),
            "retry_count": outcome.get("retry_count", 0),
            "acceptance": outcome.get("acceptance", {}),
        }

    # -- model / agent status ------------------------------------------------------------------

    def model_status(self) -> dict[str, Any]:
        """Summarize model usage across recorded runs."""
        models: dict[str, int] = {}
        for record in self._tasks.values():
            model = record["outcome"].get("model") or ""
            if model:
                models[model] = models.get(model, 0) + 1
        return {"runs": len(self._tasks), "models": models}

    def agent_status(self) -> dict[str, Any]:
        """Summarize agent selection across recorded runs."""
        agents: dict[str, int] = {}
        for record in self._tasks.values():
            for name in record["outcome"].get("selected_agents", []):
                agents[name] = agents.get(name, 0) + 1
        return {"runs": len(self._tasks), "agents": agents}
