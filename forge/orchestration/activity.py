"""Agent activity visualization model (A81).

:class:`AgentActivityTracker` is the single source of truth the desktop
views render: it folds the scheduler's event stream into a per-role
activity state (queued / waiting for locks / running / blocked /
succeeded / failed / denied / cancelled), with attempt counts, held
locks, the most recent structured message, and queue depth.

The tracker is passive — it only reacts to events the scheduler emits —
so the view can never drift from what actually happened, and rendering
is a pure function of the snapshot.
"""
from __future__ import annotations

import time
from typing import Any

#: Per-role activity states, in display order.
ACTIVITY_STATES = (
    "idle", "queued", "waiting_lock", "running", "blocked", "denied",
    "succeeded", "failed", "cancelled", "skipped",
)

_ROLE_EVENT_FIELDS = {
    "task_queued": ("role",),
    "task_started": ("role",),
    "task_finished": ("role",),
    "task_blocked": ("role",),
    "task_denied": ("role",),
    "task_retried": ("role",),
    "lock_wait": ("role",),
    "lock_acquired": ("role",),
    "lock_released": ("role",),
    "message_sent": ("receiver",),
    "task_unblocked": ("role",),
}


class AgentActivityTracker:
    """Folds scheduler events into a per-role activity snapshot."""

    def __init__(self, roles: set[str] | frozenset[str] | None = None) -> None:
        self.roles = set(roles) if roles is not None else None
        self._agents: dict[str, dict[str, Any]] = {}
        self._counters: dict[str, int] = {}
        self._recent_messages: list[dict[str, Any]] = []
        self._locks: dict[str, Any] = {}
        self.run_id = ""
        self.requirement = ""
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.status = "IDLE"

    # -- event intake -----------------------------------------------------------

    def on_event(self, name: str, details: dict[str, Any] | None = None) -> None:
        details = details or {}
        if name == "run_started":
            self.run_id = details.get("run_id", self.run_id)
            self.requirement = details.get("requirement", self.requirement)
            self.started_at = details.get("at", time.time())
            self.status = "RUNNING"
            return
        if name == "run_finished":
            self.finished_at = details.get("at", time.time())
            self.status = details.get("status", "FINISHED")
            return
        if name == "lock_snapshot":
            self._locks = details.get("keys", {})
            return
        if name == "message_sent":
            entry = {
                "sender": details.get("sender", ""),
                "receiver": details.get("receiver", ""),
                "type": details.get("type", ""),
                "task_id": details.get("task_id", ""),
                "content": str(details.get("content", ""))[:200],
            }
            self._recent_messages.append(entry)
            del self._recent_messages[:-10]
            self._agent_for(details.get("receiver", "")).setdefault(
                "last_message_at", entry)
            return
        role = details.get("role", "")
        agent = self._agent_for(role)
        task_id = details.get("task_id", "")
        if task_id:
            agent["task_id"] = task_id
        if name == "task_queued":
            agent["state"] = "queued"
            agent["queued"] = agent.get("queued", 0) + 1
            self._bump("queued")
        elif name == "lock_wait":
            agent["state"] = "waiting_lock"
            self._bump("waiting_lock")
        elif name == "lock_acquired":
            agent["held_locks"] = sorted(details.get("keys", []))
        elif name == "lock_released":
            agent["held_locks"] = []
        elif name == "task_started":
            agent["state"] = "running"
            agent["started_at"] = details.get("at", time.time())
            agent["attempts"] = details.get("attempt",
                                            agent.get("attempts", 0))
            self._bump("running")
        elif name == "task_retried":
            agent["attempts"] = details.get("attempt",
                                            agent.get("attempts", 0))
        elif name == "task_blocked":
            agent["state"] = "blocked"
            agent["blocked_reason"] = details.get("reason", "")
            self._bump("blocked")
        elif name == "task_unblocked":
            agent["state"] = "queued"
        elif name == "task_denied":
            agent["state"] = "denied"
            agent["error"] = details.get("reason", "")
            self._bump("denied")
        elif name == "task_finished":
            status = details.get("status", "")
            state = {
                "SUCCEEDED": "succeeded", "FAILED": "failed",
                "CANCELLED": "cancelled", "SKIPPED": "skipped",
                "DENIED": "denied", "BLOCKED": "blocked",
            }.get(status, "failed")
            agent["state"] = state
            agent["finished_at"] = details.get("at", time.time())
            if details.get("error"):
                agent["error"] = str(details["error"])[:300]
            if state in ("succeeded", "failed", "denied", "cancelled",
                         "skipped"):
                agent["held_locks"] = []
                self._bump(state)
        self._agent_last_event(agent, name, details)

    def _agent_for(self, role: str) -> dict[str, Any]:
        if not role:
            return {}
        if role not in self._agents:
            self._agents[role] = {
                "role": role, "state": "idle", "queued": 0,
                "attempts": 0, "held_locks": [], "task_id": "",
                "last_event": "", "last_event_at": 0.0,
                "started_at": None, "finished_at": None, "error": "",
                "blocked_reason": "",
            }
        return self._agents[role]

    def _agent_last_event(self, agent: dict[str, Any], name: str,
                          details: dict[str, Any]) -> None:
        if not agent:
            return
        agent["last_event"] = name
        agent["last_event_at"] = details.get("at", time.time())

    def _bump(self, key: str) -> None:
        self._counters[key] = self._counters.get(key, 0) + 1

    def set_role_order(self, roles: list[str]) -> None:
        self.roles = set(roles)

    # -- snapshot -----------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        agents = []
        for role in (sorted(self._agents) if self.roles is None
                     else [r for r in sorted(self.roles)
                           if r in self._agents]
                     + [r for r in sorted(self._agents)
                        if r not in (self.roles or set())]):
            agents.append(dict(self._agents[role]))
        return {
            "run_id": self.run_id,
            "requirement": self.requirement,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "agents": agents,
            "counts": dict(self._counters),
            "locks": dict(self._locks),
            "recent_messages": list(self._recent_messages),
        }
