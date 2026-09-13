"""Benchmark testing for created agents.

Every check is judged by deterministic code — never by a model grading its
own work. Checks that need a subsystem the caller did not attach (model
fabric, mediated runtime) are reported as ``skipped``, never as passed, and
the score is computed over executed checks only.

Checks:

* ``spec-valid`` — the package spec re-validates from storage.
* ``tools-allowlisted`` — every stored tool is in the known tool set.
* ``permissions-bounded`` — every stored permission request re-validates
  against the A33 engine vocabulary.
* ``memory-isolated`` — the stored memory policy re-validates (isolation
  mandatory) and the namespace round-trips.
* ``verification-declared`` — the stored verification requirements
  re-validate.
* ``limits-bounded`` — the stored resource limits re-validate.
* ``lifecycle-gate`` — the mediated runtime refuses a non-enabled copy of
  the package (needs a runtime).
* ``isolation-memory`` — the agent writes its own namespace but cannot read
  a sibling namespace (needs a runtime).
* ``permission-boundary`` — an unlisted tool is refused, a grant-shaped
  tool is refused, and a self-granted approval is refused (needs a
  runtime).
* ``grant-enforcement`` — an allowlisted power tool with no recorded
  grant is refused (needs a runtime; skipped when the spec lists no
  power tool).
* ``model-smoke`` — the fabric routes the spec's required capability and
  the response echoes a marker (needs a fabric).

The boundary checks (``spec-valid``, ``lifecycle-gate``,
``isolation-memory``, ``permission-boundary``, ``grant-enforcement``)
are mandatory: the verdict requires all of them green no matter how low
the spec's ``min_benchmark_score`` goes. The report carries the
benchmarked spec's fingerprint so enablement can bind the result to the
current spec instead of a stale one.
"""
from __future__ import annotations

import shutil
import tempfile
import time
from typing import Any

from forge.agents.creation import spec_fingerprint
from forge.agents.specs import (
    AgentSpec,
    KNOWN_TOOLS,
    MemoryPolicy,
    PermissionRequestSpec,
    ResourceLimits,
    VerificationRequirements,
)

MARKER = "FORGE-AGENT-BENCH-OK"

#: Checks that must pass for any passing verdict, regardless of the
#: spec's min_benchmark_score. A low score bar can excuse weak
#: capabilities, never broken boundaries.
MANDATORY_CHECKS = frozenset({
    "spec-valid",
    "lifecycle-gate",
    "isolation-memory",
    "permission-boundary",
    "grant-enforcement",
})


def _denied_probe_args(tool: str) -> dict[str, Any]:
    """Plausible args so the grant probe reaches the grant check."""
    if tool in ("read_file", "delete_file"):
        return {"path": "bench-denied.txt"}
    if tool == "write_file":
        return {"path": "bench-denied.txt", "content": "bench"}
    if tool in ("terminal", "run_tests"):
        return {"command": ["bench-denied"]}
    if tool == "search":
        return {"query": "bench-denied"}
    if tool == "memory_read":
        return {"key": "bench-denied"}
    if tool == "memory_write":
        return {"key": "bench-denied", "value": "bench"}
    return {}


def _check(name: str, description: str, status: str,
           detail: str = "") -> dict[str, Any]:
    return {"name": name, "description": description, "status": status,
            "detail": detail[:500]}


def _stored_section(spec_dict: Any, section: str) -> Any:
    if isinstance(spec_dict, dict):
        return spec_dict.get(section)
    return None


