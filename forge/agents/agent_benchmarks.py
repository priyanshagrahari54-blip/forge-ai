"""Agent benchmark testing (Forge Agent Creation Engine).

Benchmarks are judged by deterministic code, never by models. Every
package runs the structural suite — manifest completeness, bounded
permissions/tools, lifecycle validity, isolation envelope, policy
enforcement, and resource limits. When a Model Fabric is supplied,
live routing checks verify the agent's required capabilities really
route; without a fabric those checks report ``skipped``, never pass
by assumption.
"""
from __future__ import annotations

import time
from typing import Any

from forge.agents.lifecycle import normalize
from forge.agents.spec import (BLOCKED_OPERATIONS, KNOWN_PERMISSIONS,
                               KNOWN_TOOLS)
from forge.models.capabilities import is_capability

MAX_CHECKS = 12


def _check(name: str, description: str, passed: bool,
           detail: str = "") -> dict[str, Any]:
    return {"check": name, "description": description,
            "passed": bool(passed), "detail": detail[:300]}


def structural_checks(package: Any) -> list[dict[str, Any]]:
    """Deterministic checks over the package itself (no network)."""
    spec = package.spec
    results: list[dict[str, Any]] = []

    manifest = package.manifest()
    wiring = manifest.get("wiring", {})
    complete = all(wiring.get(key) for key in (
        "model_fabric", "policy_gate", "tool_runtime", "memory",
        "verification", "checkpoints"))
    results.append(_check(
        "manifest-complete",
        "Package wires Model Fabric, PolicyGate, Tool Runtime, Memory, "
        "Verification, and Checkpoints",
        bool(complete),
        "" if complete else "wiring envelope is incomplete"))

    caps_ok = bool(spec.capabilities) and all(
        is_capability(cap) for cap in spec.capabilities)
    results.append(_check(
        "capabilities-bounded",
        "Capabilities come from the canonical vocabulary",
        caps_ok,
        "" if caps_ok else "capability outside the vocabulary"))

    tools_ok = bool(spec.tools) and all(
        tool in KNOWN_TOOLS for tool in spec.tools)
    results.append(_check(
        "tools-bounded",
        "Tools are known Tool Runtime names only",
        tools_ok,
        "" if tools_ok else "unknown tool requested"))

    blocked = [perm for perm in spec.permissions
               if perm in BLOCKED_OPERATIONS]
    unknown = [perm for perm in spec.permissions
               if perm not in KNOWN_PERMISSIONS]
    perms_ok = bool(spec.permissions) and not blocked and not unknown
    detail = ""
    if blocked:
        detail = "blocked operation(s): %s" % ", ".join(blocked)
    elif unknown:
        detail = "unknown permission(s): %s" % ", ".join(unknown)
    elif not spec.permissions:
        detail = "no permissions listed"
    results.append(_check(
        "permissions-bounded",
        "Permissions are allowlisted; blocked operations never granted",
        perms_ok, detail))

    try:
        normalize(package.lifecycle)
        lifecycle_ok = True
        lifecycle_detail = ""
    except ValueError as exc:
        lifecycle_ok = False
        lifecycle_detail = str(exc)[:300]
    results.append(_check(
        "lifecycle-valid",
        "Lifecycle state is one of the seven legal states",
        lifecycle_ok, lifecycle_detail))

    namespace = wiring.get("memory", "")
    isolated = isinstance(namespace, str) and namespace == (
        "namespaced:%s" % spec.name)
    results.append(_check(
        "isolation-namespace",
        "Memory namespace is scoped to this agent alone",
        isolated,
        "" if isolated else "memory namespace is not agent-scoped"))

    limits = spec.resource_limits
    limits_ok = (
        1 <= limits.max_runs_per_hour <= 1000
        and 1 <= limits.max_concurrent <= 20
        and 1 <= limits.max_seconds_per_run <= 3600
        and 1 <= limits.max_output_chars <= 200000
        and 1 <= limits.max_files_per_run <= 500)
    results.append(_check(
        "resource-limits-present",
        "Resource limits are present and within enforceable bounds",
        limits_ok,
        "" if limits_ok else "resource limits out of bounds"))

    model_caps = spec.model_requirements.capabilities
    model_ok = bool(model_caps) and all(
        is_capability(cap) for cap in model_caps)
    results.append(_check(
        "model-requirements-routable",
        "Model requirements name real, routable capabilities",
        model_ok,
        "" if model_ok else "model capability outside the vocabulary"))

    if package.real and not package.executor:
        binding_ok = False
        binding_detail = "real=True without a bound executor"
    else:
        binding_ok = True
        binding_detail = ""
    results.append(_check(
        "binding-honest",
        "real=True only with a bound executor",
        binding_ok, binding_detail))

    return results[:MAX_CHECKS]


