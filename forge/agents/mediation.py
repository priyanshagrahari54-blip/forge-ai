"""Mediated execution for created agents.

A created agent never touches the world directly. :class:`GatedAgentRuntime`
is the single choke point, and every run passes through the six Forge
subsystems:

* **Model Fabric** — the only route to a model, bounded by the spec's
  model requirements. With no fabric, runs fail honestly (``NO_MODEL``)
  instead of fabricating output.
* **PolicyGate** — every mutating tool call is authorized before execution,
  and only an explicit ``ALLOW`` proceeds: ``REQUIRE_APPROVAL`` without a
  redeemed approval token (or any unknown decision) refuses.
* **Tool Runtime** — tools execute only through the runtime, only when the
  spec's allowlist names them, only when a recorded grant covers the
  call's target scope, and only inside the per-run tool budget.
  ``memory_read``/``memory_write`` are served by the runtime itself
  against the agent's isolated namespace (grant-checked like the rest).
* **Memory** — namespaced per agent (``agent-<name>/``); cross-agent reads
  are refused, entries are bounded by the memory policy.
* **Verification** — output is scanned (secrets/dangerous patterns) and,
  per the verification requirements, reviewed and/or test-gated. The
  tests gate runs a fixed project-pytest command through the constrained
  ``run_tests`` tool — callers supply no command, so there is nothing to
  inject.
* **Checkpoints** — a checkpoint is captured before any mutating tool
  call; tool or verification failures roll the changed files back.
  Mutating runs without a checkpoint manager are refused outright.

Isolation invariants (also covered by tests):

* non-``enabled`` packages are refused before any subsystem runs;
* every run needs a named, non-agent actor, recorded in the report;
* an agent's approver may never be the agent itself (no self-grants);
* one agent can never read or write another agent's memory namespace;
* resource limits (runs/hour, concurrency, tool calls, wall clock) are
  enforced with no side effects on refusal;
* ``approved=True`` is trusted-local-operator consent (the same flag the
  gate and tool runtime honor): remote API callers can never set it —
  API approval flows through redeemable approval tokens only.
"""
from __future__ import annotations

import os
import sys
import time
from typing import Any
from uuid import uuid4

from forge.agents.creation import (_is_self_admin, agent_identity,
                                   spec_fingerprint)
from forge.agents.specs import MEDIATED_TOOLS
# _match_fs_pattern is private to the policy module; the engine
# imports it deliberately so grant scopes match with byte-identical
# semantics to the approval store instead of a drifting copy.
from forge.security.policy import _match_fs_pattern


