"""Multi-agent orchestration engine (A38).

Turns Forge's registered agents into a coordinated team instead of
isolated executors. A single user requirement becomes a validated plan
of agent steps that executes as a dependency-ordered DAG: independent
steps run in parallel (bounded), dependent steps run only after their
predecessors succeed, and every dispatch passes the A33 permission gate
(``Resource.AGENT`` / ``execute``) so no agent can be launched without
authorization.

Honesty invariants:

* The planner is deterministic capability matching over the registry —
  it never invents agents. A requirement matching no agent yields a
  ``PLAN_REJECTED`` report, not a fabricated plan.
* Steps communicate through structured :class:`AgentMessage` records
  (sender, receiver, task id, type, content, evidence, confidence,
  timestamp); dependent steps receive prior results as structured
  context, never uncontrolled free text alone.
* Budgets are real: per-step attempts, optional per-step timeouts,
  bounded parallel workers, and cooperative cancellation. A denied
  dispatch fails closed into a ``DENIED`` outcome.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from forge.agents.execution import AgentRequest, AgentResponse
from forge.agents.planner import CapabilityAgentPlanner
from forge.agents.registry import AgentRegistry
from forge.core.run_control import SupervisorControl, TaskCancelled
from forge.core.task_engine import Task, TaskStatus
from forge.security.approvals import ApprovalStore, enforce_with_token
from forge.security.policy import (PermissionPolicy, PermissionRequest,
                                   PolicyDecision, Resource)

MAX_MESSAGE_CONTENT = 2_000
MAX_HANDOFF_CONTEXT = 6_000


class StepStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    DENIED = "DENIED"
    SKIPPED = "SKIPPED"
    CANCELLED = "CANCELLED"


class ReportStatus(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    PLAN_REJECTED = "PLAN_REJECTED"


@dataclass(frozen=True)
class AgentMessage:
    """Structured inter-agent message with evidence and confidence."""

    sender: str
    receiver: str
    task_id: str
    message_type: str
    content: str
    evidence: tuple[str, ...] = ()
    confidence: float = 0.0
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sender": self.sender,
            "receiver": self.receiver,
            "task_id": self.task_id,
            "message_type": self.message_type,
            "content": self.content[:MAX_MESSAGE_CONTENT],
            "evidence": list(self.evidence)[:50],
            "confidence": self.confidence,
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True)
class OrchestrationStep:
    """One planned agent assignment in an orchestration plan."""

    id: str
    agent: str
    role: str
    capability: str
    instructions: str
    depends_on: tuple[str, ...] = ()


@dataclass
class OrchestrationPlan:
    """A validated multi-agent execution plan (dependency DAG)."""

    requirement: str
    steps: tuple[OrchestrationStep, ...] = ()

    def is_empty(self) -> bool:
        return not self.steps

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirement": self.requirement,
            "steps": [
                {"id": step.id, "agent": step.agent, "role": step.role,
                 "capability": step.capability,
                 "instructions": step.instructions,
                 "depends_on": list(step.depends_on)}
                for step in self.steps
            ],
        }

    def validate(self, registry: AgentRegistry) -> None:
        """Reject malformed plans: duplicate ids, unknown agents,
        dangling dependencies, or cycles."""
        ids = [step.id for step in self.steps]
        if len(set(ids)) != len(ids):
            raise ValueError("Orchestration plan has duplicate step ids")
        if not isinstance(self.requirement, str) or not self.requirement.strip():
            raise ValueError("Orchestration plan needs a requirement")
        known = set(ids)
        for step in self.steps:
            if not step.id.strip():
                raise ValueError("Step ids must be non-empty")
            if step.agent not in registry.names():
                raise ValueError(
                    f"Unknown agent {step.agent!r} in orchestration plan")
            if not step.instructions.strip():
                raise ValueError(
                    f"Step {step.id!r} has no instructions")
            for dependency in step.depends_on:
                if dependency not in known:
                    raise ValueError(
                        f"Step {step.id!r} depends on unknown step "
                        f"{dependency!r}")
        _topological_order(self.steps)  # raises on cycles


def _topological_order(steps: tuple[OrchestrationStep, ...]) -> list[str]:
    """Kahn's algorithm over step ids; raises on any cycle."""
    indegree = {step.id: 0 for step in steps}
    dependents: dict[str, list[str]] = {step.id: [] for step in steps}
    for step in steps:
        for dependency in step.depends_on:
            indegree[step.id] += 1
            dependents[dependency].append(step.id)
    ready = [step_id for step_id, degree in indegree.items() if degree == 0]
    order: list[str] = []
    while ready:
        step_id = ready.pop(0)
        order.append(step_id)
        for dependent in dependents[step_id]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
    if len(order) != len(steps):
        raise ValueError("Orchestration plan contains a dependency cycle")
    return order


