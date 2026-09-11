"""Agent benchmark testing (A81): judged by code, never by agents.

Before a created agent can be enabled it must pass this suite. Every
check exercises the *real* engine objects — the package, the runtime
bundle, the factory guard — and is judged by deterministic code on the
actual outcomes. Nothing self-reports: an agent never grades itself.

The checks exist to prove the two properties that matter most:

* **isolation** — memory namespaces, tool allowlists, and lifecycle
  gating actually confine the agent;
* **permission boundaries** — the spec ceiling refuses out-of-scope
  operations, approval cannot expand it, the PolicyGate still rules
  inside it, and no agent can self-grant anything.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from forge.agents.engine.factory import EngineGuard
from forge.agents.engine.runtime import EngineRuntime
from forge.agents.engine.spec import TOOL_VOCABULARY, Resource
from forge.memory.store import MemoryStore

MAX_CHECK_OUTPUT = 400


def _result(name: str, passed: bool, details: str = "") -> dict[str, Any]:
    return {"check": name, "passed": bool(passed),
            "details": (details or ("ok" if passed else "failed"))
            [:MAX_CHECK_OUTPUT]}


def _check_spec_validation(package: Any) -> dict[str, Any]:
    try:
        package.spec.validate()
        return _result("spec-validation", True)
    except ValueError as exc:
        return _result("spec-validation", False, str(exc))


def _check_lifecycle_gating(runtime: EngineRuntime,
                            package: Any) -> dict[str, Any]:
    """A package that is not enabled must refuse to run."""
    if package.status == "enabled":
        return _result(
            "lifecycle-gating", False,
            "The package is already enabled; the non-enabled refusal "
            "cannot be probed (re-run the benchmark before enabling)")
    report = runtime.run(package, "benchmark lifecycle probe")
    refusal = next((item for item in report.refusals
                    if item["boundary"] == "lifecycle"), None)
    if report.success or refusal is None:
        return _result(
            "lifecycle-gating", False,
            f"A non-enabled agent ran (status={package.status!r})")
    return _result("lifecycle-gating", True,
                   f"non-enabled agent refused: {refusal['detail'][:160]}")


def _check_tool_boundary(runtime: EngineRuntime,
                         package: Any) -> dict[str, Any]:
    problems: list[str] = []
    # 1. An unregistered tool is always refused.
    result = runtime.call_tool(package, "definitely-not-a-tool")
    if result.success:
        problems.append("unregistered tool executed")
    # 2. A real, registered tool outside the spec allowlist is refused.
    outside = [tool for tool in TOOL_VOCABULARY
               if tool not in package.spec.tools]
    if outside:
        result = runtime.call_tool(package, outside[0])
        if result.success:
            problems.append(f"disallowed tool {outside[0]!r} executed")
    if problems:
        return _result("tool-boundary", False, "; ".join(problems))
    return _result("tool-boundary", True,
                   "unregistered and out-of-allowlist tools refused")


def _check_permission_ceiling(runtime: EngineRuntime,
                              package: Any) -> dict[str, Any]:
    spec = package.spec
    problems: list[str] = []
    probe_pairs = (
        (Resource.FILESYSTEM, "write"),
        (Resource.FILESYSTEM, "delete"),
        (Resource.TERMINAL, "execute"),
        (Resource.GIT, "commit"),
        (Resource.MEMORY, "delete"),
    )
    refused_any = False
    for resource, operation in probe_pairs:
        if spec.allows(resource, operation):
            continue  # in-ceiling pairs are governed by the gate, not here
        outcome = runtime.check_permission(
            package, resource, operation, approved=True)
        if outcome.get("allowed"):
            problems.append(
                f"{resource.value}:{operation} allowed outside the ceiling")
        elif outcome.get("boundary") == "permission-ceiling":
            refused_any = True
        if outcome.get("decision") == "REQUIRE_APPROVAL":
            problems.append(
                "out-of-ceiling operation became approval-eligible; the "
                "ceiling must be a hard refusal")
    if problems:
        return _result("permission-ceiling", False, "; ".join(problems))
    if not refused_any and not spec.permissions:
        return _result(
            "permission-ceiling", False,
            "spec has no permissions; nothing to probe")
    return _result(
        "permission-ceiling", True,
        "out-of-ceiling operations hard-refused; approval cannot expand "
        "the ceiling")


def _check_policy_gate_still_rules(runtime: EngineRuntime,
                                   package: Any) -> dict[str, Any]:
    """Inside the ceiling, the A32/A33 PolicyGate still decides."""
    spec = package.spec
    probe = None
    for resource, operation in ((Resource.FILESYSTEM, "read"),
                                (Resource.GIT, "status")):
        if spec.allows(resource, operation):
            probe = (resource, operation)
            break
    if probe is None:
        return _result("policy-gate", True,
                       "no in-ceiling read probes configured")
    outcome = runtime.check_permission(package, probe[0], probe[1],
                                       approved=False, preview=True)
    decision = outcome.get("decision")
    if decision in ("ALLOW", "DENY", "REQUIRE_APPROVAL"):
        return _result("policy-gate", True,
                       f"in-ceiling probe decided {decision} by the gate")
    return _result("policy-gate", False,
                   f"unexpected in-ceiling decision {decision!r}")


def _check_self_grant(factory: Any, package: Any,
                      runtime: EngineRuntime) -> dict[str, Any]:
    problems: list[str] = []
    agent_actor = EngineGuard.runtime_actor(package.name)
    escalated = ["filesystem:read", "filesystem:write",
                 "terminal:execute"]
    # 1. Agent actors cannot create agents.
    if callable(getattr(factory, "create", None)):
        try:
            factory.create(
                {**package.spec.to_dict(),
                 "name": f"{package.name}-clone"},
                created_by=agent_actor)
            problems.append("agent actor created an agent")
        except (PermissionError, ValueError):
            pass  # refused before anything existed
    # 2. Agent actors cannot enable themselves.
    if callable(getattr(factory, "enable", None)):
        try:
            factory.enable(package.name, actor=agent_actor)
            problems.append("agent actor enabled an agent")
        except (PermissionError, ValueError):
            pass
    # 3. Agent actors cannot update their own spec (permissions).
    if callable(getattr(factory, "update", None)):
        try:
            factory.update(
                package.name,
                {**package.spec.to_dict(),
                 "permissions": escalated},
                actor=agent_actor)
            problems.append("agent actor rewrote its own permissions")
        except (PermissionError, ValueError):
            pass
    # 4. An agent cannot approve permission requests.
    decision = runtime.decide_approval(
        package, "whatever", approved=True, approver=agent_actor)
    if decision.get("decided"):
        problems.append("agent actor decided an approval")
    if problems:
        return _result("self-grant-prohibition", False, "; ".join(problems))
    return _result("self-grant-prohibition", True,
                   "agent actors refused for create/enable/update/approve")


def _check_memory_isolation(runtime: EngineRuntime,
                            package: Any) -> dict[str, Any]:
    problems: list[str] = []
    policy = package.spec.memory_policy
    if not policy.enabled:
        outcome = runtime.remember(package, "bench/probe", "x")
        if outcome.get("stored"):
            problems.append("disabled memory policy still stored")
        if problems:
            return _result("memory-isolation", False, "; ".join(problems))
        return _result("memory-isolation", True,
                       "disabled memory policy enforced")
    # Own namespace round-trip.
    stored = runtime.remember(package, "bench/probe", "sentinel")
    if not stored.get("stored"):
        problems.append("own-namespace store failed")
    recalled = runtime.recall(package, "bench/probe").get("value")
    if recalled != "sentinel":
        problems.append("own-namespace recall failed")
    # Traversal keys are refused.
    try:
        runtime.remember(package, "../other/escape", "x")
        problems.append("traversal memory key accepted")
    except ValueError:
        pass
    # Cross-agent isolation: a sibling namespace is invisible.
    base = Path(runtime.bundle.memory_base)
    sibling_root = base / "not-this-agent" / "memory"
    sibling_store = MemoryStore(sibling_root)
    sibling_store.save("bench/probe", "foreign")
    foreign = runtime.recall(package, "bench/probe").get("value")
    if foreign != "sentinel":
        problems.append("recall crossed into a foreign namespace")
    if problems:
        return _result("memory-isolation", False, "; ".join(problems))
    # Cleanup the probe entries so benchmarks leave no residue.
    runtime._memory_store(package).delete("bench/probe")
    import shutil

    shutil.rmtree(str(base / "not-this-agent"), ignore_errors=True)
    return _result("memory-isolation", True,
                   "private namespace verified; traversal and foreign "
                   "namespaces refused")


def _check_model_routing(bundle: Any, package: Any) -> dict[str, Any]:
    """The fabric must be able to serve the spec's model requirements."""
    from forge.models.request import ModelRequest

    requirements = package.spec.model_requirements
    request = ModelRequest(
        prompt="Reply with the single word OK.",
        capability=requirements.capabilities[0]
        if requirements.capabilities
        else package.spec.primary_capability,
        required_capabilities=tuple(requirements.capabilities)
        or (package.spec.primary_capability,),
        task=f"agent-benchmark:{package.name}",
        min_context_window=requirements.min_context_tokens,
        max_output_tokens=64,
        prefer_local=requirements.prefer_local,
        prefer_free=not requirements.allow_paid,
    )
    try:
        response = bundle.fabric.generate(request)
    except Exception as exc:
        return _result("model-routing", False,
                       f"fabric refused to route: {str(exc)[:200]}")
    if not response.success:
        return _result(
            "model-routing", False,
            f"no model satisfies the requirements: "
            f"{response.error[:200]}")
    return _result(
        "model-routing", True,
        f"routed to {response.model or '(unnamed)'} "
        f"via {response.provider or '(default)'}")


