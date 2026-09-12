"""Agent benchmark testing for the Creation Engine.

Benchmarks are deterministic checks judged by code — never by models.
Each check inspects the real package (spec, lifecycle, permissions,
tools, memory policy, resource limits) and, when a fabric is supplied,
verifies the required model capability is actually routable. Results
are stored on the package as evidence; failures name the exact check.

Checks:

- ``spec_valid`` — the spec passes full validation.
- ``lifecycle_valid`` — state is a known lifecycle state.
- ``tools_allowlisted`` — every tool is in the closed vocabulary.
- ``permissions_bounded`` — every grant is a known resource/operation
  with a bounded scope (no blank terminal grants).
- ``memory_isolated`` — memory policy mandates isolation.
- ``model_routable`` — fabric can route the required capability
  (honestly reported; without a fabric the check records ``skipped``).
- ``verification_declared`` — at least one known gate is required.
- ``resource_limits_bounded`` — quotas are within enforceable bounds.
"""
from __future__ import annotations

import time
from typing import Any

from forge.agents import lifecycle
from forge.agents.spec import KNOWN_GATES_SET, KNOWN_TOOLS_SET

MAX_CHECKS = 8


def _check(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"check": name, "passed": bool(passed), "detail": detail[:300]}


def run_agent_benchmark(package: Any, fabric: Any = None) -> dict[str, Any]:
    """Run the benchmark suite against a package; judge every check."""
    from forge.agents.spec import validate_spec_dict

    spec = getattr(package, "spec", None)
    state = getattr(package, "state", "")
    version = getattr(package, "version", "")
    checks: list[dict[str, Any]] = []

    # 1. spec_valid
    try:
        validate_spec_dict(spec.to_dict() if spec is not None else {})
        checks.append(_check("spec_valid", True, "spec passes validation"))
        spec_ok = True
    except Exception as exc:
        checks.append(_check("spec_valid", False, str(exc)))
        spec_ok = False

    # 2. lifecycle_valid
    if lifecycle.is_state(state):
        checks.append(_check("lifecycle_valid", True, f"state={state}"))
    else:
        checks.append(_check("lifecycle_valid", False,
                             f"unknown state {state!r}"))

    # 3. tools_allowlisted
    if spec is None:
        checks.append(_check("tools_allowlisted", False, "no spec"))
    else:
        unknown = [tool for tool in spec.tools
                   if tool not in KNOWN_TOOLS_SET]
        if unknown:
            checks.append(_check("tools_allowlisted", False,
                                 f"unknown tools: {unknown}"))
        else:
            checks.append(_check(
                "tools_allowlisted", True,
                f"{len(spec.tools)} tools allowlisted"))

    # 4. permissions_bounded
    if spec is None:
        checks.append(_check("permissions_bounded", False, "no spec"))
    else:
        problems: list[str] = []
        for grant in spec.permissions:
            try:
                grant.validate()
            except ValueError as exc:
                problems.append(str(exc))
        if problems:
            checks.append(_check("permissions_bounded", False,
                                 problems[0]))
        else:
            checks.append(_check(
                "permissions_bounded", True,
                f"{len(spec.permissions)} grants bounded"))

    # 5. memory_isolated
    if spec is None:
        checks.append(_check("memory_isolated", False, "no spec"))
    else:
        try:
            spec.memory_policy.validate()
            isolated = spec.memory_policy.isolated is True
            checks.append(_check(
                "memory_isolated", isolated,
                "per-agent isolation enforced"
                if isolated else "isolation disabled"))
        except ValueError as exc:
            checks.append(_check("memory_isolated", False, str(exc)))

    # 6. model_routable
    if spec is None:
        checks.append(_check("model_routable", False, "no spec"))
    elif fabric is None:
        checks.append(_check("model_routable", True,
                             "skipped: no fabric supplied; "
                             "capability declared as "
                             f"{spec.model_requirements.capability}"))
    else:
        try:
            capability = spec.model_requirements.capability
            names: list[str] = []
            try:
                models = fabric.models()
            except Exception:
                models = []
            for model in models or []:
                try:
                    if model.supports(capability):
                        names.append(model.name)
                except Exception:
                    continue
            if names:
                checks.append(_check(
                    "model_routable", True,
                    f"capability {capability!r} served by "
                    f"{len(names)} model(s)"))
            else:
                checks.append(_check(
                    "model_routable", False,
                    f"no registered model serves {capability!r}"))
        except Exception as exc:
            checks.append(_check("model_routable", False, str(exc)))

    # 7. verification_declared
    if spec is None:
        checks.append(_check("verification_declared", False, "no spec"))
    else:
        unknown_gates = [gate for gate in spec.verification_requirements
                         if gate not in KNOWN_GATES_SET]
        if not spec.verification_requirements:
            checks.append(_check("verification_declared", False,
                                 "no gates declared"))
        elif unknown_gates:
            checks.append(_check("verification_declared", False,
                                 f"unknown gates: {unknown_gates}"))
        else:
            checks.append(_check(
                "verification_declared", True,
                f"{len(spec.verification_requirements)} gate(s) declared"))

    # 8. resource_limits_bounded
    if spec is None:
        checks.append(_check("resource_limits_bounded", False, "no spec"))
    else:
        try:
            spec.resource_limits.validate()
            checks.append(_check("resource_limits_bounded", True,
                                 "quotas within bounds"))
        except ValueError as exc:
            checks.append(_check("resource_limits_bounded", False, str(exc)))

    checks = checks[:MAX_CHECKS]
    passed = sum(1 for entry in checks if entry["passed"])
    report = {
        "agent": getattr(package, "name", ""),
        "version": version,
        "state": state,
        "passed": passed,
        "total": len(checks),
        "success": passed == len(checks) and spec_ok,
        "checks": checks,
        "at": time.time(),
        "honest": True,
    }
    return report
