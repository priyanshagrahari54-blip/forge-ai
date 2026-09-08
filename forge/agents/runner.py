"""Agent execution (A51): runtime-defined agents really run tasks.

Honesty rules:

* A defined agent only runs when its definition binds a real
  executor for its role (``real=True``). Unbound definitions are
  refused with an explanation — they are specifications, not
  capabilities.
* Execution goes through the existing ``Resource.AGENT / execute``
  policy gate (handled by the plane, which owns the gate); this
  module only runs what it is told to run.
* Results carry real output/errors from the executor and real
  elapsed time; the per-agent run log is bounded and never
  embellished.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable
from uuid import uuid4

from forge.agents.execution import AgentExecutor, AgentRequest
from forge.core.task_engine import Task, TaskStatus

MAX_REQUIREMENT = 4000
MAX_RUNS_PER_AGENT = 20

SUPPORTED_ROLES = ("coding", "planning", "research")


@dataclass
class AgentRunResult:
    run_id: str
    agent: str
    role: str
    success: bool
    output: str = ""
    error: str = ""
    elapsed_ms: float = 0.0
    files: tuple[str, ...] = ()
    at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "agent": self.agent,
            "role": self.role,
            "success": self.success,
            "output": self.output[:2000],
            "error": self.error[:500],
            "elapsed_ms": self.elapsed_ms,
            "files": list(self.files),
            "at": self.at,
        }


class AgentRunner:
    """Runs defined agents through real executors, bounded per agent."""

    def __init__(self, session_id: str,
                 executor_builder: Callable[[str], AgentExecutor | None]
                 ) -> None:
        self.session_id = session_id
        self.executor_builder = executor_builder
        self._runs: dict[str, list[AgentRunResult]] = {}

    def run(self, definition: Any,
            requirement: str, *, run_id: str = "") -> AgentRunResult:
        requirement = (requirement or "").strip()[:MAX_REQUIREMENT]
        if not requirement:
            raise ValueError("requirement must be non-empty")
        if not getattr(definition, "real", False):
            raise ValueError(
                f"Agent {definition.name!r} is a definition without a "
                "bound executor; it cannot run tasks.")
        if definition.role not in SUPPORTED_ROLES:
            raise ValueError(
                f"No executor is available for role "
                f"{definition.role!r}; supported roles: "
                f"{', '.join(SUPPORTED_ROLES)}.")
        executor = self.executor_builder(definition.role)
        if executor is None:
            raise ValueError(
                f"No executor could be built for role "
                f"{definition.role!r} in this session.")
        run_id = run_id or uuid4().hex[:12]
        task = Task(id=f"agent-run-{run_id}", description=requirement,
                    status=TaskStatus.CODING)
        request = AgentRequest(task=task, stage=TaskStatus.CODING)
        started = time.monotonic()
        response = executor.execute(request)
        elapsed_ms = round((time.monotonic() - started) * 1000.0, 1)
        files = tuple(str(path) for path in
                      response.metadata.get("files", ()))
        result = AgentRunResult(
            run_id=run_id, agent=definition.name, role=definition.role,
            success=bool(response.success),
            output=(response.output or "")[:2000],
            error=(response.error or "")[:500],
            elapsed_ms=elapsed_ms, files=files)
        log = self._runs.setdefault(definition.name, [])
        log.append(result)
        self._runs[definition.name] = log[-MAX_RUNS_PER_AGENT:]
        return result

    def history(self, agent_name: str) -> list[dict[str, Any]]:
        return [run.to_dict() for run in
                self._runs.get(agent_name, [])]