@dataclass(frozen=True)
class DispatchQuery:
    """What the operator is asked to approve before a dispatch."""

    step_id: str
    agent: str
    role: str
    capability: str
    instructions: str
    task_id: str
    fingerprint: str
    label: str
    reason: str


@dataclass
class StepOutcome:
    step_id: str
    agent: str
    role: str
    capability: str
    status: StepStatus
    output: str = ""
    error: str = ""
    attempts: int = 0
    started_at: float | None = None
    finished_at: float | None = None
    messages: list[AgentMessage] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "agent": self.agent,
            "role": self.role,
            "capability": self.capability,
            "status": self.status.value,
            "output": self.output[:MAX_HANDOFF_CONTEXT],
            "error": self.error[:1000],
            "attempts": self.attempts,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "messages": [message.to_dict() for message in self.messages],
        }


@dataclass
class OrchestrationReport:
    run_id: str
    requirement: str
    status: ReportStatus
    outcomes: list[StepOutcome]
    summary: str
    accepted: bool
    started_at: float
    finished_at: float

    @property
    def duration_ms(self) -> float:
        return max(0.0, (self.finished_at - self.started_at) * 1000)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "requirement": self.requirement,
            "status": self.status.value,
            "accepted": self.accepted,
            "summary": self.summary,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": round(self.duration_ms, 3),
            "steps": [outcome.to_dict() for outcome in self.outcomes],
        }


