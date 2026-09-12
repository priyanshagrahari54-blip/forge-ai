"""Managed agent execution through Forge's six subsystems.

Every managed-agent run is evidence-linked to:

- **Model Fabric** — the requirement is routed by the spec's
  ``model_requirements.capability``; the responding model/provider is
  recorded honestly.
- **PolicyGate** — any write tool call is evaluated through the A32
  :class:`PolicyGate` before the :class:`ToolRuntime` sees it; ``DENY``
  fails closed.
- **Tool Runtime** — tool calls execute only when the tool is in the
  spec's allowlist; unknown tools are refused without side effects.
- **Memory** — run records land in the agent's own namespace only
  (``AgentMemoryStore`` keyed by agent name); cross-agent access is
  never attempted.
- **Verification** — each declared verification gate runs through the
  :class:`VerificationPipeline` (or an injected verifier in tests) and
  its pass/fail is recorded.
- **Checkpoints** — a :class:`CheckpointManager` snapshot is taken
  before the first write tool executes, so writes are always
  rollback-capable.

The executor performs no permission grants itself. Grants are an
operator-only control-plane action; any grant-shaped tool call is
rejected as an unknown tool.
"""
from __future__ import annotations

import time
from typing import Any
from uuid import uuid4

from forge.agents import lifecycle

WRITE_TOOLS = frozenset({"write_file", "delete_file", "terminal", "git_commit"})

#: Tool calls that would grant power are never valid tools. They are
#: listed explicitly so the refusal reason names the violation.
FORBIDDEN_TOOL_FRAGMENTS = ("grant", "permission", "approve", "escalat")


class ManagedExecutionError(ValueError):
    """A managed run was refused or failed honestly."""


def _is_forbidden_tool(tool: str) -> bool:
    lowered = (tool or "").lower()
    return any(fragment in lowered for fragment in FORBIDDEN_TOOL_FRAGMENTS)