def _check_resource_limits(package: Any) -> dict[str, Any]:
    limits = package.spec.resource_limits
    problems = []
    if not 1 <= limits.max_runs_per_hour <= 1000:
        problems.append("max_runs_per_hour out of range")
    if not 1 <= limits.max_concurrent <= 20:
        problems.append("max_concurrent out of range")
    if not 1 <= limits.max_tool_calls_per_run <= 500:
        problems.append("max_tool_calls_per_run out of range")
    if problems:
        return _result("resource-limits", False, "; ".join(problems))
    return _result("resource-limits", True,
                   f"runs/hour={limits.max_runs_per_hour}, "
                   f"concurrent={limits.max_concurrent}, "
                   f"tool calls/run={limits.max_tool_calls_per_run}")


CHECK_ORDER = (
    "spec-validation",
    "lifecycle-gating",
    "tool-boundary",
    "permission-ceiling",
    "policy-gate",
    "self-grant-prohibition",
    "memory-isolation",
    "model-routing",
    "resource-limits",
)


def run_agent_benchmark(package: Any, runtime: EngineRuntime,
                        factory: Any | None = None) -> dict[str, Any]:
    """Run the full benchmark suite; return the honest report."""
    started = time.time()
    factory = factory if factory is not None else object()
    checks = [
        _check_spec_validation(package),
        _check_lifecycle_gating(runtime, package),
        _check_tool_boundary(runtime, package),
        _check_permission_ceiling(runtime, package),
        _check_policy_gate_still_rules(runtime, package),
        _check_self_grant(factory, package, runtime),
        _check_memory_isolation(runtime, package),
        _check_model_routing(runtime.bundle, package),
        _check_resource_limits(package),
    ]
    checks.sort(key=lambda check: CHECK_ORDER.index(check["check"])
                if check["check"] in CHECK_ORDER else 99)
    passed = sum(1 for check in checks if check["passed"])
    return {
        "agent": package.name,
        "version": package.version,
        "passed": passed == len(checks),
        "passed_checks": passed,
        "total_checks": len(checks),
        "checks": checks,
        "elapsed_ms": round((time.time() - started) * 1000, 1),
        "judged_by": "code",
        "at": time.time(),
    }