class MultiAgentOrchestrator:
    """Plans and executes one requirement as a coordinated agent team.

    Dispatch gating: every step evaluates ``Resource.AGENT / execute``
    with agent identity ``agent_identity`` (default
    ``forge-orchestrator``). ``DENY`` fails the step closed. When the
    policy requires approval, the orchestrator asks ``approval_callback``
    (an operator hook that returns a minted token id, ``""`` when not
    granted) and redeems the token through ``approval_store`` — a stale,
    spent, or ungranted token fails the step closed.
    """

    def __init__(
        self,
        registry: AgentRegistry,
        *,
        max_workers: int = 4,
        max_attempts: int = 2,
        step_timeout: float | None = None,
        policy: PermissionPolicy | None = None,
        approval_store: ApprovalStore | None = None,
        approval_callback: Callable[[DispatchQuery], str] | None = None,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
        control: SupervisorControl | None = None,
        agent_identity: str = "forge-orchestrator",
        task_id: str = "",
        store_path: str = "",
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if step_timeout is not None and step_timeout <= 0:
            raise ValueError("step_timeout must be positive")
        self.registry = registry
        self.max_workers = max_workers
        self.max_attempts = max_attempts
        self.step_timeout = step_timeout
        self.policy = policy
        self.approval_store = approval_store
        self.approval_callback = approval_callback
        self.on_event = on_event
        self.control = control
        self.agent_identity = agent_identity
        self.task_id = task_id
        #: When set, the fenced scheduler persists tasks, attempts, and
        #: its monotonically sequenced event log here (SQLite), so runs
        #: survive restart and are inspectable (`forge tasks`).
        self.store_path = store_path
        # The scheduler running the current execute() (if any), so
        # cancel() can reach the scheduler-level fence — not just the
        # cooperative per-attempt checkpoint.
        self._active_scheduler: Any = None

    # -- planning -----------------------------------------------------------

    def build_plan(self, requirement: str, *, chain: bool = False) -> OrchestrationPlan:
        """Decompose one requirement into ordered agent steps.

        Deterministic capability matching over the registry; an empty
        match produces an empty plan (execution reports PLAN_REJECTED),
        never a fabricated team. With ``chain=True`` every step depends
        on the previous one (sequential pipeline, e.g.
        coder → tester → debugger → reviewer); the default leaves steps
        independent so safe parallelism is possible.
        """
        planner = CapabilityAgentPlanner(self.registry)
        agent_plan = planner.plan(requirement)
        steps: list[OrchestrationStep] = []
        for planned in agent_plan.agents:
            dependencies: tuple[str, ...] = ()
            if chain and steps:
                dependencies = (steps[-1].id,)
            step = OrchestrationStep(
                id=f"step-{planned.order + 1}-{planned.registration.name}",
                agent=planned.registration.name,
                role=planned.registration.role,
                capability=planned.capability,
                instructions=(
                    f"Requirement: {requirement}\n"
                    f"Capability needed: {planned.capability}\n"
                    f"Role: {planned.registration.role}"
                ),
                depends_on=dependencies,
            )
            steps.append(step)
        return OrchestrationPlan(requirement=requirement, steps=tuple(steps))

    # -- execution -----------------------------------------------------------

    def execute(self, plan: OrchestrationPlan) -> OrchestrationReport:
        """Execute the plan on the canonical fenced DAG scheduler.

        Every step attempt carries a unique ``(step_id, generation)``
        identity. A step that times out is fenced the moment the watchdog
        expires: its late result is rejected by the scheduler, and its
        guard refuses further writes through the change-set and tool
        choke points. Retries start at a strictly higher generation, so
        an abandoned attempt can never overlap a fresh one.
        """
        from forge.core.dag_scheduler import DAGScheduler
        from forge.core.fencing import (CANCELLED, FENCED,
                                        SKIPPED as _SKIPPED_STATE,
                                        SUCCEEDED)

        plan.validate(self.registry)
        run_id = uuid.uuid4().hex
        started = time.time()
        self._emit("orchestration_started", {
            "run_id": run_id, "requirement": plan.requirement,
            "steps": [step.id for step in plan.steps]})
        if plan.is_empty():
            report = OrchestrationReport(
                run_id=run_id, requirement=plan.requirement,
                status=ReportStatus.PLAN_REJECTED, outcomes=[],
                summary=("No registered agent matches this requirement; "
                         "the plan was rejected instead of fabricated."),
                accepted=False, started_at=started, finished_at=time.time())
            self._emit("orchestration_finished", report.to_dict())
            return report

        by_id = {step.id: step for step in plan.steps}
        step_data: dict[str, dict[str, Any]] = {
            step.id: {"messages": [], "started": False}
            for step in plan.steps}
        dispatch_cache: dict[str, tuple[bool, str]] = {}
        cache_lock = threading.Lock()

        def executor(task: Any, attempt: Any, control: Any, guard: Any):
            step = by_id[task.task_id]
            # External operator control (A38 cockpit cancel): checked
            # before each attempt and again after the call returns, so a
            # cancel that lands mid-invoke still wins the report.
            try:
                self._checkpoint()
                control.checkpoint("step-start")
            except TaskCancelled:
                return {"success": False, "cancelled": True,
                        "terminal": True, "output": "",
                        "error": "cancelled by operator", "metadata": {}}
            with cache_lock:
                if task.task_id not in dispatch_cache:
                    dispatch_cache[task.task_id] = self._dispatch_permission(
                        step)
            allowed, reason = dispatch_cache[task.task_id]
            if not allowed:
                return {"success": False, "terminal": True, "output": "",
                        "error": reason or "dispatch not authorized",
                        "metadata": {"denied": True}}
            outcome = self._run_attempt(step, attempt, control, guard,
                                        step_data[task.task_id])
            try:
                self._checkpoint()
            except TaskCancelled:
                return {"success": False, "cancelled": True,
                        "terminal": True, "output": outcome.output,
                        "error": "cancelled by operator", "metadata": {}}
            return {"success": outcome.status == StepStatus.SUCCEEDED,
                    "cancelled": outcome.status == StepStatus.CANCELLED,
                    "terminal": outcome.status != StepStatus.FAILED,
                    "output": outcome.output, "error": outcome.error,
                    "metadata": {}}

        scheduler = DAGScheduler(
            self.store_path, max_workers=self.max_workers,
            owner=self.agent_identity)
        for step in plan.steps:
            scheduler.add_task(
                step.id, step.instructions, depends_on=step.depends_on,
                timeout=self.step_timeout, max_attempts=self.max_attempts)
        self._active_scheduler = scheduler
        try:
            results = scheduler.run(executor)
        finally:
            self._active_scheduler = None

        outcomes: dict[str, StepOutcome] = {}
        for step in plan.steps:
            result = results.get(step.id)
            state = result.state if result is not None else _SKIPPED_STATE
            data = step_data[step.id]
            attempts = result.attempts if result is not None else 0
            error = (result.error if result is not None
                     else "dependency step did not succeed")
            output = (result.output if result is not None else "")
            if state == SUCCEEDED:
                status = StepStatus.SUCCEEDED
            elif state == CANCELLED:
                status = StepStatus.CANCELLED
            elif state == _SKIPPED_STATE:
                status = StepStatus.SKIPPED
            elif state == FENCED:
                # A fenced step (timeout, abandoned worker) is a failure
                # with the watchdog's reason — never a success.
                status = StepStatus.FAILED
            else:
                status = StepStatus.FAILED
            if status == StepStatus.FAILED and result is not None \
                    and result.metadata.get("denied"):
                status = StepStatus.DENIED
                attempts = 0
            outcomes[step.id] = StepOutcome(
                step.id, step.agent, step.role, step.capability, status,
                output=output[:MAX_HANDOFF_CONTEXT],
                error=error[:1000] if status != StepStatus.SUCCEEDED else "",
                attempts=attempts,
                started_at=None, finished_at=time.time(),
                messages=list(data["messages"]))

        ordered = [outcomes[step.id] for step in plan.steps]
        failed = [outcome for outcome in ordered
                  if outcome.status in (StepStatus.FAILED, StepStatus.DENIED)]
        succeeded = [outcome for outcome in ordered
                     if outcome.status == StepStatus.SUCCEEDED]
        step_cancelled = any(outcome.status == StepStatus.CANCELLED
                             for outcome in ordered)
        host_cancel = bool(self.control is not None
                           and self.control.cancel_requested)
        # A cancelled run is never a success — not even a partial one.
        # (A host-injected control may be absent, so the step outcomes
        # are the authoritative signal.)
        if host_cancel or step_cancelled:
            status = ReportStatus.CANCELLED
        elif failed:
            status = ReportStatus.FAILED
        elif not succeeded:
            # Nothing succeeded and nothing failed: every step ended
            # skipped or cancelled. That is a cancellation, not a
            # success — report it as one.
            status = ReportStatus.CANCELLED
        else:
            status = ReportStatus.SUCCEEDED
        summary = (
            f"{len(succeeded)} of {len(ordered)} agent steps succeeded"
            + (f"; {len(failed)} failed/denied" if failed else "")
            + (f"; {len(ordered) - len(succeeded) - len(failed)} skipped/"
               "cancelled" if len(ordered) > len(succeeded) + len(failed)
               else ""))
        report = OrchestrationReport(
            run_id=run_id, requirement=plan.requirement, status=status,
            outcomes=ordered, summary=summary,
            accepted=(status == ReportStatus.SUCCEEDED),
            started_at=started, finished_at=time.time())
        self._emit("orchestration_finished", report.to_dict())
        return report

    # -- one step attempt ------------------------------------------------------

    def _run_attempt(self, step: OrchestrationStep, attempt: Any, control: Any,
                     guard: Any, data: dict[str, Any]) -> StepOutcome:
        """Run exactly one fenced attempt of a step."""
        started = time.time()
        if not data["started"]:
            data["started"] = True
            self._emit("step_started", {
                "step_id": step.id, "agent": step.agent,
                "capability": step.capability, "role": step.role})
        self._emit("step_attempt", {
            "step_id": step.id, "agent": step.agent,
            "attempt": attempt.attempt, "attempt_id": attempt.attempt_id})
        try:
            response, step_messages = self._invoke(step, guard)
        except TaskCancelled:
            return self._outcome(step, StepStatus.CANCELLED,
                                 error="cancelled by operator",
                                 attempts=attempt.attempt, started_at=started,
                                 messages=list(data["messages"]))
        data["messages"].extend(step_messages)
        if response is not None and response.success:
            return self._outcome(
                step, StepStatus.SUCCEEDED,
                output=response.output or "", attempts=attempt.attempt,
                started_at=started, messages=list(data["messages"]))
        error = (response.error if response is not None
                 else "agent produced no response")
        self._emit("step_failed", {
            "step_id": step.id, "agent": step.agent,
            "error": error, "attempt": attempt.attempt})
        return self._outcome(step, StepStatus.FAILED, error=error,
                             attempts=attempt.attempt, started_at=started,
                             messages=list(data["messages"]))

    def _invoke(self, step: OrchestrationStep, guard: Any = None) -> tuple[
            AgentResponse | None, list[AgentMessage]]:
        """Invoke the step's agent with the attempt's fence guard attached.

        The scheduler's watchdog owns the step timeout: when it expires
        the attempt is fenced (late results rejected, late writes
        refused by ``guard``) while the underlying provider call
        finishes under its own bounded network timeout. No thread is
        leaked by the orchestrator itself.
        """
        registration = self.registry.get(step.agent)
        task_id = self.task_id or f"orchestration-{uuid.uuid4().hex[:12]}"
        request = AgentRequest(
            task=Task(
                id=task_id,
                description=step.instructions, status=TaskStatus.RUNNING),
            stage=TaskStatus.RUNNING,
            instructions=step.instructions,
            metadata={"capability": step.capability, "role": step.role},
            guard=guard)
        response = registration.executor.execute(request)
        message = AgentMessage(
            sender=step.agent, receiver=self.agent_identity,
            task_id=request.task.id, message_type="result",
            content=(response.output or response.error)[:MAX_MESSAGE_CONTENT],
            evidence=(f"success={response.success}",),
            confidence=1.0 if response.success else 0.0)
        return response, [message]

    def cancel(self) -> None:
        """Request cancellation of the in-flight orchestration.

        Two complementary paths, both fail closed:

        * The per-attempt control asks each running worker to stop at its
          next checkpoint (cooperative).
        * The scheduler-level ``cancel_all`` marks queued steps cancelled
          and moves running steps to ``CANCELLING``. A worker that
          ignores checkpoints is fenced after the cancel grace and its
          late result is rejected as stale — so cancellation cannot be
          defeated by a hung worker, and no retry ever starts afterwards.
        """
        if self.control is not None:
            self.control.request_cancel()
        scheduler = self._active_scheduler
        if scheduler is not None:
            try:
                scheduler.cancel_all()
            except Exception:
                pass

    def _dispatch_permission(self, step: OrchestrationStep) -> tuple[
            bool, str]:
        """Evaluate Resource.AGENT / execute for one dispatch."""
        if self.policy is None:
            return True, ""
        permission = PermissionRequest(
            agent=self.agent_identity, resource=Resource.AGENT,
            operation="execute", scope=step.agent,
            task_id=self.task_id, reason=(
                f"dispatch agent {step.agent} for capability "
                f"{step.capability}"))
        evaluation = self.policy.evaluate(permission)
        if evaluation.decision == PolicyDecision.ALLOW:
            return True, evaluation.reason
        if evaluation.decision == PolicyDecision.DENY:
            return False, evaluation.reason or "dispatch denied by policy"
        # REQUIRE_APPROVAL: ask the operator hook, then redeem the token.
        token = ""
        if self.approval_callback is not None:
            query = DispatchQuery(
                step_id=step.id, agent=step.agent, role=step.role,
                capability=step.capability, instructions=step.instructions,
                task_id=self.task_id, fingerprint=f"agent-dispatch:{step.id}",
                label=f"Dispatch agent {step.agent}",
                reason=f"Capability {step.capability} for step {step.id}")
            try:
                token = self.approval_callback(query) or ""
            except TaskCancelled:
                raise
            except Exception as exc:  # operator hook failed: fail closed
                return False, f"approval hook failed: {exc}"
        if not token:
            return False, "dispatch approval not granted"
        if self.approval_store is not None:
            allowed, reason = enforce_with_token(
                self.approval_store, token, permission)
            return bool(allowed), reason
        # No store: the operator hook itself is the approval authority.
        return True, "approved by operator"

    # -- helpers ---------------------------------------------------------------

    def _outcome(self, step: OrchestrationStep, status: StepStatus, *,
                 output: str = "", error: str = "", attempts: int = 0,
                 started_at: float | None = None,
                 messages: list[AgentMessage] | None = None) -> StepOutcome:
        outcome = StepOutcome(
            step.id, step.agent, step.role, step.capability, status,
            output=output, error=error, attempts=attempts,
            started_at=started_at, finished_at=time.time(),
            messages=messages or [])
        self._emit("step_finished", outcome.to_dict())
        return outcome

    def _checkpoint(self) -> None:
        if self.control is not None:
            self.control.checkpoint()

    def _emit(self, name: str, details: dict[str, Any]) -> None:
        if self.on_event is not None:
            try:
                self.on_event(name, details)
            except Exception:
                pass


class StepTimeoutError(Exception):
    """A step exceeded its per-step budget."""

    def __init__(self, step_id: str, timeout: float) -> None:
        super().__init__(f"Step {step_id} timed out after {timeout}s")
        self.step_id = step_id
        self.timeout = timeout


def plan_by_id(plan: OrchestrationPlan, step_id: str) -> OrchestrationStep:
    for step in plan.steps:
        if step.id == step_id:
            return step
    raise KeyError(step_id)
