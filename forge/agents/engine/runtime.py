"""Agent execution (A81): every run goes through the real subsystems.

An agent run is a fixed pipeline, and no stage can be skipped by the
agent itself:

.. code-block:: text

    lifecycle gate → resource budget → Model Fabric → sandboxed tools
    → Tool Runtime → PolicyGate → verification → checkpoint/rollback
    → memory (policy-bounded) → recorded history

The agent only ever sees an :class:`AgentSandbox`. The sandbox exposes
``use_tool``, ``remember``, ``recall``, and ``note`` — nothing else. There
is no call path by which a running agent can widen its own permissions,
touch another agent's memory, write outside its path rules, or bypass the
PolicyGate: ungranted operations are ``BLOCKED`` in the agent's own
permission view before any tool is reached, and every write is checked
against the spec, the ledger, and the gate in that order.

If verification fails after writes, the pre-write checkpoint is rolled
back and the run is reported as failed — never as partially successful.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any
from uuid import uuid4

from forge.agents.engine.errors import (
    AgentIsolationError,
    AgentLimitError,
    AgentPermissionError,
)
from forge.agents.engine.governor import AgentGovernor, RunBudget
from forge.agents.engine.grants import GrantLedger
from forge.agents.engine.memory import AgentMemory
from forge.agents.engine.package import AgentPackage, PackageStore
from forge.agents.engine.spec import (
    GRANTABLE_OPERATIONS,
    PROTECTED_PATH_PARTS,
    TOOL_CATALOG,
    AgentSpec,
)
from forge.memory.store import MemoryStore
from forge.runtime.runtime import ToolDefinition, ToolRuntime
from forge.security.permissions import (
    OperationMode,
    PermissionLevel,
    PermissionManager,
)
from forge.security.policy_gate import PolicyDecision, PolicyGate
from forge.tools.checkpoint import CheckpointManager

#: Restrictiveness order: lower is stricter. An agent's effective mode is
#: the stricter of the session mode and its spec ceiling.
_MODE_RANK = {OperationMode.LOCKED: 0, OperationMode.SAFE: 1,
              OperationMode.ASSISTED: 2, OperationMode.AUTONOMOUS: 3}

_CREDENTIAL_TOKENS = ("credential", "secret", "private_key", "token")


def stricter_mode(*modes: Any) -> OperationMode:
    resolved = [mode if isinstance(mode, OperationMode)
                else OperationMode(mode) for mode in modes if mode]
    if not resolved:
        return OperationMode.ASSISTED
    return min(resolved, key=lambda mode: _MODE_RANK[mode])


@dataclass
class ActionRecord:
    """What the sandbox did (or refused) for one requested action."""

    tool: str
    allowed: bool
    reason: str = ""
    output: str = ""
    path: str = ""
    decision: str = ""

    def to_dict(self) -> dict:
        return {"tool": self.tool, "allowed": self.allowed,
                "reason": self.reason, "output": self.output[:500],
                "path": self.path, "decision": self.decision}


@dataclass
class GateRecord:
    """One verification gate result for this run."""

    name: str
    passed: bool
    details: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "passed": self.passed,
                "details": self.details}


@dataclass
class AgentRunResult:
    """Structured, auditable outcome of one agent run."""

    agent: str
    run_id: str
    success: bool
    stage: str
    output: str = ""
    error: str = ""
    state: str = ""
    task_id: str = ""
    model: str = ""
    provider: str = ""
    latency_ms: float = 0.0
    duration_ms: float = 0.0
    actions: list = field(default_factory=list)
    gates: list = field(default_factory=list)
    refusals: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    memory: list = field(default_factory=list)
    budget: dict = field(default_factory=dict)
    checkpoint: str = ""
    rolled_back: bool = False
    files_changed: list = field(default_factory=list)

    @property
    def allowed_actions(self) -> list:
        return [action for action in self.actions if action.allowed]

    def to_dict(self) -> dict:
        return {
            "agent": self.agent, "run_id": self.run_id,
            "success": self.success, "stage": self.stage,
            "output": self.output[:2000], "error": self.error,
            "state": self.state, "task_id": self.task_id,
            "model": self.model, "provider": self.provider,
            "latency_ms": self.latency_ms,
            "duration_ms": round(self.duration_ms, 3),
            "actions": [action.to_dict() for action in self.actions],
            "gates": [gate.to_dict() for gate in self.gates],
            "refusals": list(self.refusals),
            "notes": list(self.notes[:20]),
            "memory": list(self.memory),
            "budget": dict(self.budget),
            "checkpoint": self.checkpoint,
            "rolled_back": self.rolled_back,
            "files_changed": list(self.files_changed),
        }


class AgentSandbox:
    """The only surface a running agent gets.

    Deliberately narrow: there is no ``grant``, ``set_mode``, ``policy``,
    or ``store`` attribute, so an agent has no vocabulary for escalating
    itself. Everything it can do is checked against the spec, the grant
    ledger, and the PolicyGate.
    """

    def __init__(self, *, package: AgentPackage, ledger: GrantLedger,
                 memory: AgentMemory, tools: ToolRuntime, gate: PolicyGate,
                 budget: RunBudget, manager: PermissionManager,
                 root: Any, actor: str, task_id: str,
                 approved: bool = False) -> None:
        self._package = package
        self._spec: AgentSpec = package.spec
        self._ledger = ledger
        self._memory = memory
        self._tools = tools
        self._gate = gate
        self._budget = budget
        self._manager = manager
        self._root = root
        self._actor = actor
        self._task_id = task_id
        self._approved = approved
        self.actions: list = []
        self.refusals: list = []
        self.notes: list = []
        self.memory_written: list = []
        self.files_changed: list = []
        self.checkpoint = None
        self.checkpoint_manager: Any = None
        self._calls: dict = {}

    # -- tools -----------------------------------------------------------

    def allowed_tools(self) -> tuple:
        return tuple(grant.name for grant in self._spec.tools)

    def use_tool(self, name: str, **kwargs: Any) -> dict:
        """Execute one declared tool through every gate, or refuse."""
        self._budget.check_wall()
        self._budget.charge_tool()
        name = (name or "").strip()
        kwargs = self._declared_arguments(name, kwargs)
        record = ActionRecord(tool=name, allowed=False,
                              path=str(kwargs.get("path", "") or ""))
        try:
            self._authorize_tool(name, kwargs, record)
        except (AgentPermissionError, AgentLimitError) as exc:
            record.reason = str(exc)
            record.decision = PolicyDecision.DENY.value
            self.actions.append(record)
            self.refusals.append({"tool": name, "reason": str(exc)})
            return {"allowed": False, "reason": str(exc)}

        if self.checkpoint is None and self._writes(name):
            self._take_checkpoint()
        result = self._tools.execute(
            name, approved=self._approved, actor=self._package.name,
            task_id=self._task_id, risk=self._risk(name), **kwargs)
        record.allowed = bool(result.success)
        record.output = result.output or ""
        record.reason = result.error or ""
        record.decision = (PolicyDecision.ALLOW.value if result.success
                           else PolicyDecision.DENY.value)
        self.actions.append(record)
        if result.success:
            if self._writes(name):
                self._budget.charge_write()
                path = str(kwargs.get("path", "") or "")
                if path and path not in self.files_changed:
                    self.files_changed.append(path)
            self._budget.charge_output(len(result.output or ""))
        else:
            self.refusals.append({"tool": name,
                                  "reason": result.error or "tool failed"})
        return {"allowed": bool(result.success),
                "output": result.output, "error": result.error or ""}

    # -- memory ----------------------------------------------------------

    def remember(self, key: str, value: Any, *, shared: bool = False) -> dict:
        try:
            entry = self._memory.remember(key, value, shared=shared)
        except (AgentPermissionError, AgentIsolationError, ValueError) as exc:
            self.refusals.append({"tool": "memory.remember",
                                  "reason": str(exc)})
            raise
        self.memory_written.append(entry)
        return entry

    def recall(self, key: str, *, shared: bool = False,
               owner: str = "") -> Any:
        try:
            return self._memory.recall(key, shared=shared, owner=owner)
        except (AgentPermissionError, AgentIsolationError, ValueError) as exc:
            self.refusals.append({"tool": "memory.recall",
                                  "reason": str(exc)})
            raise

    def keys(self, *, shared: bool = False) -> list:
        """This agent's own memory keys — never another agent's."""
        return self._memory.keys(shared=shared)

    def note(self, text: str) -> None:
        """Free-form progress note recorded with the run (bounded)."""
        cleaned = str(text or "").strip()
        if cleaned and len(self.notes) < 20:
            self.notes.append(cleaned[:500])

    # -- internals -------------------------------------------------------

    def _declared_arguments(self, name: str, kwargs: dict) -> dict:
        """Keep only the arguments the catalog says this tool accepts.

        Tool arguments originate in model output, so an unknown or
        dunder-named key is dropped rather than forwarded to a handler.
        A tool the catalog does not know keeps no arguments at all — it
        will be refused a moment later anyway.
        """
        info = TOOL_CATALOG.get(name)
        if info is None:
            return {}
        allowed = set(info.arguments)
        return {key: value for key, value in kwargs.items()
                if key in allowed}

    def _writes(self, name: str) -> bool:
        info = TOOL_CATALOG.get(name)
        return bool(info and info.writes)

    def _risk(self, name: str) -> str:
        info = TOOL_CATALOG.get(name)
        return info.risk if info else "HIGH"

    def _declared(self, name: str):
        for grant in self._spec.tools:
            if grant.name == name:
                return grant
        return None

    def _authorize_tool(self, name: str, kwargs: dict,
                        record: ActionRecord) -> None:
        if not name:
            raise AgentPermissionError("A tool name is required")
        grant = self._declared(name)
        if grant is None:
            raise AgentPermissionError(
                "Tool %r is not in the specification for agent %r "
                "(declared: %s)"
                % (name, self._spec.name,
                   ", ".join(self.allowed_tools()) or "none"))
        if name not in self._tools.tools:
            raise AgentPermissionError(
                "Tool %r is not registered in this runtime" % name)
        used = self._calls.get(name, 0)
        if used >= grant.max_calls:
            raise AgentLimitError(
                "Per-run call limit reached for tool %r (%d)"
                % (name, grant.max_calls))
        self._calls[name] = used + 1

        operation = TOOL_CATALOG[name].operation
        if not self._ledger.holds(operation):
            raise AgentPermissionError(
                "Operation %r is not granted to agent %r"
                % (operation, self._spec.name))

        path = str(kwargs.get("path", "") or "")
        if path:
            self._authorize_path(name, path)

        outcome = self._gate.evaluate(
            operation=operation, path=path, tool=name,
            risk=self._risk(name),
            capability=(self._spec.capabilities[0]
                        if self._spec.capabilities else ""),
            approved=self._approved, mode=self._manager.mode,
            agent=self._spec.name, task_id=self._task_id)
        record.decision = outcome.decision.value
        if outcome.decision != PolicyDecision.ALLOW:
            raise AgentPermissionError(
                "PolicyGate %s for %s: %s"
                % (outcome.decision.value, operation, outcome.reason))

    def _authorize_path(self, name: str, path: str) -> None:
        permissions = self._spec.permissions
        candidate = PurePosixPath(path)
        if not path or candidate.is_absolute() or "\\" in path \
                or ".." in candidate.parts:
            raise AgentPermissionError(
                "Path must be project-relative without traversal: %r" % path)
        parts = candidate.parts
        for part in PROTECTED_PATH_PARTS:
            if part in parts:
                raise AgentPermissionError(
                    "Path %r touches protected directory %r" % (path, part))
        lowered = candidate.name.lower()
        if lowered == ".env" or lowered.endswith(".env") or \
                any(token in lowered for token in _CREDENTIAL_TOKENS):
            raise AgentPermissionError(
                "Path %r looks like credential material" % path)
        for rule in permissions.denied_paths:
            if path == rule or path.startswith(rule.rstrip("/") + "/"):
                raise AgentPermissionError(
                    "Path %r is denied by the specification" % path)
        allowed = tuple(permissions.allowed_paths)
        if allowed and not any(
                path == rule or path.startswith(rule.rstrip("/") + "/")
                for rule in allowed):
            raise AgentPermissionError(
                "Path %r is outside the allowed paths (%s)"
                % (path, ", ".join(allowed)))

    def _take_checkpoint(self) -> None:
        """Snapshot the worktree before the first write of this run.

        The snapshot covers the whole project (minus runtime state), so
        rollback can restore any path this run later touches — including
        files that do not exist yet.
        """
        manager = CheckpointManager(self._root)
        self.checkpoint = manager.create(
            label="agent-%s" % self._spec.name,
            declared=list(self.files_changed))
        self.checkpoint_manager = manager

    def rollback(self) -> bool:
        """Restore the pre-write snapshot. ``False`` when there was none.

        Reporting a rollback that never happened would be a lie in the run
        record, so callers must use this return value rather than assume.
        """
        if self.checkpoint is None or self.checkpoint_manager is None:
            return False
        self.checkpoint_manager.rollback(self.checkpoint,
                                         changed_files=self.files_changed)
        return True

    def cleanup(self) -> None:
        if self.checkpoint is not None and self.checkpoint_manager is not None:
            self.checkpoint_manager.cleanup(self.checkpoint)


class AgentRuntime:
    """Runs enabled agents through Model Fabric, PolicyGate, and tools."""

    def __init__(self, *, root: str = ".", store: PackageStore | None = None,
                 fabric: Any = None, permission_manager: Any = None,
                 tool_runtime: Any = None, memory_store: Any = None,
                 governor: AgentGovernor | None = None,
                 audit: Any = None) -> None:
        from pathlib import Path

        self.root = str(Path(root).resolve())
        self.store = store or PackageStore(self.root)
        self.fabric = fabric
        self.permission_manager = permission_manager or PermissionManager()
        self.audit = audit if audit is not None else \
            getattr(self.permission_manager, "audit", None)
        self.governor = governor or AgentGovernor()
        self._tool_runtime = tool_runtime
        self._memory_store = memory_store

    # -- construction helpers -------------------------------------------

    def tool_runtime(self) -> ToolRuntime:
        """The host tool runtime, built from Forge defaults on first use."""
        if self._tool_runtime is None:
            from forge.runtime.defaults import create_default_runtime

            self._tool_runtime = create_default_runtime(
                self.permission_manager, self.root)
        return self._tool_runtime

    def memory_store(self) -> MemoryStore:
        if self._memory_store is None:
            from pathlib import Path

            self._memory_store = MemoryStore(
                str(Path(self.root) / ".forge" / "memory"))
        return self._memory_store

    def agent_manager(self, package: AgentPackage,
                      ledger: GrantLedger) -> PermissionManager:
        """A permission view for one agent: ceiling mode, granted ops only.

        Operations the ledger has not granted are ``BLOCKED`` here, so a
        refusal happens in the permission layer even if a caller forgets
        the sandbox check. This view can only be stricter than the host
        session's — never looser.
        """
        base = self.permission_manager
        manager = PermissionManager(
            rules=dict(getattr(base, "rules", {}) or {}),
            mode=strict_mode_for(package.spec, base),
            policy=getattr(base, "policy", None),
            store=getattr(base, "store", None),
            agent=package.name,
            audit=getattr(base, "audit", None))
        granted = set(ledger.active_operations())
        for operation in GRANTABLE_OPERATIONS:
            if operation not in granted:
                manager.rules[operation] = PermissionLevel.BLOCKED
        for operation in ("delete_repository", "expose_secrets"):
            manager.rules[operation] = PermissionLevel.BLOCKED
        return manager

    def sandbox(self, package: AgentPackage, *, actor: str,
                approved: bool = False, task_id: str = "",
                budget: RunBudget | None = None) -> AgentSandbox:
        """Build a sandbox for one agent (used by runs and by benchmarks)."""
        ledger = GrantLedger(package.name, package.spec,
                             self.store.read_grants(package.name))
        manager = self.agent_manager(package, ledger)
        runtime = ToolRuntime(manager)
        for tool in self.tool_runtime().list_tools():
            runtime.register(ToolDefinition(
                name=tool.name, description=tool.description,
                handler=tool.handler, permission=tool.permission))
        memory = AgentMemory(self.memory_store(), package.name,
                             package.spec.memory)
        return AgentSandbox(
            package=package, ledger=ledger, memory=memory, tools=runtime,
            gate=PolicyGate(manager),
            budget=budget or RunBudget(limits=package.spec.limits,
                                       started_at=time.time()),
            manager=manager, root=self.root, actor=actor, task_id=task_id,
            approved=approved)

    # -- the run ---------------------------------------------------------

    def run(self, agent: str, task: str, *, actor: str = "operator",
            approved: bool = False, task_id: str = "",
            instructions: str = "") -> AgentRunResult:
        """Execute one agent run end to end."""
        started = time.time()
        run_id = uuid4().hex[:16]
        task = (task or "").strip()
        package = self.store.load(agent)
        result = AgentRunResult(agent=package.name, run_id=run_id,
                                success=False, stage="lifecycle",
                                state=package.state, task_id=task_id)
        if not task:
            result.stage = "input"
            result.error = "A task is required"
            self._finish(package, result, started)
            return result
        refusal = lifecycle_refusal(package)
        if refusal:
            result.error = refusal
            self._finish(package, result, started)
            return result

        budget: RunBudget | None = None
        sandbox: AgentSandbox | None = None
        try:
            budget = self.governor.begin(package.name, package.spec.limits)
        except AgentLimitError as exc:
            result.stage = "limits"
            result.error = str(exc)
            self._finish(package, result, started)
            return result

        try:
            sandbox = self.sandbox(package, actor=actor, approved=approved,
                                   task_id=task_id, budget=budget)
            result.stage = "model"
            if self.fabric is None:
                raise AgentPermissionError(
                    "No Model Fabric is configured, so the agent cannot run")
            response = self._model_call(package, task, instructions, budget)
            result.model = response.get("model", "")
            result.provider = response.get("provider", "")
            result.latency_ms = response.get("latency_ms", 0.0)
            if not response.get("success"):
                raise AgentPermissionError(
                    "Model Fabric refused the request: %s"
                    % response.get("error", "unknown error"))
            if response.get("fallback") and \
                    not package.spec.model.allow_fallback:
                raise AgentPermissionError(
                    "The offline placeholder answered; the spec requires a "
                    "real model (model.allow_fallback=false)")
            payload = _parse_actions(response.get("text", ""))
            result.output = (payload.get("summary")
                             or response.get("text", "")).strip()[:4000]
            result.stage = "tools"
            for action in payload.get("actions", [])[:16]:
                sandbox.use_tool(str(action.get("tool", "")),
                                 **_safe_kwargs(action.get("args")))
            result.stage = "memory"
            for key, value in list(
                    (payload.get("memory") or {}).items())[:16]:
                try:
                    sandbox.remember(str(key), value)
                except (AgentPermissionError, AgentIsolationError,
                        ValueError):
                    pass  # recorded as a refusal inside the sandbox
            result.stage = "verification"
            gates = self._verify(package, sandbox, budget)
            result.gates = gates
            failed = [gate for gate in gates if not gate.passed]
            if failed:
                result.rolled_back = sandbox.rollback()
                result.stage = "verification"
                result.success = False
                result.error = "Verification failed: %s" % "; ".join(
                    "%s (%s)" % (gate.name, gate.details) for gate in failed)
            else:
                sandbox.cleanup()
                result.success = True
                result.stage = "complete"
        except (AgentPermissionError, AgentIsolationError,
                AgentLimitError) as exc:
            result.error = str(exc)
            if sandbox is not None:
                result.rolled_back = sandbox.rollback()
        except Exception as exc:  # unexpected: fail closed, keep the record
            result.error = "%s: %s" % (type(exc).__name__, exc)
            if sandbox is not None:
                result.rolled_back = sandbox.rollback()
        finally:
            self.governor.end(package.name)

        if sandbox is not None:
            result.actions = list(sandbox.actions)
            result.refusals = list(sandbox.refusals)
            result.notes = list(sandbox.notes)
            result.memory = list(sandbox.memory_written)
            result.files_changed = list(sandbox.files_changed)
            result.checkpoint = (sandbox.checkpoint.id
                                 if sandbox.checkpoint is not None else "")
        result.budget = budget.to_dict() if budget is not None else {}
        self._finish(package, result, started)
        return result

    # -- internals -------------------------------------------------------

    def _model_call(self, package: AgentPackage, task: str,
                    instructions: str, budget: RunBudget) -> dict:
        from forge.models.readiness import is_fallback_response
        from forge.models.request import ModelRequest

        budget.charge_model()
        model = package.spec.model
        capability = model.capabilities[0] if model.capabilities else "coding"
        request = ModelRequest(
            prompt=_prompt_for(package.spec, task, instructions),
            capability=capability,
            required_capabilities=tuple(model.capabilities),
            task=package.spec.name,
            context=package.spec.purpose,
            min_context_window=model.min_context_window,
            max_output_tokens=model.max_output_tokens or None,
            prefer_local=model.prefer_local, prefer_free=model.prefer_free,
            max_cost_per_token=model.max_cost_per_token or None,
            max_latency_ms=model.max_latency_ms or None)
        response = self.fabric.generate(request)
        fallback = False
        try:
            fallback = bool(is_fallback_response(
                self.fabric, response.model or "", response.provider or ""))
        except Exception:
            fallback = False
        budget.charge_output(len(response.text or ""))
        return {"success": bool(response.success), "text": response.text,
                "model": response.model, "provider": response.provider,
                "latency_ms": response.latency_ms,
                "error": response.error, "fallback": fallback}

    def _verify(self, package: AgentPackage, sandbox: AgentSandbox,
                budget: RunBudget) -> list:
        from forge.security.verification import VerificationPipeline

        spec = package.spec.verification
        gates: list = []
        changed = list(sandbox.files_changed)
        if not changed:
            # Nothing was written: the write-scoped gates have nothing to
            # judge, and saying so is honest — they are not "passed".
            return gates
        pipeline = VerificationPipeline(self.root)
        if spec.require_security_scan:
            outcome = pipeline.security(changed)
            findings = list(outcome.evidence.get("findings", []) or [])
            passed = (outcome.passed
                      and len(findings) <= spec.max_security_findings)
            gates.append(GateRecord(
                "security", passed,
                outcome.details or ("%d finding(s)" % len(findings))))
        if spec.require_review:
            outcome = pipeline.review("", changed)
            gates.append(GateRecord("review", outcome.passed,
                                    outcome.details))
        if spec.require_tests:
            gates.append(self._run_tests(package, sandbox, budget))
        return gates

    def _run_tests(self, package: AgentPackage, sandbox: AgentSandbox,
                   budget: RunBudget) -> GateRecord:
        import sys

        if "run_tests" not in sandbox.allowed_tools():
            return GateRecord("tests", False,
                              "spec requires tests but has no run_tests tool")
        command = [sys.executable, "-m", "pytest", "-q",
                   "-p", "no:cacheprovider"]
        outcome = sandbox.use_tool("run_tests", command=command)
        if not outcome.get("allowed"):
            return GateRecord("tests", False,
                              str(outcome.get("reason")
                                  or outcome.get("error") or "tests refused"))
        text = str(outcome.get("output") or "")
        if "no tests ran" in text.lower():
            return GateRecord("tests", False, "no tests ran")
        return GateRecord("tests", True, text.strip()[-300:])

    def _finish(self, package: AgentPackage, result: AgentRunResult,
                started: float) -> None:
        result.duration_ms = (time.time() - started) * 1000.0
        entry = {
            "run_id": result.run_id, "at": time.time(),
            "success": result.success, "stage": result.stage,
            "state": result.state, "error": result.error[:500],
            "model": result.model, "provider": result.provider,
            "actions": len(result.actions),
            "refusals": len(result.refusals),
            "files_changed": list(result.files_changed),
            "rolled_back": result.rolled_back,
            "duration_ms": round(result.duration_ms, 3),
        }
        try:
            self.store.append_history(package.name, entry)
            package.run_count += 1
            package.last_run = entry
            self.store.write_manifest(package)
        except Exception:
            # A bookkeeping failure must not mask the run outcome.
            pass
        if self.audit is not None:
            try:
                self.audit.record_decision(
                    agent=package.name, resource="agent",
                    operation="agent_run", scope=result.stage,
                    decision=(PolicyDecision.ALLOW if result.success
                              else PolicyDecision.DENY),
                    reason=result.error or "run completed",
                    task_id=result.task_id)
            except Exception:
                pass


def strict_mode_for(spec: AgentSpec, manager: Any) -> OperationMode:
    """Effective mode: the stricter of the session mode and the ceiling."""
    ceiling = OperationMode(spec.permissions.mode_ceiling)
    session = getattr(manager, "mode", OperationMode.ASSISTED)
    return stricter_mode(session, ceiling)


def lifecycle_refusal(package: AgentPackage) -> str:
    """Why this agent cannot run right now (``""`` when it can).

    Kept as a standalone function so the benchmark suite can exercise the
    exact gate :meth:`AgentRuntime.run` uses, in every lifecycle state,
    without having to move a real agent around.
    """
    if package.lifecycle.runnable:
        return ""
    return ("Agent %r is %s; only an enabled agent can run"
            % (package.name, package.state))


def _prompt_for(spec: AgentSpec, task: str, instructions: str) -> str:
    lines = [
        "You are the Forge agent %r." % spec.name,
        "Purpose: %s" % spec.purpose,
        "Capabilities: %s" % ", ".join(spec.capabilities),
        "Tools you may request: %s" % (", ".join(spec.tool_names()) or "none"),
    ]
    if instructions:
        lines.append("Operator instructions: %s" % instructions.strip())
    lines.extend([
        "",
        "Task: %s" % task,
        "",
        "Answer with a JSON object using exactly these keys: "
        '"summary" (string), "actions" (list of {"tool", "args"}), '
        '"memory" (object of durable facts). Request only tools you are '
        "listed as allowed to use; anything else will be refused.",
    ])
    return "\n".join(lines)


def _parse_actions(text: str) -> dict:
    """Extract the structured action payload from a model response.

    A response that is not JSON is not an error: text-only agents
    (research, documentation) legitimately answer in prose. Only the
    ``actions``/``memory`` keys are consumed, and anything malformed is
    dropped rather than executed.
    """
    candidate = (text or "").strip()
    if candidate.startswith("```"):
        candidate = candidate.strip("`")
        if candidate.lower().startswith("json"):
            candidate = candidate[4:]
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start < 0 or end <= start:
        return {"summary": (text or "").strip(), "actions": [], "memory": {}}
    try:
        payload = json.loads(candidate[start:end + 1])
    except ValueError:
        return {"summary": (text or "").strip(), "actions": [], "memory": {}}
    if not isinstance(payload, dict):
        return {"summary": (text or "").strip(), "actions": [], "memory": {}}
    actions = payload.get("actions")
    if not isinstance(actions, list):
        actions = []
    cleaned = [item for item in actions
               if isinstance(item, dict) and isinstance(
                   item.get("tool"), str)]
    memory = payload.get("memory")
    if not isinstance(memory, dict):
        memory = {}
    return {"summary": str(payload.get("summary", "") or ""),
            "actions": cleaned, "memory": memory}


def _safe_kwargs(args: Any) -> dict:
    """Tool arguments from a model are untrusted: only plain values pass."""
    if not isinstance(args, dict):
        return {}
    safe: dict = {}
    for key, value in args.items():
        if not isinstance(key, str) or not key.isidentifier():
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            safe[key] = value
        elif isinstance(value, (list, tuple)) and all(
                isinstance(item, (str, int, float)) for item in value):
            safe[key] = list(value)
    return safe
