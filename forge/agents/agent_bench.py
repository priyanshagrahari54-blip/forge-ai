"""Benchmark testing for created agents.

Every check is judged by deterministic code — never by a model grading its
own work. Checks that need a subsystem the caller did not attach (model
fabric, mediated runtime) are reported as ``skipped``, never as passed, and
the score is computed over executed checks only.

Checks:

* ``spec-valid`` — the package spec re-validates from storage.
* ``lifecycle-gate`` — the mediated runtime refuses a non-enabled copy of
  the package (needs a runtime).
* ``isolation-memory`` — the agent writes its own namespace but cannot read
  a sibling namespace (needs a runtime).
* ``permission-boundary`` — an unlisted tool is refused and a self-granted
  approval is refused (needs a runtime).
* ``model-smoke`` — the fabric routes the spec's required capability and
  the response echoes a marker (needs a fabric).
"""
from __future__ import annotations

import tempfile
import time
from typing import Any

from forge.agents.specs import AgentSpec

MARKER = "FORGE-AGENT-BENCH-OK"


def _check(name: str, description: str, status: str,
           detail: str = "") -> dict[str, Any]:
    return {"name": name, "description": description, "status": status,
            "detail": detail[:500]}


def run_agent_benchmark(package: Any, *, fabric: Any = None,
                        runtime: Any = None) -> dict[str, Any]:
    """Benchmark one created agent package. ``package`` may be an
    :class:`AgentPackage` or its ``to_dict()`` form."""
    from forge.agents.mediation import GatedAgentRuntime, MediationError

    if isinstance(package, dict):
        name = str(package.get("name", ""))
        spec_dict = package.get("spec", {})
        state = str(package.get("state", ""))
    else:
        name = str(getattr(package, "name", ""))
        spec_dict = getattr(package, "spec", {}) or {}
        state = str(getattr(package, "state", ""))
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

    # 2-4. Runtime-mediated boundaries (need a runtime; build an
    # ephemeral one so benchmarks never touch real agent memory).
    mediated = runtime
    if mediated is None:
        tmp = tempfile.mkdtemp(prefix="forge-agent-bench-")
        mediated = GatedAgentRuntime(memory_root=tmp)
    probe = {"name": name, "spec": spec_dict, "state": state}

    disabled_probe = dict(probe)
    disabled_probe["state"] = "disabled" if state != "disabled" \
        else "created"
    try:
        mediated.run(disabled_probe, "benchmark probe")
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
        self_refused = False
        try:
            mediated.run(enabled_probe, "benchmark probe",
                         approver="agent:%s" % name)
        except MediationError as exc:
            self_refused = exc.code == "SELF_GRANT"
        if tool_refused and self_refused:
            checks.append(_check(
                "permission-boundary",
                "Unlisted tools and self-approval are refused", "passed"))
        else:
            checks.append(_check(
                "permission-boundary",
                "Unlisted tools and self-approval are refused", "failed",
                "tool_refused=%r self_refused=%r"
                % (tool_refused, self_refused)))
    except Exception as exc:
        checks.append(_check("permission-boundary",
                             "Unlisted tools and self-approval are refused",
                             "failed", str(exc)))

    # 5. Model smoke test (needs a fabric).
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
    verdict = bool(executed) and score >= minimum
    return {"agent": name, "at": time.time(), "checks": checks,
            "executed": len(executed), "passed_count": len(passed),
            "score": round(score, 4), "min_score": minimum,
            "passed": verdict}
