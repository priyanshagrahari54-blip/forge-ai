"""Mediated execution for created agents.

A created agent never touches the world directly. :class:`GatedAgentRuntime`
is the single choke point, and every run passes through the six Forge
subsystems:

* **Model Fabric** — the only route to a model, bounded by the spec's
  model requirements. With no fabric, runs fail honestly (``NO_MODEL``)
  instead of fabricating output.
* **PolicyGate** — every mutating tool call is authorized before execution.
* **Tool Runtime** — tools execute only through the runtime, only when the
  spec's allowlist names them, and only inside the per-run tool budget.
  ``memory_read``/``memory_write`` are served by the runtime itself
  against the agent's isolated namespace.
* **Memory** — namespaced per agent (``agent-<name>/``); cross-agent reads
  are refused, entries are bounded by the memory policy.
* **Verification** — output is scanned (secrets/dangerous patterns) and,
  per the verification requirements, reviewed and/or test-gated.
* **Checkpoints** — a checkpoint is captured before any mutating tool
  call; tool or verification failures roll the changed files back.

Isolation invariants (also covered by tests):

* non-``enabled`` packages are refused before any subsystem runs;
* an agent's approver may never be the agent itself (no self-grants);
* one agent can never read or write another agent's memory namespace;
* resource limits (runs/hour, concurrency, tool calls, wall clock) are
  enforced with no side effects on refusal.
"""
from __future__ import annotations

import os
import time
from typing import Any
from uuid import uuid4

from forge.agents.creation import agent_identity
from forge.agents.specs import MEDIATED_TOOLS