def run_agent_benchmark(package: Any, *, fabric: Any = None,
                        runtime: Any = None) -> dict[str, Any]:
    """Benchmark one created agent package. ``package`` may be an
    :class:`AgentPackage` or its ``to_dict()`` form."""
    from forge.agents.mediation import (TOOL_GRANTS, GatedAgentRuntime,
                                          MediationError)

    if isinstance(package, dict):
        name = str(package.get("name", ""))
        spec_dict = package.get("spec", {})
        state = str(package.get("state", ""))
        version = str(package.get("version", ""))
    else:
        name = str(getattr(package, "name", ""))
        spec_dict = getattr(package, "spec", {}) or {}
        state = str(getattr(package, "state", ""))
        version = str(getattr(package, "version", "") or "")
    checks: list[dict[str, Any]] = []

    # 1. The stored spec re-validates.
    try:
        issues = AgentSpec.from_dict(spec_dict).validate()
    except ValueError as exc:
        issues = [str(exc)]
    checks.append(_check(
        "spec-valid", "Stored spec re-validates",
        "passed" if not issues else "failed",
        "" if not issues else "; ".join(issues)))

    # 2. Stored tools are all known.
    try:
        tools = _stored_section(spec_dict, "tools")
        if not isinstance(tools, list):
            raise ValueError("stored tools must be a list")
        unknown = [tool for tool in tools if tool not in KNOWN_TOOLS]
        checks.append(_check(
            "tools-allowlisted", "Stored tools are all known",
            "passed" if not unknown else "failed",
            "" if not unknown else "unknown tools: %s"
            % ", ".join(sorted(str(tool) for tool in unknown))))
    except ValueError as exc:
        checks.append(_check("tools-allowlisted",
                             "Stored tools are all known",
                             "failed", str(exc)))

    # 3. Stored permissions re-validate against the A33 vocabulary.
    try:
        permissions = _stored_section(spec_dict, "permissions")
        if not isinstance(permissions, list):
            raise ValueError("stored permissions must be a list")
        problems: list[str] = []
        for index, entry in enumerate(permissions):
            try:
                problems.extend(
                    "[%d] %s" % (index, issue)
                    for issue in PermissionRequestSpec.from_dict(
                        entry).validate())
            except ValueError as exc:
                problems.append("[%d] %s" % (index, exc))
        checks.append(_check(
            "permissions-bounded",
            "Stored permissions re-validate",
            "passed" if not problems else "failed",
            "" if not problems else "; ".join(problems)))
    except ValueError as exc:
        checks.append(_check("permissions-bounded",
                             "Stored permissions re-validate",
                             "failed", str(exc)))

    # 4. Stored memory policy re-validates (isolation mandatory).
    try:
        memory_issues = MemoryPolicy.from_dict(
            _stored_section(spec_dict, "memory_policy")).validate()
        checks.append(_check(
            "memory-isolated", "Stored memory policy re-validates",
            "passed" if not memory_issues else "failed",
            "" if not memory_issues else "; ".join(memory_issues)))
    except ValueError as exc:
        checks.append(_check("memory-isolated",
                             "Stored memory policy re-validates",
                             "failed", str(exc)))

    # 5. Stored verification requirements re-validate.
    try:
        verification_issues = VerificationRequirements.from_dict(
            _stored_section(spec_dict,
                            "verification_requirements")).validate()
        checks.append(_check(
            "verification-declared",
            "Stored verification requirements re-validate",
            "passed" if not verification_issues else "failed",
            "" if not verification_issues
            else "; ".join(verification_issues)))
    except ValueError as exc:
        checks.append(_check("verification-declared",
                             "Stored verification requirements re-validate",
                             "failed", str(exc)))

    # 6. Stored resource limits re-validate.
    try:
        limit_issues = ResourceLimits.from_dict(
            _stored_section(spec_dict, "resource_limits")).validate()
        checks.append(_check(
            "limits-bounded", "Stored resource limits re-validate",
            "passed" if not limit_issues else "failed",
            "" if not limit_issues else "; ".join(limit_issues)))
    except ValueError as exc:
        checks.append(_check("limits-bounded",
                             "Stored resource limits re-validate",
                             "failed", str(exc)))

    # 7-9. Runtime-mediated boundaries (need a runtime; build an
    # ephemeral one so benchmarks never touch real agent memory).
    mediated = runtime
    tmp_dir = ""
    if mediated is None:
        tmp_dir = tempfile.mkdtemp(prefix="forge-agent-bench-")
        mediated = GatedAgentRuntime(memory_root=tmp_dir)
    probe = {"name": name, "spec": spec_dict, "state": state}

    disabled_probe = dict(probe)
    disabled_probe["state"] = "disabled" if state != "disabled" \
        else "created"
    try:
        mediated.run(disabled_probe, "benchmark probe", actor="benchmark")
        checks.append(_check(
            "lifecycle-gate",
            "Runtime refuses non-enabled agents", "failed",
            "a %s package was allowed to run"
            % disabled_probe["state"]))
    except MediationError as exc:
        checks.append(_check(
            "lifecycle-gate", "Runtime refuses non-enabled agents",
            "passed" if exc.code == "NOT_ENABLED" else "failed",
            exc.code))
    except Exception as exc:  # fail honestly, never silently
        checks.append(_check("lifecycle-gate",
                             "Runtime refuses non-enabled agents",
                             "failed", str(exc)))

    try:
        try:
            mediated.write_memory(probe, "bench/probe.txt", MARKER)
            own = mediated.read_memory(probe, "bench/probe.txt")
            retained = own == MARKER
        except MediationError as exc:
            if exc.code != "MEMORY_DISABLED":
                raise
            # retention=none stores nothing; isolation then holds
            # trivially as long as cross-agent reads stay refused.
            retained = True
        try:
            mediated.read_agent_memory(probe, "some-other-agent",
                                       "bench/probe.txt")
            sibling_refused = False
        except MediationError as exc:
            sibling_refused = exc.code == "ISOLATION"
        if retained and sibling_refused:
            checks.append(_check(
                "isolation-memory",
                "Own memory works; sibling memory is refused", "passed"))
        else:
            checks.append(_check(
                "isolation-memory",
                "Own memory works; sibling memory is refused", "failed",
                "retained=%r sibling_refused=%r"
                % (retained, sibling_refused)))
    except Exception as exc:
        checks.append(_check("isolation-memory",
                             "Own memory works; sibling memory is refused",
                             "failed", str(exc)))

    try:
        enabled_probe = dict(probe)
        enabled_probe["state"] = "enabled"
        tool_refused = False
        try:
            mediated.execute_tool(enabled_probe, "definitely-not-a-tool",
                                  run_id="bench")
        except MediationError as exc:
            tool_refused = exc.code == "TOOL_DENIED"
        grant_refused = False
        try:
            mediated.execute_tool(enabled_probe, "grant_permissions",
                                  run_id="bench")
        except MediationError as exc:
            grant_refused = exc.code == "TOOL_DENIED"
        self_refused = False
        try:
            mediated.run(enabled_probe, "benchmark probe",
                         actor="benchmark", approver="agent:%s" % name)
        except MediationError as exc:
            self_refused = exc.code == "SELF_GRANT"
        if tool_refused and grant_refused and self_refused:
            checks.append(_check(
                "permission-boundary",
                "Unlisted/grant-shaped tools and self-approval are "
                "refused", "passed"))
        else:
            checks.append(_check(
                "permission-boundary",
                "Unlisted/grant-shaped tools and self-approval are "
                "refused", "failed",
                "tool_refused=%r grant_refused=%r self_refused=%r"
                % (tool_refused, grant_refused, self_refused)))
    except Exception as exc:
        checks.append(_check("permission-boundary",
                             "Unlisted/grant-shaped tools and self-approval "
                             "are refused",
                             "failed", str(exc)))

    # 10. Grant enforcement: power tools need recorded grants.
    try:
        granted_probe = dict(probe)
        granted_probe["state"] = "enabled"
        granted_probe["grants"] = []
        stored_tools = _stored_section(spec_dict, "tools")
        power = [tool for tool in stored_tools if tool in TOOL_GRANTS] \
            if isinstance(stored_tools, list) else []
        if not power:
            checks.append(_check(
                "grant-enforcement",
                "Ungranted power tools are refused", "skipped",
                "the spec lists no power tool"))
        else:
            try:
                mediated.execute_tool(
                    granted_probe, power[0], run_id="bench-grant",
                    **_denied_probe_args(power[0]))
                checks.append(_check(
                    "grant-enforcement",
                    "Ungranted power tools are refused", "failed",
                    "ungranted %r executed" % (power[0],)))
            except MediationError as exc:
                checks.append(_check(
                    "grant-enforcement",
                    "Ungranted power tools are refused",
                    "passed" if exc.code == "TOOL_DENIED" else "failed",
                    exc.code))
    except Exception as exc:
        checks.append(_check("grant-enforcement",
                             "Ungranted power tools are refused",
                             "failed", str(exc)))
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # 11. Model smoke test (needs a fabric).
    if fabric is None:
        checks.append(_check("model-smoke",
                             "Fabric routes the required capability and "
                             "echoes the marker", "skipped",
                             "no model fabric attached"))
    else:
        try:
            from forge.models.request import ModelRequest

            spec = AgentSpec.from_dict(spec_dict)
            capability = spec.model_requirements.capabilities[0] \
                if spec.model_requirements.capabilities else "coding"
            response = fabric.generate(ModelRequest(
                prompt="Reply with exactly the text %s and nothing else."
                % MARKER, capability=capability))
            text = (getattr(response, "text", "")
                    or getattr(response, "output", "") or "")
            if (getattr(response, "success", False)
                    and MARKER in text):
                checks.append(_check("model-smoke",
                                     "Fabric routes the required capability "
                                     "and echoes the marker", "passed"))
            else:
                checks.append(_check(
                    "model-smoke",
                    "Fabric routes the required capability and echoes the "
                    "marker", "failed",
                    "success=%r marker_present=%r"
                    % (getattr(response, "success", False),
                       MARKER in text)))
        except Exception as exc:
            checks.append(_check("model-smoke",
                                 "Fabric routes the required capability "
                                 "and echoes the marker", "failed",
                                 str(exc)))

    executed = [check for check in checks if check["status"] != "skipped"]
    passed = [check for check in executed if check["status"] == "passed"]
    score = (len(passed) / len(executed)) if executed else 0.0
    try:
        minimum = float(AgentSpec.from_dict(spec_dict)
                        .verification_requirements.min_benchmark_score)
    except ValueError:
        minimum = 1.0
    mandatory_failed = [check["name"] for check in checks
                        if check["name"] in MANDATORY_CHECKS
                        and check["status"] != "passed"]
    verdict = bool(executed) and score >= minimum \
        and not mandatory_failed
    fingerprint = spec_fingerprint(
        spec_dict if isinstance(spec_dict, dict) else {})
    return {"agent": name, "at": time.time(), "checks": checks,
            "executed": len(executed), "passed_count": len(passed),
            "score": round(score, 4), "min_score": minimum,
            "passed": verdict, "spec_hash": fingerprint,
            "version": version}