def live_checks(package: Any, fabric: Any) -> list[dict[str, Any]]:
    """Route each required model capability through a real fabric."""
    from forge.models.request import ModelRequest

    spec = package.spec
    results: list[dict[str, Any]] = []
    for capability in spec.model_requirements.capabilities[:4]:
        request = ModelRequest(
            prompt="Reply with exactly the text FORGE-AGENT-OK and "
                   "nothing else.",
            capability=capability, task="agent-benchmark",
            prefer_local=spec.model_requirements.prefer_local,
            prefer_free=spec.model_requirements.prefer_free,
            max_output_tokens=64)
        started = time.time()
        try:
            response = fabric.generate(request)
        except Exception as exc:
            results.append(_check(
                "live-%s" % capability,
                "Capability %s routes through the Model Fabric"
                % capability,
                False, "fabric error: %s" % (exc,)))
            continue
        latency = round((time.time() - started) * 1000.0, 1)
        passed = bool(response.success
                      and "FORGE-AGENT-OK" in (response.text or ""))
        detail = ("model=%s provider=%s latency=%sms"
                  % (response.model or "-",
                     response.provider or "-",
                     latency)) if passed else (
            response.error or "response missing marker")[:300]
        entry = _check(
            "live-%s" % capability,
            "Capability %s routes through the Model Fabric"
            % capability,
            passed, detail)
        entry["latency_ms"] = latency
        entry["model"] = response.model or ""
        entry["provider"] = response.provider or ""
        results.append(entry)
    return results


def run_agent_benchmark(package: Any, fabric: Any = None, *,
                        live: bool = False
                        ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run the agent benchmark suite; judge everything by code.

    Structural checks always run. Live routing checks run only when
    ``live=True`` with a fabric supplied; otherwise they are recorded
    as ``skipped`` — never passed by assumption.
    """
    started = time.time()
    checks = structural_checks(package)
    live_results: list[dict[str, Any]] = []
    skipped: list[str] = []
    if live and fabric is not None:
        try:
            live_results = live_checks(package, fabric)
        except Exception as exc:
            live_results = [_check(
                "live-routing", "Live routing checks",
                False, "fabric error: %s" % (exc,))]
    else:
        skipped = ["live-%s" % cap for cap in
                   package.spec.model_requirements.capabilities[:4]]
    checks = (checks + live_results)[:MAX_CHECKS]
    passed = sum(1 for entry in checks if entry["passed"])
    total = len(checks)
    summary = {
        "agent": package.spec.name,
        "version": package.version,
        "passed": passed,
        "total": total,
        "pass_rate": (passed / total) if total else 0.0,
        "skipped": skipped,
        "elapsed_ms": round((time.time() - started) * 1000.0, 1),
        "honest": True,
    }
    return checks, summary


def build_test_report(package: Any, fabric: Any = None, *,
                      live: bool = False) -> dict[str, Any]:
    """Run benchmarks and shape the report stored on the package."""
    checks, summary = run_agent_benchmark(package, fabric, live=live)
    required = float(
        package.spec.verification_requirements
        .min_benchmark_pass_rate)
    summary["required_pass_rate"] = required
    summary["meets_requirement"] = bool(
        summary["pass_rate"] >= required)
    return {"checks": checks, "summary": summary,
            "recorded_at": time.time()}