class MediationError(Exception):
    """A refused or failed mediated action, with a stable machine code."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


#: Tool calls that would grant power are never valid tools. They are
#: refused explicitly so the reason names the violation.
FORBIDDEN_TOOL_FRAGMENTS = ("grant", "permission", "approve", "escalat")


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


def _decision_name(outcome: Any) -> str:
    """Best-effort decision label from a gate outcome (real or fake)."""
    decision = getattr(outcome, "decision", outcome)
    value = getattr(decision, "value", decision)
    return str(value).upper()


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
        max_runs = int(limits.get("max_runs_per_hour", 60) or 60)
        if len(starts) >= max_runs:
            raise MediationError(
                "RATE_LIMITED",
                "agent %r hit its hourly run limit (%d)"
                % (name, max_runs))
        max_active = int(limits.get("max_concurrent", 2) or 2)
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
        if approver and approver == agent_identity(name):
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
        store = self._memory_store(name)
        max_bytes = int(policy.get("max_bytes_per_entry", 20480) or 20480)
        if len(value.encode("utf-8")) > max_bytes:
            raise MediationError(
                "MEMORY_BOUND",
                "memory entry exceeds %d bytes" % max_bytes)
        max_entries = int(policy.get("max_entries", 200) or 200)
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
                     **kwargs: Any) -> dict[str, Any]:
        """Run one tool call through the allowlist, gate, and runtime."""
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
        allowed = list(spec.get("tools", []))
        if tool not in allowed:
            raise MediationError(
                "TOOL_DENIED",
                "agent %r may not use tool %r (allowlist: %s)"
                % (name, tool, ", ".join(allowed) or "none"))
        limits = _limits_of(spec)
        budget = int(limits.get("max_tool_calls_per_run", 50) or 0)
        used = self._tool_calls.get(run_id or name, 0)
        if used >= budget:
            raise MediationError(
                "TOOL_BUDGET",
                "agent %r exceeded %d tool calls for this run"
                % (name, budget))
        gate_decision = "not-required"
        gate_reason = ""
        if tool in self.MUTATING_TOOLS and self.policy_gate is not None:
            outcome = self.policy_gate.evaluate(
                operation=tool, path=str(kwargs.get("path", "")),
                tool=tool, risk="MEDIUM", capability=tool,
                agent=agent_identity(name), approved=approved,
                approval_token_id=approval_token_id)
            gate_decision = _decision_name(outcome)
            gate_reason = str(getattr(outcome, "reason", "") or "")[:300]
            if gate_decision == "DENY" or (
                    gate_decision == "REQUIRE_APPROVAL"
                    and not approved and not approval_token_id):
                raise MediationError(
                    "GATE_DENIED",
                    "policy gate refused %s for agent %r: %s"
                    % (tool, name,
                       getattr(outcome, "reason", gate_decision)))
        if tool in MEDIATED_TOOLS:
            self._tool_calls[run_id or name] = used + 1
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
        self._tool_calls[run_id or name] = used + 1
        result = self.tool_runtime.execute(
            tool, approved=approved, actor=agent_identity(name),
            task_id=run_id, **kwargs)
        entry = {"tool": tool, "success": bool(result.success),
                 "output": (result.output or "")[:2000],
                 "error": (result.error or "")[:500],
                 "gate": gate_decision}
        if gate_reason:
            entry["gate_reason"] = gate_reason
        return entry

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

    # -- runs --------------------------------------------------------------

    def run(self, package: Any, requirement: str, *,
            actor: str = "", approver: str = "",
            test_command: str = "",
            tool_calls: list[dict[str, Any]] | None = None,
            approved: bool = False,
            approval_token_id: str = "") -> dict[str, Any]:
        """Run an enabled agent: fabric → tools → verification → memory.

        ``test_command`` satisfies ``require_tests`` when verification
        demands it; without one, a tests-required run fails honestly as
        ``TESTS_NOT_EXECUTED`` instead of pretending tests ran.
        ``tool_calls`` (``{"tool": ..., "args": {...}}``) execute through
        :meth:`execute_tool` after the model responds; a checkpoint is
        captured before the first mutating call and failures roll back.
        """
        name = self._require_enabled(package)
        self._refuse_self_approval(name, approver)
        requirement = (requirement or "").strip()
        if not requirement or len(requirement) > 4000:
            raise MediationError("BAD_REQUIREMENT",
                                 "requirement must be 1-4000 characters")
        spec = _spec_of(package)
        limits = _limits_of(spec)
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
            if self.fabric is None:
                raise MediationError(
                    "NO_MODEL",
                    "no model fabric is attached; agent %r refuses to "
                    "fabricate output" % name)
            model_req = spec.get("model_requirements", {})
            if not isinstance(model_req, dict):
                model_req = {}
            capabilities = list(model_req.get("capabilities", ["coding"]))
            response = self._route_model(
                requirement, capabilities, model_req, name, run_id)
            output = (response.get("text", "") or "")[:4000]
            for call in tool_calls or []:
                tool = str((call or {}).get("tool", ""))
                args = dict((call or {}).get("args", {}) or {})
                if tool in self.MUTATING_TOOLS and checkpoint is None:
                    checkpoint = self.begin_mutation(package)
                    checkpoint_id = getattr(checkpoint, "id", "") or ""
                if tool in self.MUTATING_TOOLS and args.get("path"):
                    changed.append(str(args["path"]))
                try:
                    result = self.execute_tool(
                        package, tool, run_id, approver=approver,
                        approved=approved,
                        approval_token_id=approval_token_id, **args)
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
            verification = self._verify_output(
                package, output, test_command=test_command)
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
            max_wall = float(limits.get("max_wall_seconds", 600.0)
                             or 600.0)
            if elapsed_ms / 1000.0 > max_wall:
                raise MediationError(
                    "WALL_CLOCK",
                    "agent %r exceeded its %ss wall clock"
                    % (name, max_wall))
            return {"run_id": run_id, "agent": name, "success": True,
                    "output": output, "model": response.get("model", ""),
                    "provider": response.get("provider", ""),
                    "verification": verification,
                    "checkpoint_id": checkpoint_id,
                    "memory_key": memory_key,
                    "tools": tool_results,
                    "evidence": {
                        "lifecycle": "enabled",
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

        caps = [cap for cap in capabilities if cap] or ["coding"]
        request = ModelRequest(
            prompt=requirement, capability=caps[0],
            required_capabilities=tuple(caps[1:]),
            task="agent:%s:%s" % (name, run_id),
            min_context_window=int(
                model_req.get("min_context_window", 0) or 0),
            prefer_free=(model_req.get("prefer_free")
                         if "prefer_free" in model_req else None),
            prefer_local=(model_req.get("prefer_local")
                          if "prefer_local" in model_req else None),
            max_cost_per_token=model_req.get("max_cost_per_token"),
            max_latency_ms=model_req.get("max_latency_ms"))
        response = self.fabric.generate(request)
        if not getattr(response, "success", False):
            raise MediationError("MODEL_FAILED",
                                 getattr(response, "error", "")
                                 or "model fabric failed the request")
        return {"text": getattr(response, "text", "")
                or getattr(response, "output", ""),
                "model": getattr(response, "model", "") or "",
                "provider": getattr(response, "provider", "") or ""}

    def _verify_output(self, package: Any, output: str, *,
                       test_command: str = "") -> dict[str, Any]:
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
            gates["tests"] = self._tests_gate(test_command)
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

        hits: list[str] = []
        for pattern in VerificationPipeline.SECRET_PATTERNS:
            found = pattern.search(output)
            if found:
                hits.append("secret:%s" % found.group(0)[:24])
        for pattern in VerificationPipeline.DANGEROUS_PATTERNS:
            found = pattern.search(output)
            if found:
                hits.append("dangerous:%s" % found.group(0)[:24])
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

    def _tests_gate(self, test_command: str) -> dict[str, Any]:
        if not (test_command or "").strip():
            return {"ran": False, "passed": False,
                    "reason": "TESTS_NOT_EXECUTED: require_tests is set "
                    "but no test command was supplied"}
        import subprocess

        try:
            completed = subprocess.run(
                (test_command or "").strip().split(), cwd=self.project_root,
                capture_output=True, text=True, timeout=120)
        except Exception as exc:
            return {"ran": True, "passed": False,
                    "reason": "test command failed to run: %s" % exc}
        passed = completed.returncode == 0
        tail = ((completed.stdout or "") + (completed.stderr or ""))[-500:]
        return {"ran": True, "passed": passed,
                "reason": "" if passed else "tests failed: %s" % tail}