class MediationError(Exception):
    """A refused or failed mediated action, with a stable machine code."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


#: Tool calls that would grant power are never valid tools. They are
#: refused explicitly so the reason names the violation.
FORBIDDEN_TOOL_FRAGMENTS = ("grant", "permission", "approve", "escalat")

#: Power tools mapped onto the grant vocabulary (resource, operation).
#: The allowlist says what the agent may CALL; grants say what the
#: operator APPROVED — every tool call needs a recorded grant covering
#: its target scope. Scope semantics mirror the approval store:
#: filesystem scopes match via ``policy._match_fs_pattern``; terminal
#: scopes pin the exact argv[0]; ``run_tests`` always runs pytest, so it
#: needs terminal/execute pinned to ``"pytest"``; search (query-scoped,
#: unmatchable to a path) needs a broad filesystem/read grant (``""``
#: or ``"**"``); git/memory use exact scope with blank covering any
#: target.
TOOL_GRANTS: dict[str, tuple[str, str]] = {
    "read_file": ("filesystem", "read"),
    "write_file": ("filesystem", "write"),
    "delete_file": ("filesystem", "delete"),
    "search": ("filesystem", "read"),
    "terminal": ("terminal", "execute"),
    "run_tests": ("terminal", "execute"),
    "git_status": ("git", "status"),
    "memory_read": ("memory", "read"),
    "memory_write": ("memory", "write"),
}

#: Gate operation keys differ from tool names in one case: the gate (and
#: token redemption) knows the permission key ``run_command``, not the
#: tool name ``terminal``.
GATE_OPERATIONS = {"terminal": "run_command"}

#: Agent-supplied terminal timeouts are clamped into this window (the
#: verification harness uses its own fixed timeout).
TERMINAL_TIMEOUT_MIN = 1
TERMINAL_TIMEOUT_MAX = 300

#: Risk ranking for choosing the gate risk from the matching grants.
#: Unknown labels rank as HIGH (fail closed, like the gate itself).
_RISK_RANK = {"NONE": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}

#: Fixed verification command: the project pytest suite via the current
#: interpreter. The run_tests tool re-validates this shape, so even a
#: compromised constant cannot smuggle another binary through.
TESTS_COMMAND = [sys.executable, "-m", "pytest", "-q", "-p",
                 "no:cacheprovider"]
TESTS_TIMEOUT = 120


def _spec_of(package: Any) -> dict[str, Any]:
    if isinstance(package, dict):
        spec = package.get("spec", package)
    else:
        spec = getattr(package, "spec", {}) or {}
    return spec if isinstance(spec, dict) else {}


def _name_of(package: Any) -> str:
    if isinstance(package, dict):
        return str(package.get("name", ""))
    return str(getattr(package, "name", ""))


def _state_of(package: Any) -> str:
    if isinstance(package, dict):
        return str(package.get("state", ""))
    return str(getattr(package, "state", ""))


def _limits_of(spec: dict[str, Any]) -> dict[str, Any]:
    limits = spec.get("resource_limits", {})
    return limits if isinstance(limits, dict) else {}


def _grants_of(package: Any) -> list[dict[str, Any]]:
    if isinstance(package, dict):
        grants = package.get("grants", [])
    else:
        grants = getattr(package, "grants", []) or []
    if not isinstance(grants, list):
        return []
    return [grant for grant in grants if isinstance(grant, dict)]


def _version_of(package: Any) -> str:
    if isinstance(package, dict):
        return str(package.get("version", ""))
    return str(getattr(package, "version", "") or "")


def _decision_name(outcome: Any) -> str:
    """Best-effort decision label from a gate outcome (real or fake)."""
    decision = getattr(outcome, "decision", outcome)
    value = getattr(decision, "value", decision)
    return str(value).upper()


def _spec_int(values: dict[str, Any], key: str, default: int) -> int:
    """Read an integer spec bound without silent coercion."""
    raw = values.get(key, default)
    if raw is None:
        raw = default
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise MediationError("BAD_SPEC",
                             "resource limit %r is not an integer" % (key,))
    return raw


def _spec_float(values: dict[str, Any], key: str,
                default: float) -> float:
    """Read a float spec bound without silent coercion."""
    raw = values.get(key, default)
    if raw is None:
        raw = default
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise MediationError("BAD_SPEC",
                             "resource limit %r is not a number" % (key,))
    return float(raw)


class _RunGovernor:
    """Per-agent run/concurrency counters honoring spec resource limits."""

    def __init__(self) -> None:
        self._starts: dict[str, list[float]] = {}
        self._active: dict[str, int] = {}

    def check(self, name: str, limits: dict[str, Any]) -> None:
        now = time.time()
        window = 3600.0
        starts = [moment for moment in self._starts.get(name, [])
                  if now - moment <= window]
        self._starts[name] = starts
        max_runs = _spec_int(limits, "max_runs_per_hour", 60)
        if len(starts) >= max_runs:
            raise MediationError(
                "RATE_LIMITED",
                "agent %r hit its hourly run limit (%d)"
                % (name, max_runs))
        max_active = _spec_int(limits, "max_concurrent", 2)
        if self._active.get(name, 0) >= max_active:
            raise MediationError(
                "CONCURRENCY_LIMITED",
                "agent %r hit its concurrency limit (%d)"
                % (name, max_active))

    def begin(self, name: str) -> None:
        self._starts.setdefault(name, []).append(time.time())
        self._active[name] = self._active.get(name, 0) + 1

    def end(self, name: str) -> None:
        self._active[name] = max(0, self._active.get(name, 0) - 1)


class GatedAgentRuntime:
    """Execute created agents exclusively through Forge subsystems."""

    #: Spec tool names that mutate state (PolicyGate pre-authorization +
    #: checkpoint-before-write). ``run_tests`` is excluded: the default
    #: runtime constrains it to the project pytest suite, which needs no
    #: write approval.
    MUTATING_TOOLS = frozenset({"write_file", "delete_file", "terminal"})

    def __init__(self, *, fabric: Any = None, policy_gate: Any = None,
                 tool_runtime: Any = None, memory_root: str = "",
                 project_root: str = ".", checkpoint_manager: Any = None,
                 ) -> None:
        self.fabric = fabric
        self.policy_gate = policy_gate
        self.tool_runtime = tool_runtime
        self.memory_root = memory_root or os.path.join(
            ".forge", "agent-memory")
        self.project_root = project_root or "."
        self.checkpoint_manager = checkpoint_manager
        self._governor = _RunGovernor()
        self._tool_calls: dict[str, int] = {}
        self._tool_call_marks: dict[str, float] = {}
        self._tool_seqs: dict[str, int] = {}

    # -- identity / lifecycle gates -------------------------------------

    def _require_enabled(self, package: Any) -> str:
        name = _name_of(package)
        state = _state_of(package)
        if state != "enabled":
            raise MediationError(
                "NOT_ENABLED",
                "agent %r is %s; only enabled agents may run"
                % (name, state or "unknown"))
        return name

    @staticmethod
    def _refuse_self_approval(name: str, approver: str) -> None:
        if approver and _is_self_admin(name, approver):
            raise MediationError(
                "SELF_GRANT",
                "refused: %s cannot approve its own action" % approver)

    # -- memory (namespaced, bounded, isolated) --------------------------

    def _memory_store(self, name: str) -> Any:
        from forge.memory.store import MemoryStore

        root = os.path.join(self.memory_root, "agent-%s" % name)
        return MemoryStore(root=root)

    def write_memory(self, package: Any, key: str, value: str) -> str:
        """Write to the agent's own namespace. Cross-agent writes go
        through :meth:`write_agent_memory` and are refused."""
        name = _name_of(package)
        spec = _spec_of(package)
        policy = spec.get("memory_policy", {})
        if not isinstance(policy, dict):
            policy = {}
        if policy.get("retention", "session") == "none":
            raise MediationError("MEMORY_DISABLED",
                                 "agent %r retains no memory" % name)
        if not isinstance(key, str) or not key:
            raise MediationError("BAD_TOOL_ARGS", "memory needs a key")
        if not isinstance(value, str):
            raise MediationError("BAD_TOOL_ARGS",
                                 "memory values must be strings")
        store = self._memory_store(name)
        max_bytes = _spec_int(policy, "max_bytes_per_entry", 20480)
        if len(value.encode("utf-8")) > max_bytes:
            raise MediationError(
                "MEMORY_BOUND",
                "memory entry exceeds %d bytes" % max_bytes)
        max_entries = _spec_int(policy, "max_entries", 200)
        try:
            keys = store.list()
        except ValueError as exc:
            raise MediationError("MEMORY_BOUND", str(exc)) from exc
        if key not in keys and len(keys) >= max_entries:
            raise MediationError(
                "MEMORY_BOUND",
                "agent %r hit its memory entry limit (%d)"
                % (name, max_entries))
        try:
            store.save(key, value)
        except ValueError as exc:
            raise MediationError("MEMORY_BOUND", str(exc)) from exc
        return key

    def read_memory(self, package: Any, key: str) -> str | None:
        name = _name_of(package)
        try:
            return self._memory_store(name).load(key)
        except ValueError:
            return None

    def read_agent_memory(self, package: Any, target: str,
                          key: str) -> str | None:
        """One agent reading another's memory: always refused."""
        name = _name_of(package)
        if target != name:
            raise MediationError(
                "ISOLATION",
                "agent %r may not read agent %r's memory" % (name, target))
        return self.read_memory(package, key)

    def write_agent_memory(self, package: Any, target: str, key: str,
                           value: str) -> str:
        name = _name_of(package)
        if target != name:
            raise MediationError(
                "ISOLATION",
                "agent %r may not write agent %r's memory" % (name, target))
        return self.write_memory(package, key, value)

    # -- tools (allowlist + gate + budget) -------------------------------

    def execute_tool(self, package: Any, tool: str, run_id: str = "",
                     *, approver: str = "", approved: bool = False,
                     approval_token_id: str = "",
                     commit_guard: Any = None,
                     **kwargs: Any) -> dict[str, Any]:
        """Run one tool call through allowlist, grants, gate, runtime.

        ``approved`` is trusted-local-operator consent (the same flag
        the PolicyGate and ToolRuntime honor): remote API callers can
        never set it — API approval flows through ``approval_token_id``
        tokens, which the gate redeems. The gate and the runtime share
        one redemption chain per call, so a single-use token authorizes
        both layers instead of being consumed by the first.
        """
        name = self._require_enabled(package)
        self._refuse_self_approval(name, approver)
        lowered = (tool or "").lower()
        if any(fragment in lowered
               for fragment in FORBIDDEN_TOOL_FRAGMENTS):
            raise MediationError(
                "TOOL_DENIED",
                "refusing grant-shaped tool call %r: agents can never "
                "grant permissions" % (tool,))
        spec = _spec_of(package)
        allowed = spec.get("tools", [])
        if not isinstance(allowed, list):
            raise MediationError("BAD_SPEC",
                                 "agent %r has a corrupt tool allowlist"
                                 % (name,))
        if tool not in allowed:
            raise MediationError(
                "TOOL_DENIED",
                "agent %r may not use tool %r (allowlist: %s)"
                % (name, tool,
                   ", ".join(str(item) for item in allowed) or "none"))
        self._check_timeout(tool, kwargs)
        scope, risk = self._require_grant(package, name, tool, kwargs)
        limits = _limits_of(spec)
        budget = _spec_int(limits, "max_tool_calls_per_run", 50)
        counter = run_id or "direct:%s" % name
        used = self._tool_calls.get(counter, 0)
        if not run_id:
            # Direct (run-less) calls share one counter per agent; it
            # resets hourly so a debugging session cannot deny service
            # to future calls.
            if time.monotonic() - self._tool_call_marks.get(
                    counter, 0.0) > 3600.0:
                used = 0
            self._tool_call_marks[counter] = time.monotonic()
        if used >= budget:
            raise MediationError(
                "TOOL_BUDGET",
                "agent %r exceeded %d tool calls for this run"
                % (name, budget))
        seq = self._tool_seqs.get(counter, 0) + 1
        self._tool_seqs[counter] = seq
        chain_id = "%s:%d" % (counter, seq)
        gate_decision = "not-required"
        gate_reason = ""
        if tool in self.MUTATING_TOOLS and self.policy_gate is not None:
            operation = GATE_OPERATIONS.get(tool, tool)
            try:
                outcome = self.policy_gate.evaluate(
                    operation=operation, path=scope,
                    tool=tool, risk=risk, capability=tool,
                    agent=agent_identity(name), approved=approved,
                    approval_token_id=approval_token_id,
                    task_id=run_id, request_id=chain_id)
            except Exception as exc:
                raise MediationError(
                    "GATE_DENIED",
                    "policy gate errored on %s for agent %r: %s"
                    % (tool, name, exc)) from exc
            gate_decision = _decision_name(outcome)
            gate_reason = str(getattr(outcome, "reason", "") or "")[:300]
            if gate_decision != "ALLOW":
                raise MediationError(
                    "GATE_DENIED",
                    "policy gate refused %s for agent %r: %s"
                    % (tool, name,
                       getattr(outcome, "reason", gate_decision)))
        if tool in MEDIATED_TOOLS:
            self._tool_calls[counter] = used + 1
            mediated = self._execute_mediated_tool(package, tool, kwargs)
            mediated["gate"] = gate_decision
            if gate_reason:
                mediated["gate_reason"] = gate_reason
            return mediated
        if self.tool_runtime is None:
            raise MediationError(
                "NO_TOOL_RUNTIME",
                "no tool runtime is attached; refusing to execute %r "
                "outside one" % tool)
        self._tool_calls[counter] = used + 1
        try:
            result = self.tool_runtime.execute(
                tool, approved=approved, actor=agent_identity(name),
                task_id=run_id, approval_token_id=approval_token_id,
                risk=risk, request_id=chain_id,
                commit_guard=commit_guard, **kwargs)
        except Exception as exc:
            raise MediationError("TOOL_FAILED",
                                 "tool %r crashed: %s" % (tool, exc)
                                 ) from exc
        entry = {"tool": tool,
                 "success": bool(getattr(result, "success", False)),
                 "output": (getattr(result, "output", "") or "")[:2000],
                 "error": (getattr(result, "error", "") or "")[:500],
                 "gate": gate_decision}
        if gate_reason:
            entry["gate_reason"] = gate_reason
        return entry

    @staticmethod
    def _check_timeout(tool: str, kwargs: dict[str, Any]) -> None:
        if "timeout" not in kwargs or tool not in ("terminal", "run_tests"):
            return
        timeout = kwargs["timeout"]
        if isinstance(timeout, bool) or not isinstance(timeout, int) \
                or not TERMINAL_TIMEOUT_MIN <= timeout \
                <= TERMINAL_TIMEOUT_MAX:
            raise MediationError(
                "BAD_TOOL_ARGS",
                "timeout must be %d-%d seconds"
                % (TERMINAL_TIMEOUT_MIN, TERMINAL_TIMEOUT_MAX))

    def _require_grant(self, package: Any, name: str, tool: str,
                       kwargs: dict[str, Any]) -> tuple[str, str]:
        """Require a recorded grant covering this tool call.

        Returns the matched ``(scope, risk)``: the scope feeds the
        gate's path, the risk (highest matching grant wins) the gate's
        risk. Only the allowlist's known tools arrive here.
        """
        wanted = TOOL_GRANTS.get(tool)
        if wanted is None:
            raise MediationError(
                "TOOL_DENIED",
                "agent %r may not use tool %r" % (name, tool))
        resource, operation = wanted
        scope = self._grant_scope(tool, kwargs)
        matches: list[str] = []
        for grant in _grants_of(package):
            permission = grant.get("permission")
            if not isinstance(permission, dict):
                continue
            if str(permission.get("resource", "")).lower() != resource:
                continue
            if str(permission.get("operation", "")).lower() != operation:
                continue
            granted = permission.get("scope", "")
            if not isinstance(granted, str):
                continue
            if self._scope_covers(tool, granted, scope):
                matches.append(str(permission.get("risk", "MEDIUM")))
        if not matches:
            raise MediationError(
                "TOOL_DENIED",
                "agent %r has no grant covering %s/%s on %r"
                % (name, resource, operation, scope))
        risk = max(matches, key=lambda label: _RISK_RANK.get(
            str(label).upper(), _RISK_RANK["HIGH"]))
        return scope, str(risk)

    def _grant_scope(self, tool: str, kwargs: dict[str, Any]) -> str:
        """Derive the grant scope target from validated tool args."""
        if tool in ("read_file", "write_file", "delete_file"):
            path = kwargs.get("path", "")
            if not isinstance(path, str) or not path:
                raise MediationError("BAD_TOOL_ARGS",
                                     "%s needs a path argument" % tool)
            return path
        if tool in ("terminal", "run_tests"):
            command = kwargs.get("command", [])
            if not isinstance(command, list) or not command \
                    or any(not isinstance(part, str) or not part
                           for part in command):
                raise MediationError(
                    "BAD_TOOL_ARGS",
                    "%s needs a command list of non-empty strings" % tool)
            if tool == "run_tests":
                return "pytest"
            return command[0]
        if tool == "search":
            query = kwargs.get("query", "")
            if not isinstance(query, str) or not query:
                raise MediationError("BAD_TOOL_ARGS",
                                     "search needs a query argument")
            return ""
        if tool in MEDIATED_TOOLS:
            key = kwargs.get("key", "")
            if not isinstance(key, str) or not key:
                raise MediationError("BAD_TOOL_ARGS",
                                     "%s needs a key argument" % tool)
            return key
        return ""  # git_status: no target; needs a blank-scope grant.

    @staticmethod
    def _scope_covers(tool: str, granted: str, scope: str) -> bool:
        if tool == "search":
            # Queries are not paths: only broad read grants cover search.
            return granted in ("", "**")
        if tool == "run_tests":
            return granted == "pytest"
        if tool == "terminal":
            # Exact executable only — mirrors the approval store.
            return granted == scope
        if tool in ("read_file", "write_file", "delete_file"):
            return bool(_match_fs_pattern(granted, scope))
        # git / memory: exact scope, blank covers any target.
        if not granted:
            return True
        return granted == scope

    def _execute_mediated_tool(self, package: Any, tool: str,
                               kwargs: dict[str, Any]) -> dict[str, Any]:
        """Serve memory tools against the agent's own namespace."""
        key = str(kwargs.get("key", "") or "")
        if not key:
            raise MediationError("BAD_TOOL_ARGS",
                                 "%s needs a key argument" % tool)
        if tool == "memory_read":
            return {"tool": tool, "success": True,
                    "output": self.read_memory(package, key) or "",
                    "error": ""}
        value = str(kwargs.get("value", "") or "")
        self.write_memory(package, key, value)
        return {"tool": tool, "success": True, "output": key, "error": ""}

    @staticmethod
    def _checked_tool_calls(
            tool_calls: list[dict[str, Any]] | None
            ) -> list[tuple[str, dict[str, Any]]]:
        """Validate run tool calls into (tool, args) pairs."""
        if tool_calls is None:
            return []
        if not isinstance(tool_calls, list):
            raise MediationError("BAD_TOOL_ARGS",
                                 "tool_calls must be a list")
        checked: list[tuple[str, dict[str, Any]]] = []
        for call in tool_calls:
            if not isinstance(call, dict):
                raise MediationError("BAD_TOOL_ARGS",
                                     "tool calls must be objects")
            tool = call.get("tool", "")
            if not isinstance(tool, str) or not tool:
                raise MediationError("BAD_TOOL_ARGS",
                                     "tool calls need a tool name")
            args = call.get("args", {})
            if args is None:
                args = {}
            if not isinstance(args, dict):
                raise MediationError("BAD_TOOL_ARGS",
                                     "tool args must be an object")
            checked.append((tool, dict(args)))
        return checked

    # -- runs --------------------------------------------------------------

    def run(self, package: Any, requirement: str, *,
            actor: str = "", approver: str = "",
            tool_calls: list[dict[str, Any]] | None = None,
            approved: bool = False,
            approval_token_id: str = "",
            guard: Any = None) -> dict[str, Any]:
        """Run an enabled agent: fabric → tools → verification → memory.

        ``tool_calls`` (``{"tool": ..., "args": {...}}``) execute through
        :meth:`execute_tool` after the model responds; a checkpoint is
        captured before the first mutating call and failures roll back.
        Tests-required specs run the fixed project pytest suite through
        the constrained ``run_tests`` tool — callers supply no command.
        ``approved`` is trusted-local-operator consent only (see
        :meth:`execute_tool`); ``actor`` must name the run's requester.
        """
        name = self._require_enabled(package)
        actor = (actor or "").strip()
        if not actor:
            raise MediationError("BAD_ACTOR",
                                 "runs need a named actor")
        if _is_self_admin(name, actor):
            raise MediationError("SELF_RUN",
                                 "refused: %s cannot run itself" % actor)
        self._refuse_self_approval(name, approver)
        requirement = (requirement or "").strip()
        if not requirement or len(requirement) > 4000:
            raise MediationError("BAD_REQUIREMENT",
                                 "requirement must be 1-4000 characters")
        if self.fabric is None:
            raise MediationError(
                "NO_MODEL",
                "no model fabric is attached; agent %r refuses to "
                "fabricate output" % name)
        spec = _spec_of(package)
        limits = _limits_of(spec)
        calls = self._checked_tool_calls(tool_calls)
        self._governor.check(name, limits)
        run_id = uuid4().hex[:12]
        self._governor.begin(name)
        started = time.monotonic()
        checkpoint: Any = None
        checkpoint_id = ""
        changed: list[str] = []
        tool_results: list[dict[str, Any]] = []

        def _fail_rollback() -> None:
            # Restore tracked paths; with no declared paths (e.g. a bare
            # terminal call), restore every modified pre-existing file —
            # rollback never deletes unknown untracked files.
            self.rollback(checkpoint, changed or None)

        try:
            model_req = spec.get("model_requirements", {})
            if not isinstance(model_req, dict):
                model_req = {}
            capabilities = model_req.get("capabilities", ["coding"])
            if isinstance(capabilities, str):
                capabilities = [capabilities]
            if not isinstance(capabilities, list):
                raise MediationError(
                    "BAD_SPEC", "model capabilities must be a list")
            response = self._route_model(
                requirement, capabilities, model_req, name, run_id)
            output = (response.get("text", "") or "")[:4000]
            for tool, args in calls:
                if tool in self.MUTATING_TOOLS \
                        and self.checkpoint_manager is None:
                    raise MediationError(
                        "NO_CHECKPOINT",
                        "refusing mutating tool %r for agent %r without "
                        "a checkpoint manager" % (tool, name))
                if tool in self.MUTATING_TOOLS and checkpoint is None:
                    checkpoint = self.begin_mutation(package)
                    checkpoint_id = getattr(checkpoint, "id", "") or ""
                if tool in self.MUTATING_TOOLS and args.get("path"):
                    changed.append(str(args["path"]))
                try:
                    result = self.execute_tool(
                        package, tool, run_id, approver=approver,
                        approved=approved,
                        approval_token_id=approval_token_id,
                        commit_guard=guard, **args)
                except MediationError:
                    _fail_rollback()
                    raise
                tool_results.append(result)
                if not result.get("success"):
                    _fail_rollback()
                    raise MediationError(
                        "TOOL_FAILED",
                        "tool %r failed: %s"
                        % (tool, result.get("error") or "unknown"))
            verification = self._verify_output(package, output)
            if not verification["passed"]:
                _fail_rollback()
                raise MediationError("VERIFICATION_FAILED",
                                     verification["reason"])
            memory_key = ""
            policy = spec.get("memory_policy", {})
            if isinstance(policy, dict) \
                    and policy.get("retention", "session") != "none":
                memory_key = "runs/%s.txt" % run_id
                try:
                    self.write_memory(package, memory_key,
                                      "requirement: %s\noutput: %s"
                                      % (requirement[:500], output[:1500]))
                except MediationError:
                    memory_key = ""
            elapsed_ms = round((time.monotonic() - started) * 1000.0, 1)
            max_wall = _spec_float(limits, "max_wall_seconds", 600.0)
            if elapsed_ms / 1000.0 > max_wall:
                raise MediationError(
                    "WALL_CLOCK",
                    "agent %r exceeded its %ss wall clock"
                    % (name, max_wall))
            version = _version_of(package)
            fingerprint = spec_fingerprint(spec)
            return {"run_id": run_id, "agent": name, "success": True,
                    "actor": actor,
                    "output": output, "model": response.get("model", ""),
                    "provider": response.get("provider", ""),
                    "verification": verification,
                    "checkpoint_id": checkpoint_id,
                    "memory_key": memory_key,
                    "tools": tool_results,
                    "evidence": {
                        "lifecycle": "enabled",
                        "actor": actor,
                        "version": version,
                        "spec_hash": fingerprint,
                        "model": {"capability": (capabilities[0]
                                                if capabilities
                                                else "coding"),
                                  "model": response.get("model", ""),
                                  "provider": response.get("provider", "")},
                        "tools": {"allowlist": list(
                            spec.get("tools", [])),
                            "calls": len(tool_results)},
                        "gates": [{"tool": entry.get("tool"),
                                   "gate": entry.get("gate",
                                                    "not-required")}
                                  for entry in tool_results],
                        "checkpoint": {"id": checkpoint_id,
                                       "rolled_back": False},
                        "memory": {"namespace": "agent-%s" % name,
                                   "key": memory_key},
                    },
                    "elapsed_ms": elapsed_ms, "at": time.time()}
        finally:
            self._governor.end(name)
            self._tool_calls.pop(run_id, None)
            self._tool_seqs.pop(run_id, None)

    def begin_mutation(self, package: Any,
                       declared: list[str] | None = None) -> Any:
        """Capture a checkpoint before a mutating sequence. Returns the
        checkpoint (or None without a manager); callers pass it back to
        :meth:`rollback` on failure."""
        self._require_enabled(package)
        if self.checkpoint_manager is None:
            return None
        return self.checkpoint_manager.create(
            label="agent-%s" % _name_of(package),
            declared=list(declared or []))

    def rollback(self, checkpoint: Any,
                 changed: list[str] | None) -> bool:
        if checkpoint is None or self.checkpoint_manager is None:
            return False
        try:
            self.checkpoint_manager.rollback(
                checkpoint, None if changed is None else list(changed))
        except Exception:
            return False
        return True

    # -- internals ---------------------------------------------------------

    def _route_model(self, requirement: str, capabilities: list[str],
                     model_req: dict[str, Any], name: str,
                     run_id: str) -> dict[str, Any]:
        from forge.models.request import ModelRequest

        caps = [cap for cap in capabilities
                if isinstance(cap, str) and cap] or ["coding"]
        prefer_free = model_req.get("prefer_free")
        if prefer_free is not None and not isinstance(prefer_free, bool):
            raise MediationError("BAD_SPEC",
                                 "prefer_free must be a boolean")
        prefer_local = model_req.get("prefer_local")
        if prefer_local is not None and not isinstance(prefer_local, bool):
            raise MediationError("BAD_SPEC",
                                 "prefer_local must be a boolean")
        max_cost = model_req.get("max_cost_per_token")
        if max_cost is not None and (
                isinstance(max_cost, bool)
                or not isinstance(max_cost, (int, float))):
            raise MediationError("BAD_SPEC",
                                 "max_cost_per_token must be a number")
        max_latency = model_req.get("max_latency_ms")
        if max_latency is not None and (
                isinstance(max_latency, bool)
                or not isinstance(max_latency, (int, float))):
            raise MediationError("BAD_SPEC",
                                 "max_latency_ms must be a number")
        request = ModelRequest(
            prompt=requirement, capability=caps[0],
            required_capabilities=tuple(caps[1:]),
            task="agent:%s:%s" % (name, run_id),
            min_context_window=_spec_int(model_req, "min_context_window",
                                         0),
            prefer_free=prefer_free,
            prefer_local=prefer_local,
            max_cost_per_token=max_cost,
            max_latency_ms=max_latency)
        try:
            response = self.fabric.generate(request)
        except Exception as exc:
            raise MediationError("MODEL_FAILED",
                                 "model fabric errored: %s" % exc) from exc
        if not getattr(response, "success", False):
            raise MediationError("MODEL_FAILED",
                                 getattr(response, "error", "")
                                 or "model fabric failed the request")
        return {"text": getattr(response, "text", "")
                or getattr(response, "output", ""),
                "model": getattr(response, "model", "") or "",
                "provider": getattr(response, "provider", "") or ""}

    def _verify_output(self, package: Any, output: str) -> dict[str, Any]:
        spec = _spec_of(package)
        needs = spec.get("verification_requirements", {})
        if not isinstance(needs, dict):
            needs = {}
        gates: dict[str, Any] = {}
        if needs.get("require_security_scan", True):
            gates["security_scan"] = self._security_gate(output)
        else:
            gates["security_scan"] = {"ran": False, "passed": True,
                                      "reason": "not required"}
        if needs.get("require_review", True):
            gates["review"] = self._review_gate(output)
        else:
            gates["review"] = {"ran": False, "passed": True,
                               "reason": "not required"}
        if needs.get("require_tests", False):
            gates["tests"] = self._tests_gate()
        else:
            gates["tests"] = {"ran": False, "passed": True,
                              "reason": "not required"}
        failed = [name for name, gate in gates.items()
                  if not gate.get("passed")]
        detail = "; ".join(
            "%s (%s)" % (name, gates[name].get("reason", "failed"))
            for name in failed if gates[name].get("reason"))
        return {"passed": not failed, "gates": gates,
                "reason": ("gates failed: %s" % (detail or ", ".join(failed)))
                if failed else "all required gates passed"}

    def _security_gate(self, output: str) -> dict[str, Any]:
        # The exact secret/dangerous patterns the verification pipeline
        # enforces on files, judged here by code on the agent output.
        from forge.security.verification import VerificationPipeline

        # Report pattern indexes, never matched text: echoing a match
        # would leak the very secret the scan caught into run reports.
        hits: list[str] = []
        for index, pattern in enumerate(
                VerificationPipeline.SECRET_PATTERNS):
            if pattern.search(output):
                hits.append("secret-pattern#%d" % index)
        for index, pattern in enumerate(
                VerificationPipeline.DANGEROUS_PATTERNS):
            if pattern.search(output):
                hits.append("dangerous-pattern#%d" % index)
        if hits:
            return {"ran": True, "passed": False,
                    "reason": "output matches forbidden patterns: %s"
                    % ", ".join(sorted(set(hits)))}
        return {"ran": True, "passed": True, "reason": ""}

    def _review_gate(self, output: str) -> dict[str, Any]:
        from forge.security.review import ReviewGate

        if not output.strip():
            return {"ran": True, "passed": False,
                    "reason": "empty output"}
        try:
            decision = ReviewGate(
                root=self.project_root).review(diff=output)
        except Exception as exc:
            return {"ran": True, "passed": False,
                    "reason": "review error: %s" % exc}
        passed = decision.verdict.value == "APPROVE"
        return {"ran": True, "passed": passed,
                "reason": "" if passed else "review verdict: %s%s"
                % (decision.verdict.value,
                   ((" — %s" % decision.reason) if decision.reason
                    else ""))}

    def _tests_gate(self) -> dict[str, Any]:
        """Run the fixed project pytest suite via the run_tests tool.

        No caller-supplied command exists anywhere on this path: the
        harness always runs exactly ``python -m pytest -q -p
        no:cacheprovider`` through the constrained run_tests tool
        (which re-validates the shape), so a tests-required run can
        neither execute an arbitrary binary nor pretend tests ran.
        Without an attached tool runtime the gate fails honestly as
        TESTS_NOT_EXECUTED.
        """
        if self.tool_runtime is None:
            return {"ran": False, "passed": False,
                    "reason": "TESTS_NOT_EXECUTED: require_tests is set "
                    "but no tool runtime is attached"}
        try:
            result = self.tool_runtime.execute(
                "run_tests", command=list(TESTS_COMMAND),
                timeout=TESTS_TIMEOUT, approved=True,
                actor="forge-agent-harness")
        except Exception as exc:
            return {"ran": True, "passed": False,
                    "reason": "test run crashed: %s" % exc}
        if not getattr(result, "success", False):
            output = getattr(result, "output", "") or ""
            error = getattr(result, "error", "") or ""
            tail = (output + error)[-500:]
            return {"ran": True, "passed": False,
                    "reason": ("tests failed: %s" % tail) if tail
                    else "tests failed"}
        return {"ran": True, "passed": True, "reason": ""}
