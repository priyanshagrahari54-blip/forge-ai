"""Created-agent runtime (A81): every run flows through the six subsystems.

A created agent never executes on its own authority. When the engine
dispatches a task, the run passes through, in order:

1. **Model Fabric** — the request routes through ``ModelFabric`` under
   the spec's model requirements; the response is untrusted text.
2. **PolicyGate** — every write, tool call, and permission question is
   evaluated against the A32/A33 gate under the agent's identity
   (``agent:<name>``); DENY is never escalated.
3. **Tool Runtime** — tools execute only through the permissioned
   ``ToolRuntime`` and only when they are in the spec's tool allowlist
   *and* covered by the spec's permission ceiling.
4. **Memory** — the agent remembers through a namespaced ``MemoryStore``
   confined to its own directory (or the explicit shared pool).
5. **Verification** — written work passes the spec's verification gates
   (security / review / tests as configured).
6. **Checkpoints** — file changes checkpoint before the first write and
   roll back exactly the agent's files when verification fails.

Isolation boundaries (all enforced here, all tested):

* tool boundary — spec allowlist ∩ registered tools, nothing else;
* permission ceiling — operations outside ``spec.permissions`` are
  refused *before* the gate; approval can never expand the ceiling;
* memory boundary — per-agent namespace, traversal-checked keys;
* lifecycle boundary — only ``enabled`` agents dispatch at all;
* resource boundary — per-agent run/concurrency/tool budgets.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from forge.agents.engine.factory import EngineGuard
from forge.agents.engine.lifecycle import LifecycleState
from forge.agents.engine.spec import TOOL_PERMISSION_MAP
from forge.memory.store import MemoryStore
from forge.models.request import ModelRequest
from forge.security.policy import Resource
from forge.security.policy_gate import PolicyDecision, PolicyGate
from forge.tools.change_applier import ChangeApplier
from forge.tools.checkpoint import CheckpointManager

MAX_TASK = 4000
MAX_MEMORY_VALUE = 2000
SHARED_MEMORY_DIR = "_shared"


@dataclass
class EngineBundle:
    """The six subsystems created agents operate through."""

    fabric: Any
    runtime: Any
    permission_manager: Any
    root: str | Path = "."
    policy_gate: PolicyGate | None = None
    checkpoint_manager: CheckpointManager | None = None
    verification: Any = None
    memory_base: str | Path = ".forge/agents"
    approval_store: Any = None
    approval_callback: Callable[..., bool] | None = None

    @classmethod
    def build(cls, root: str | Path = ".", *, fabric: Any | None = None,
              mode: Any | None = None, policy: Any | None = None,
              approval_store: Any | None = None,
              approval_callback: Callable[..., bool] | None = None,
              ) -> "EngineBundle":
        """Assemble the standard bundle over one project root."""
        from forge.runtime.defaults import create_default_runtime
        from forge.security.permissions import OperationMode, PermissionManager
        from forge.security.verification import VerificationPipeline

        root = Path(root).resolve()
        manager = PermissionManager(
            mode=OperationMode(mode) if mode is not None
            else OperationMode.ASSISTED,
            policy=policy)
        runtime = create_default_runtime(manager, str(root))
        return cls(
            fabric=fabric,
            runtime=runtime,
            permission_manager=manager,
            root=root,
            policy_gate=PolicyGate(manager),
            checkpoint_manager=CheckpointManager(str(root)),
            verification=VerificationPipeline(root),
            memory_base=root / ".forge" / "agents",
            approval_store=approval_store,
            approval_callback=approval_callback,
        )


@dataclass
class EngineRunReport:
    """Honest, structured record of one created-agent run."""

    run_id: str
    agent: str
    version: int
    task: str
    success: bool = False
    error: str = ""
    model: dict = field(default_factory=dict)
    changes: list = field(default_factory=list)
    refusals: list = field(default_factory=list)
    checkpoint_id: str = ""
    verification: list = field(default_factory=list)
    memory: dict = field(default_factory=dict)
    rolled_back: bool = False
    elapsed_ms: float = 0.0
    at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "agent": self.agent,
            "version": self.version,
            "task": self.task[:200],
            "success": self.success,
            "error": self.error[:500],
            "model": dict(self.model),
            "changes": list(self.changes),
            "refusals": [dict(item) for item in self.refusals],
            "checkpoint_id": self.checkpoint_id,
            "verification": [dict(item) for item in self.verification],
            "memory": dict(self.memory),
            "rolled_back": self.rolled_back,
            "elapsed_ms": self.elapsed_ms,
            "at": self.at,
        }


def _boundary_refusal(agent: str, kind: str, detail: str) -> dict[str, Any]:
    return {"agent": agent, "boundary": kind, "detail": detail,
            "at": time.time()}


class EngineRuntime:
    """Dispatches created agents through the six-subsystem pipeline."""

    def __init__(self, bundle: EngineBundle, *,
                 governor: Any | None = None) -> None:
        self.bundle = bundle
        from forge.agents.governance import AgentGovernor

        self.governor = governor if governor is not None else AgentGovernor()
        self.boundary_events: list[dict[str, Any]] = []
        self._tool_budgets: dict[tuple[str, str], int] = {}

    # -- boundaries -------------------------------------------------------

    def _record(self, refusal: dict[str, Any]) -> None:
        self.boundary_events.append(refusal)
        self.boundary_events = self.boundary_events[-500:]

    def _memory_store(self, package: Any) -> MemoryStore:
        policy = package.spec.memory_policy
        base = Path(self.bundle.memory_base)
        if policy.share_across_agents:
            root = base / SHARED_MEMORY_DIR / "memory"
        else:
            root = base / package.name / "memory"
        return MemoryStore(root)

    def check_permission(self, package: Any, resource: Resource | str,
                         operation: str, *, path: str = "",
                         risk: str = "NONE", approved: bool = False,
                         preview: bool = False) -> dict[str, Any]:
        """Two-layer permission answer: ceiling, then the PolicyGate.

        Outside the ceiling the answer is a hard refusal — approval can
        never expand what a spec never granted. Inside the ceiling the
        A32/A33 PolicyGate decides (ALLOW / DENY / REQUIRE_APPROVAL).
        """
        actor = EngineGuard.runtime_actor(package.name)
        resource_value = resource.value if isinstance(resource, Resource) \
            else str(resource)
        if not package.spec.allows(resource, operation):
            refusal = _boundary_refusal(
                package.name, "permission-ceiling",
                f"{resource_value}:{operation} is outside this agent's "
                "permission ceiling; approval cannot expand it")
            if not preview:
                self._record(refusal)
            return {"allowed": False, "decision": "DENY",
                    "boundary": "permission-ceiling",
                    "reason": refusal["detail"], "actor": actor}
        gate = self.bundle.policy_gate or PolicyGate(
            self.bundle.permission_manager)
        outcome = gate.evaluate(
            operation=self._a32_operation(resource, operation),
            path=path, risk=risk,
            capability=package.spec.primary_capability,
            approved=approved, agent=actor, preview=preview)
        return {
            "allowed": outcome.decision is PolicyDecision.ALLOW,
            "decision": outcome.decision.value,
            "boundary": "policy-gate",
            "reason": outcome.reason,
            "actor": actor,
            "path": path,
        }

    @staticmethod
    def _a32_operation(resource: Resource | str, operation: str) -> str:
        """Map an (resource, operation) pair onto an A32 tool operation."""
        mapping = {
            (Resource.FILESYSTEM, "read"): "read_file",
            (Resource.FILESYSTEM, "write"): "write_file",
            (Resource.FILESYSTEM, "delete"): "delete_file",
            (Resource.TERMINAL, "execute"): "run_command",
            (Resource.GIT, "status"): "git_status",
            (Resource.GIT, "diff"): "git_status",
            (Resource.GIT, "commit"): "git_commit",
        }
        try:
            key = (Resource(resource), (operation or "").lower())
        except ValueError:
            return str(operation)
        return mapping.get(key, str(operation))

    def _budget_key(self, package: Any, run_id: str) -> tuple[str, str]:
        return (package.name, run_id or "adhoc")

    def _spend_tool_budget(self, package: Any, run_id: str) -> tuple[bool, str]:
        key = self._budget_key(package, run_id)
        used = self._tool_budgets.get(key, 0)
        limit = package.spec.resource_limits.max_tool_calls_per_run
        if used >= limit:
            refusal = _boundary_refusal(
                package.name, "tool-budget",
                f"Run exceeded its tool-call budget ({limit})")
            self._record(refusal)
            return False, refusal["detail"]
        self._tool_budgets[key] = used + 1
        return True, ""

    # -- tools ----------------------------------------------------------------

    def call_tool(self, package: Any, tool: str, *, run_id: str = "",
                  approved: bool = False, **kwargs: Any) -> Any:
        """Execute one tool under every boundary, then the runtime gate."""
        from forge.runtime.runtime import ToolResult

        actor = EngineGuard.runtime_actor(package.name)
        if tool not in package.spec.tools:
            refusal = _boundary_refusal(
                package.name, "tool-allowlist",
                f"Tool {tool!r} is not in this agent's tool allowlist")
            self._record(refusal)
            return ToolResult.fail(tool, refusal["detail"])
        if tool not in self.bundle.runtime.tools:
            refusal = _boundary_refusal(
                package.name, "tool-allowlist",
                f"Tool {tool!r} is not registered in the tool runtime")
            self._record(refusal)
            return ToolResult.fail(tool, refusal["detail"])
        resource, operation = TOOL_PERMISSION_MAP[tool]
        if not package.spec.allows(resource, operation):
            refusal = _boundary_refusal(
                package.name, "permission-ceiling",
                f"Tool {tool!r} needs {resource.value}:{operation}, which "
                "is outside this agent's permission ceiling")
            self._record(refusal)
            return ToolResult.fail(tool, refusal["detail"])
        ok, reason = self._spend_tool_budget(package, run_id)
        if not ok:
            return ToolResult.fail(tool, reason)
        return self.bundle.runtime.execute(
            tool, approved=approved, actor=actor,
            task_id=f"agent-run-{run_id}" if run_id else "", **kwargs)

    # -- memory ----------------------------------------------------------------

    def remember(self, package: Any, key: str, value: str) -> dict[str, Any]:
        policy = package.spec.memory_policy
        if not policy.enabled:
            refusal = _boundary_refusal(
                package.name, "memory-policy",
                "This agent's memory policy is disabled")
            self._record(refusal)
            return {"stored": False, "reason": refusal["detail"]}
        store = self._memory_store(package)
        entries = store.list()
        if key not in entries and len(entries) >= policy.max_entries:
            refusal = _boundary_refusal(
                package.name, "memory-policy",
                f"Memory is full ({policy.max_entries} entries)")
            self._record(refusal)
            return {"stored": False, "reason": refusal["detail"]}
        store.save(key, str(value)[:MAX_MEMORY_VALUE])
        return {"stored": True, "key": key,
                "namespace": str(store.root)}

    def recall(self, package: Any, key: str) -> dict[str, Any]:
        policy = package.spec.memory_policy
        if not policy.enabled:
            return {"value": None, "reason": "memory policy disabled"}
        store = self._memory_store(package)
        return {"value": store.load(key),
                "namespace": str(store.root)}

    def memory_keys(self, package: Any) -> list[str]:
        policy = package.spec.memory_policy
        if not policy.enabled:
            return []
        return self._memory_store(package).list()

    # -- approvals (no self-grant, ever) -----------------------------------------

    def request_approval(self, package: Any, resource: Resource | str,
                         operation: str, *, reason: str = "",
                         consequences: str = "") -> dict[str, Any]:
        """Submit an approval request for an in-ceiling operation.

        The request is filed by the agent; only a *distinct operator*
        can ever approve it (see :meth:`decide_approval`).
        """
        if self.bundle.approval_store is None:
            return {"submitted": False,
                    "reason": "No approval store is attached"}
        from forge.security.approvals import ApprovalRequest

        request = ApprovalRequest(
            agent=EngineGuard.runtime_actor(package.name),
            resource=resource, operation=operation,
            task_id="", reason=reason or "Agent runtime operation",
            consequences=consequences or
            "The agent wants to perform this operation.")
        stored = self.bundle.approval_store.submit(request)
        return {"submitted": True, "approval": stored.to_dict()}

    def decide_approval(self, package: Any, approval_id: str, *,
                        approved: bool, approver: str) -> dict[str, Any]:
        """An operator decides a pending approval. Agents never can.

        Refuses when the approver is an agent actor or the deciding
        agent itself — an agent may not approve its own request (and no
        agent may approve any request at all). This boundary holds even
        when no approval store is attached.
        """
        if EngineGuard.is_agent_actor(approver) or \
                approver == package.name:
            refusal = _boundary_refusal(
                package.name, "self-approval",
                f"Approver {approver!r} is an agent; agents cannot "
                "approve permission requests")
            self._record(refusal)
            return {"decided": False, "reason": refusal["detail"]}
        if self.bundle.approval_store is None:
            return {"decided": False, "reason": "No approval store"}
        store = self.bundle.approval_store
        decided = store.decide(approval_id, approved=approved)
        return {"decided": True, "approval": decided.to_dict()}

    # -- the run pipeline ----------------------------------------------------------

    def run(self, package: Any, task: str, *, approved: bool = False,
            run_id: str = "", context: str = "") -> EngineRunReport:
        task = (task or "").strip()[:MAX_TASK]
        if not task:
            raise ValueError("task must be non-empty")
        run_id = run_id or uuid4().hex[:12]
        report = EngineRunReport(
            run_id=run_id, agent=package.name, version=package.version,
            task=task)
        started = time.monotonic()
        actor = EngineGuard.runtime_actor(package.name)

        # Lifecycle boundary: only enabled agents dispatch.
        if package.status != LifecycleState.ENABLED.value:
            refusal = _boundary_refusal(
                package.name, "lifecycle",
                f"Agent status is {package.status!r}; only "
                f"'{LifecycleState.ENABLED.value}' agents can run")
            self._record(refusal)
            report.error = refusal["detail"]
            report.refusals.append(refusal)
            report.elapsed_ms = round((time.monotonic() - started) * 1000, 1)
            return report

        # Resource boundary: per-agent quotas, enforced before any work.
        self.governor.set_limits(
            package.name,
            max_runs_per_hour=package.spec.resource_limits.max_runs_per_hour,
            max_concurrent=package.spec.resource_limits.max_concurrent)
        ok, reason = self.governor.check(package.name)
        if not ok:
            refusal = _boundary_refusal(package.name, "resource-limits",
                                        reason)
            self._record(refusal)
            report.error = reason
            report.refusals.append(refusal)
            report.elapsed_ms = round((time.monotonic() - started) * 1000, 1)
            return report
        self.governor.begin(package.name)
        try:
            self._run_inner(package, task, report, actor=actor,
                            approved=approved, run_id=run_id,
                            context=context)
        except Exception as exc:  # never crash the dispatcher; report honestly
            report.success = False
            report.error = f"Agent run failed internally: {exc}"[:500]
            report.memory.pop("stored", None)
        finally:
            self.governor.end(package.name)
        report.elapsed_ms = round((time.monotonic() - started) * 1000, 1)
        return report

    # -- run internals ------------------------------------------------------------

    def _run_inner(self, package: Any, task: str, report: EngineRunReport,
                   *, actor: str, approved: bool, run_id: str,
                   context: str) -> None:
        spec = package.spec
        # Memory: recall prior knowledge for context.
        recalled_keys = self.memory_keys(package)
        memory_context = ""
        if recalled_keys:
            memories = [f"{key}: {self.recall(package, key)['value']}"
                        for key in recalled_keys[:10]
                        if self.recall(package, key)["value"] is not None]
            memory_context = "\n".join(memories)
        report.memory["recalled"] = recalled_keys[:10]

        # Model Fabric: route through the fabric under the spec's needs.
        requirements = spec.model_requirements
        request = ModelRequest(
            prompt=task,
            capability=requirements.capabilities[0]
            if requirements.capabilities else spec.primary_capability,
            required_capabilities=tuple(requirements.capabilities)
            or (spec.primary_capability,),
            context="\n".join(part for part in (context, memory_context)
                              if part),
            task=f"agent:{spec.name}",
            min_context_window=requirements.min_context_tokens,
            max_output_tokens=spec.resource_limits.max_output_tokens,
            prefer_local=requirements.prefer_local,
            prefer_free=not requirements.allow_paid,
            metadata={"agent": spec.name, "agent_version": package.version},
        )
        response = self.bundle.fabric.generate(request)
        report.model = {
            "success": bool(response.success),
            "model": response.model,
            "provider": response.provider,
            "capability": request.capability,
            "error": response.error[:200],
        }
        if not response.success:
            report.error = f"Model fabric refused the request: " \
                           f"{response.error[:300]}"
            return
        report.model["latency_ms"] = round(response.latency_ms, 1)
        report.model["output_tokens"] = response.output_tokens

        # The response is untrusted: parse a change proposal out of it.
        changes = self._parse_changes(response.text)
        if not changes:
            # A text-only answer is a legitimate outcome (research
            # agents, Q&A) — nothing to write, nothing to verify.
            report.success = True
            report.memory["stored"] = self._remember_outcome(
                package, task, "text-only answer", report)
            return

        # Permission ceiling: every proposed write must be in it.
        applicable = []
        for change in changes:
            path = change.get("path", "")
            action = change.get("action", "modify")
            resource = Resource.FILESYSTEM
            operation = "delete" if action == "delete" else "write"
            if spec.allows(resource, operation):
                applicable.append(change)
            else:
                refusal = _boundary_refusal(
                    package.name, "permission-ceiling",
                    f"Proposed {operation} of {path!r} is outside this "
                    "agent's permission ceiling")
                self._record(refusal)
                report.refusals.append(refusal)
        if report.refusals:
            report.error = ("The model proposed operations outside this "
                            "agent's permission ceiling; nothing was "
                            "written")
            report.memory["stored"] = self._remember_outcome(
                package, task, "refused out-of-ceiling proposal", report)
            return

        # PolicyGate + Checkpoints via the ChangeApplier transaction.
        applier = ChangeApplier(
            self.bundle.runtime,
            checkpoint_manager=self.bundle.checkpoint_manager,
            root=self.bundle.root,
            policy_gate=self.bundle.policy_gate,
            approval_store=self.bundle.approval_store,
            approval_callback=self._approval_hook(package),
        )
        result = applier.apply(applicable, approved=approved,
                               label=f"agent:{package.name}:{run_id}",
                               capability=spec.primary_capability,
                               actor=actor, task_id=f"agent-run-{run_id}")
        report.checkpoint_id = result.checkpoint_id or ""
        report.changes = list(result.changed_paths)
        for decision in result.decisions:
            if decision.get("decision") == "DENY":
                refusal = _boundary_refusal(
                    package.name, "policy-gate",
                    f"PolicyGate denied a proposed change: "
                    f"{decision.get('reason', '')}")
                self._record(refusal)
                report.refusals.append(refusal)
        if not result.success:
            report.error = "; ".join(result.errors)[:500] or \
                "Change set was not applied"
            report.rolled_back = result.rolled_back
            report.memory["stored"] = self._remember_outcome(
                package, task, "failed change set", report)
            return

        # Verification gates over exactly what the agent wrote.
        gates = self._verify(package, report.changes)
        report.verification = gates
        failed = [gate for gate in gates if not gate.get("passed")]
        if failed:
            # Verification failure: roll back exactly this run's files.
            report.rolled_back = self._rollback_checkpoint(
                result, report.changes)
            report.error = "Verification failed: " + "; ".join(
                gate.get("name", "?") for gate in failed)
            report.memory["stored"] = self._remember_outcome(
                package, task, "verification failure (rolled back)",
                report)
            return

        report.success = True
        report.memory["stored"] = self._remember_outcome(
            package, task, "applied change set", report)

    def _approval_hook(self, package: Any) -> Callable[..., bool] | None:
        """Wrap the operator callback so DENY never reaches the operator."""
        callback = self.bundle.approval_callback
        if callback is None:
            return None

        def hook(**kwargs: Any) -> bool:
            kwargs.setdefault("agent", EngineGuard.runtime_actor(
                package.name))
            return bool(callback(**kwargs))

        return hook

    def _rollback_checkpoint(self, apply_result: Any,
                             changed: list[str]) -> bool:
        """Roll back exactly the files this run wrote.

        Uses the checkpoint the ChangeApplier created for this
        transaction; restores only the candidate paths and leaves
        unrelated work alone. Returns whether a rollback happened.
        """
        checkpoint = getattr(apply_result, "checkpoint", None)
        manager = self.bundle.checkpoint_manager
        if checkpoint is None or manager is None:
            return False
        try:
            manager.rollback(checkpoint, sorted(set(changed)))
            return True
        except Exception:  # pragma: no cover - defensive
            return False

    def _verify(self, package: Any, changed: list[str]) -> list[dict]:
        """Run the spec's verification gates over the changed files."""
        requirements = package.spec.verification
        gates: list[dict] = []
        pipeline = self.bundle.verification
        if pipeline is None:
            return gates
        if requirements.run_security:
            result = pipeline.security(changed)
            gates.append({"name": "security", "passed": result.passed,
                          "details": result.details[:500]})
        if requirements.run_review:
            result = pipeline.review("", changed)
            gates.append({"name": "review", "passed": result.passed,
                          "details": result.details[:500]})
        if requirements.run_tests:
            result = pipeline.tests()
            gates.append({"name": "tests", "passed": result.passed,
                          "details": result.details[:500]})
        return gates

    def _remember_outcome(self, package: Any, task: str, outcome: str,
                          report: EngineRunReport) -> dict[str, Any]:
        key = f"runs/{report.run_id}"
        value = json.dumps({
            "task": task[:200], "outcome": outcome,
            "success": report.success,
            "changes": report.changes[:20],
        })[:MAX_MEMORY_VALUE]
        return self.remember(package, key, value)

    # -- model output parsing ---------------------------------------------------

    @staticmethod
    def _parse_changes(text: str) -> list[dict[str, Any]]:
        """Extract a change proposal from untrusted model text.

        Accepts the structured coder schema (``changes`` list or
        mapping). Anything else is treated as a plain answer.
        """
        if not text:
            return []
        candidate = text.strip()
        if candidate.startswith("```"):
            candidate = candidate.strip("`")
            if candidate.startswith("json"):
                candidate = candidate[4:]
        try:
            data = json.loads(candidate)
        except (ValueError, TypeError):
            return []
        if not isinstance(data, dict) or "changes" not in data:
            return []
        changes = data["changes"]
        if isinstance(changes, dict):
            return [{"path": path, "action": "modify", "content": content}
                    for path, content in changes.items()
                    if isinstance(path, str) and isinstance(content, str)]
        if not isinstance(changes, list):
            return []
        parsed: list[dict[str, Any]] = []
        for entry in changes:
            if not isinstance(entry, dict):
                continue
            path = entry.get("path")
            content = entry.get("content", "")
            action = entry.get("action", "modify")
            if not isinstance(path, str) or not path:
                continue
            if action not in ("create", "modify", "delete"):
                continue
            if action != "delete" and not isinstance(content, str):
                continue
            parsed.append({"path": path, "action": action,
                           "content": content,
                           "risk": entry.get("risk", "NONE")})
        return parsed