def execute_managed_agent(
    package: Any,
    requirement: str,
    *,
    fabric: Any = None,
    policy: Any = None,
    approval_store: Any = None,
    approval_token_id: str = "",
    policy_gate: Any = None,
    runtime: Any = None,
    tool_calls: list[dict[str, Any]] | None = None,
    approved: bool = False,
    memory_store: Any = None,
    verifier: Any = None,
    checkpoint_manager: Any = None,
    governor: Any = None,
    actor: str = "",
    task_id: str = "",
    run_id: str = "",
) -> dict[str, Any]:
    """Run one bounded managed-agent step with full subsystem evidence.

    Returns a result dict with ``success``, ``evidence`` (one entry per
    subsystem), and bounded ``output``/``error``. Raises
    :class:`ManagedExecutionError` for refusals (wrong lifecycle state,
    quota exhaustion, policy denial, allowlist violation).
    """
    from forge.security.policy import PermissionRequest, Resource
    from forge.security.policy_gate import PolicyDecision

    name = getattr(package, "name", "")
    spec = getattr(package, "spec", None)
    state = getattr(package, "state", "")
    if spec is None or not name:
        raise ManagedExecutionError("Cannot run: package has no spec")
    if state != lifecycle.ENABLED:
        raise ManagedExecutionError(
            f"Agent {name!r} is {state or 'unknown'}; only enabled "
            "agents can run")
    requirement = (requirement or "").strip()[:4000]
    if not requirement:
        raise ManagedExecutionError("Requirement must be 1-4000 characters")
    run_id = run_id or uuid4().hex[:12]
    task_id = task_id or f"managed-run-{run_id}"
    agent_id = f"forge-managed:{name}"
    evidence: dict[str, Any] = {
        "agent": name,
        "run_id": run_id,
        "task_id": task_id,
        "lifecycle": state,
    }

    # -- resource limits (governor) --------------------------------------
    if governor is not None:
        allowed, reason = governor.check(name)
        if not allowed:
            raise ManagedExecutionError(reason)
        governor.begin(name)
        evidence["governor"] = {"checked": True, "allowed": True}
        begun = True
    else:
        begun = False
        evidence["governor"] = {"checked": False,
                                "reason": "no governor supplied"}

    try:
        # -- permissions (A33 policy for AGENT/execute) -------------------
        if policy is not None:
            permission = PermissionRequest(
                agent=agent_id, resource=Resource.AGENT,
                operation="execute", scope=name, task_id=task_id,
                reason=f"managed agent run {name}")
            evaluation = policy.evaluate(permission)
            evidence["policy"] = {
                "decision": evaluation.decision.value,
                "reason": (evaluation.reason or "")[:300],
            }
            if evaluation.decision == PolicyDecision.REQUIRE_APPROVAL \
                    and approval_token_id and approval_store is not None:
                from forge.security.approvals import enforce_with_token

                allowed, token_reason = enforce_with_token(
                    approval_store, approval_token_id, permission)
                evidence["policy"]["token_redeemed"] = bool(allowed)
                evidence["policy"]["token_reason"] = token_reason[:200]
                if allowed:
                    evaluation = _allowed_evaluation(
                        evaluation, token_reason)
            if evaluation.decision == PolicyDecision.REQUIRE_APPROVAL:
                raise ManagedExecutionError(
                    "Approval required before this agent may run: "
                    f"{evaluation.reason or 'policy'}")
            if evaluation.decision != PolicyDecision.ALLOW:
                raise ManagedExecutionError(
                    f"Denied by policy: {evaluation.reason or 'denied'}")
        else:
            evidence["policy"] = {"decision": "SKIPPED",
                                  "reason": "no policy supplied"}

        # -- model fabric ------------------------------------------------
        if fabric is not None:
            from forge.models.request import ModelRequest

            capability = spec.model_requirements.capability
            request = ModelRequest(
                prompt=requirement, capability=capability,
                task=f"managed:{name}:{run_id}",
                prefer_local=spec.model_requirements.prefer_local,
                prefer_free=spec.model_requirements.prefer_free,
                max_output_tokens=512)
            response = fabric.generate(request)
            evidence["model"] = {
                "capability": capability,
                "model": getattr(response, "model", "") or "",
                "provider": getattr(response, "provider", "") or "",
                "success": bool(getattr(response, "success", False)),
                "error": (getattr(response, "error", "") or "")[:300],
                "latency_ms": round(
                    float(getattr(response, "latency_ms", 0.0) or 0.0), 1),
            }
            model_output = (getattr(response, "text", "") or "")[:2000]
            if not response.success:
                raise ManagedExecutionError(
                    f"Model Fabric could not serve {capability!r}: "
                    f"{getattr(response, 'error', '') or 'unknown error'}")
        else:
            evidence["model"] = {"success": False,
                                 "reason": "no fabric supplied"}
            model_output = ""

        # -- tool runtime (allowlisted) -----------------------------------
        calls = list(tool_calls or [])
        tool_results: list[dict[str, Any]] = []
        checkpoint_id = ""
        for call in calls:
            tool = str((call or {}).get("tool", ""))
            args = dict((call or {}).get("args", {}) or {})
            if _is_forbidden_tool(tool):
                raise ManagedExecutionError(
                    f"Refusing grant-shaped tool call {tool!r}: agents "
                    "can never grant permissions")
            if tool not in set(spec.tools):
                raise ManagedExecutionError(
                    f"Tool {tool!r} is not in agent {name!r}'s allowlist: "
                    f"{sorted(spec.tools)}")
            if runtime is None:
                raise ManagedExecutionError(
                    f"Tool {tool!r} requested but no ToolRuntime supplied")
            # Writes go through the PolicyGate first.
            if tool in WRITE_TOOLS and policy_gate is not None:
                outcome = policy_gate.evaluate(
                    operation=tool if tool in (
                        "write_file", "delete_file") else "write_file",
                    path=str(args.get("path", "agent-output.md")),
                    tool=tool, risk="LOW", capability="coding",
                    agent=agent_id, task_id=task_id,
                    approved=approved,
                    approval_token_id=approval_token_id)
                evidence.setdefault("policy_gate", []).append(outcome.to_dict())
                if outcome.decision == PolicyDecision.DENY or (
                        outcome.decision == PolicyDecision.REQUIRE_APPROVAL
                        and not approved and not approval_token_id):
                    raise ManagedExecutionError(
                        f"PolicyGate refused {tool}: {outcome.reason}")
            # Checkpoint before the first write.
            if tool in WRITE_TOOLS and checkpoint_manager is not None \
                    and not checkpoint_id:
                checkpoint = checkpoint_manager.create(
                    label=f"managed-{name}-{run_id}")
                checkpoint_id = checkpoint.id
                evidence["checkpoint"] = {
                    "id": checkpoint_id,
                    "before_write": tool,
                }
            result = runtime.execute(
                tool, approved=approved, actor=agent_id, task_id=task_id,
                approval_token_id=approval_token_id, **args)
            tool_results.append({
                "tool": tool,
                "success": bool(result.success),
                "output": (result.output or "")[:1000],
                "error": (result.error or "")[:500],
            })
            if not result.success:
                raise ManagedExecutionError(
                    f"Tool {tool!r} failed: {result.error or 'unknown'}")
        evidence["tools"] = {
            "allowlist": sorted(spec.tools),
            "calls": tool_results,
        }
        if "checkpoint" not in evidence:
            evidence["checkpoint"] = {
                "id": "",
                "note": ("no write tools executed; no snapshot needed"
                         if checkpoint_manager is not None else
                         "no checkpoint manager supplied"),
            }

        # -- memory (agent-namespaced only) -------------------------------
        if memory_store is not None:
            key = f"runs/{run_id}"
            value = (f"requirement={requirement[:200]} "
                     f"model={evidence['model'].get('model', '')} "
                     f"tools={len(tool_results)}")[:spec.memory_policy.max_value_chars]
            # Memory writes are bounded by the agent's own policy.
            entries = memory_store.list(name)
            if len(entries) >= spec.memory_policy.max_entries:
                raise ManagedExecutionError(
                    f"Agent {name!r} memory is full "
                    f"({spec.memory_policy.max_entries} entries)")
            memory_store.set(name, key, value)
            evidence["memory"] = {
                "namespace": name,
                "key": key,
                "isolated": True,
            }
        else:
            evidence["memory"] = {"isolated": True,
                                  "reason": "no memory store supplied"}

        # -- verification --------------------------------------------------
        if verifier is not None:
            gate_results: list[dict[str, Any]] = []
            for gate in spec.verification_requirements:
                method = getattr(verifier, gate, None)
                if not callable(method):
                    gate_results.append({"gate": gate, "passed": False,
                                         "details": "unknown gate"})
                    continue
                started = time.monotonic()
                try:
                    result = method()
                    passed = bool(getattr(result, "passed", False))
                    details = str(getattr(result, "details", ""))[:500]
                except Exception as exc:
                    passed, details = False, str(exc)[:500]
                gate_results.append({
                    "gate": gate, "passed": passed, "details": details,
                    "elapsed_ms": round(
                        (time.monotonic() - started) * 1000.0, 1),
                })
            evidence["verification"] = {
                "gates": gate_results,
                "passed": all(entry["passed"] for entry in gate_results),
            }
            failures = [entry["gate"] for entry in gate_results
                        if not entry["passed"]]
            if failures:
                raise ManagedExecutionError(
                    f"Verification failed: {', '.join(failures)}")
        else:
            evidence["verification"] = {
                "gates": [],
                "reason": "no verifier supplied",
            }

        return {
            "success": True,
            "agent": name,
            "run_id": run_id,
            "output": model_output[:2000],
            "evidence": evidence,
        }
    finally:
        if begun and governor is not None:
            try:
                governor.end(name)
            except Exception:
                pass


def _allowed_evaluation(evaluation: Any, reason: str) -> Any:
    from forge.security.policy_gate import PolicyDecision
    from forge.security.policy import PermissionEvaluation

    return PermissionEvaluation(
        decision=PolicyDecision.ALLOW, reason=reason,
        risk=evaluation.risk, scope=evaluation.scope,
        request_id=evaluation.request_id)
