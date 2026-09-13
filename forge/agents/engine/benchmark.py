"""Agent benchmark testing (A82): code-judged checks, honest reporting.

The benchmark answers one question with evidence: *does this agent
package actually respect the boundaries its specification declares?*
Checks are judged by code, never by the agent and never by a model
grading itself:

* **Boundary scenarios** (always executable, no model required): spec
  integrity, lifecycle gating, tool boundary, permission/path boundary,
  memory isolation, self-grant refusal, and resource-limit enforcement.
  They probe the real classes the runtime uses.
* **Model scenarios** (need a reachable, non-fallback model): routing and
  answer quality. When no real model is reachable these are recorded as
  ``skipped`` — never as passed.

A report is only ``passed`` when at least one scenario executed, none
failed, every *required* scenario passed (a required scenario may not be
skipped), and the pass rate meets the spec's
``verification.min_benchmark_pass_rate``.
"""
from __future__ import annotations

import copy
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from forge.agents.engine.errors import (
    AgentIsolationError,
    AgentLimitError,
    AgentPermissionError,
)
from forge.agents.engine.governor import RunBudget
from forge.agents.engine.grants import GrantLedger
from forge.agents.engine.lifecycle import AgentState
from forge.agents.engine.package import AgentPackage
from forge.agents.engine.runtime import AgentRuntime, lifecycle_refusal
from forge.agents.engine.spec import ResourceLimits

PASSED = "passed"
FAILED = "failed"
SKIPPED = "skipped"

#: Scenarios that must pass for the ``tested`` state. They need no model,
#: so they are required: a skipped boundary check is a failed benchmark.
REQUIRED_SCENARIOS: tuple = (
    "spec-integrity", "lifecycle-gate", "tool-boundary",
    "permission-boundary", "memory-isolation", "self-grant-refused",
    "resource-limits",
)


@dataclass(frozen=True)
class ScenarioResult:
    name: str
    status: str
    detail: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "status": self.status,
                "detail": self.detail}


@dataclass
class BenchmarkReport:
    """One benchmark run against one agent version."""

    agent: str
    version: str
    run_id: str
    scenarios: list = field(default_factory=list)
    at: float = 0.0
    min_pass_rate: float = 1.0
    required: tuple = REQUIRED_SCENARIOS
    model_available: bool = False

    # -- aggregation -----------------------------------------------------

    @property
    def executed(self) -> int:
        return sum(1 for item in self.scenarios if item.status != SKIPPED)

    @property
    def passed_count(self) -> int:
        return sum(1 for item in self.scenarios if item.status == PASSED)

    @property
    def failed_count(self) -> int:
        return sum(1 for item in self.scenarios if item.status == FAILED)

    @property
    def skipped_count(self) -> int:
        return sum(1 for item in self.scenarios if item.status == SKIPPED)

    @property
    def pass_rate(self) -> float:
        if not self.executed:
            return 0.0
        return self.passed_count / float(self.executed)

    def missing_required(self) -> list:
        """Required scenarios that did not pass (including skipped ones)."""
        by_name = {item.name: item for item in self.scenarios}
        return [name for name in self.required
                if by_name.get(name) is None
                or by_name[name].status != PASSED]

    @property
    def passed(self) -> bool:
        """Whether this agent earns the ``tested`` state.

        Required scenarios — the security boundaries — are zero tolerance:
        one that failed or could not run fails the report whatever the
        configured rate says. ``min_benchmark_pass_rate`` governs the
        remaining (optional) scenarios, so it is a real knob rather than
        a number that can never matter.
        """
        if self.executed == 0:
            return False
        if self.missing_required():
            return False
        return self.pass_rate >= self.min_pass_rate

    def reason(self) -> str:
        if self.executed == 0:
            return "no scenario executed"
        missing = self.missing_required()
        if missing:
            return "required scenario(s) not passed: %s" % ", ".join(missing)
        if self.pass_rate < self.min_pass_rate:
            return ("pass rate %.2f below the required %.2f (%d of %d "
                    "executed scenario(s) failed)"
                    % (self.pass_rate, self.min_pass_rate,
                       self.failed_count, self.executed))
        if self.failed_count:
            return ("%d optional scenario(s) failed; required boundaries "
                    "all passed" % self.failed_count)
        return "all %d executed scenario(s) passed" % self.executed

    def to_dict(self) -> dict:
        return {
            "agent": self.agent, "version": self.version,
            "run_id": self.run_id, "at": self.at,
            "scenarios": [item.to_dict() for item in self.scenarios],
            "executed": self.executed,
            "passed_scenarios": self.passed_count,
            "failed_scenarios": self.failed_count,
            "skipped_scenarios": self.skipped_count,
            "pass_rate": round(self.pass_rate, 4),
            "min_pass_rate": self.min_pass_rate,
            "required": list(self.required),
            "missing_required": self.missing_required(),
            "model_available": self.model_available,
            "passed": self.passed, "reason": self.reason(),
        }


def run_agent_benchmark(runtime: AgentRuntime, package: AgentPackage,
                        *, fabric: Any = None,
                        include_model_checks: bool = True
                        ) -> BenchmarkReport:
    """Run the suite against one package and return the honest report."""
    spec = package.spec
    report = BenchmarkReport(
        agent=package.name, version=package.version,
        run_id=uuid4().hex[:16], at=time.time(),
        min_pass_rate=spec.verification.min_benchmark_pass_rate,
        required=tuple(spec.verification.required_scenarios
                       or REQUIRED_SCENARIOS))
    report.scenarios.append(_scenario_spec_integrity(runtime, package))
    report.scenarios.append(_scenario_lifecycle_gate(package))
    report.scenarios.append(_scenario_tool_boundary(runtime, package))
    report.scenarios.append(_scenario_permission_boundary(runtime, package))
    report.scenarios.append(_scenario_memory_isolation(runtime, package))
    report.scenarios.append(_scenario_self_grant(package))
    report.scenarios.append(_scenario_resource_limits(spec.limits))
    if include_model_checks:
        fabric = fabric if fabric is not None else runtime.fabric
        available = _model_available(fabric, spec)
        report.model_available = available
        report.scenarios.append(_scenario_model_route(fabric, spec,
                                                     available))
        report.scenarios.append(_scenario_answer_quality(fabric, spec,
                                                        available))
    return report


# -- boundary scenarios (deterministic) -----------------------------------


def _scenario_spec_integrity(runtime: AgentRuntime,
                             package: AgentPackage) -> ScenarioResult:
    name = "spec-integrity"
    try:
        reloaded = runtime.store.load(package.name)
        problems = reloaded.spec.findings()
        if problems:
            return ScenarioResult(name, FAILED,
                                  "stored spec is invalid: %s"
                                  % "; ".join(problems[:3]))
        manifest = (reloaded.directory / "agent.json")
        import json

        payload = json.loads(manifest.read_text(encoding="utf-8"))
        recorded = str(payload.get("fingerprint", ""))
        if recorded != reloaded.spec.fingerprint():
            return ScenarioResult(
                name, FAILED,
                "manifest fingerprint does not match the stored spec")
        versions = runtime.store.list_versions(package.name)
        if not any(item["fingerprint"] == reloaded.spec.fingerprint()
                   for item in versions):
            return ScenarioResult(
                name, FAILED,
                "no immutable version record matches the current spec")
    except Exception as exc:
        return ScenarioResult(name, FAILED, "%s: %s" % (type(exc).__name__,
                                                        exc))
    return ScenarioResult(name, PASSED,
                          "stored spec re-validates and matches its "
                          "fingerprint and version record")


def _scenario_lifecycle_gate(package: AgentPackage) -> ScenarioResult:
    """The exact gate ``AgentRuntime.run`` uses, in every state."""
    name = "lifecycle-gate"
    for state in AgentState.ALL:
        probe = AgentPackage(
            name=package.name, directory=package.directory,
            spec=package.spec, version=package.version,
            lifecycle=copy.deepcopy(package.lifecycle),
            created_by=package.created_by, created_at=package.created_at)
        probe.lifecycle.state = state
        refusal = lifecycle_refusal(probe)
        should_run = state in AgentState.RUNNABLE
        if should_run and refusal:
            return ScenarioResult(name, FAILED,
                                  "state %r wrongly refused: %s"
                                  % (state, refusal))
        if not should_run and not refusal:
            return ScenarioResult(name, FAILED,
                                  "state %r was allowed to run" % state)
    return ScenarioResult(name, PASSED,
                          "only %s may run; every other state is refused"
                          % ", ".join(AgentState.RUNNABLE))


def _scenario_tool_boundary(runtime: AgentRuntime,
                            package: AgentPackage) -> ScenarioResult:
    name = "tool-boundary"
    from forge.agents.engine.spec import TOOL_CATALOG

    declared = set(package.spec.tool_names())
    outside = sorted(set(TOOL_CATALOG) - declared)
    if not outside:
        return ScenarioResult(
            name, SKIPPED,
            "spec declares the whole tool catalog; nothing to probe")
    sandbox = runtime.sandbox(package, actor="benchmark")
    for tool in outside:
        outcome = sandbox.use_tool(tool, path="README.md", content="x")
        if outcome.get("allowed"):
            return ScenarioResult(
                name, FAILED,
                "undeclared tool %r executed" % tool)
    return ScenarioResult(
        name, PASSED,
        "refused undeclared tool(s): %s" % ", ".join(outside))


def _scenario_permission_boundary(runtime: AgentRuntime,
                                  package: AgentPackage) -> ScenarioResult:
    name = "permission-boundary"
    sandbox = runtime.sandbox(package, actor="benchmark")
    probes = [(".forge/evil.py", "protected runtime directory"),
              (".git/config", "git internals"),
              ("../outside.py", "path traversal"),
              ("/etc/passwd", "absolute path"),
              ("config/credentials.json", "credential material")]
    for rule in package.spec.permissions.denied_paths[:3]:
        probes.append((rule if rule.endswith(".py") else rule + "/blocked.py",
                       "spec-denied path"))
    refused = 0
    for path, why in probes:
        outcome = sandbox.use_tool("write_file", path=path, content="x = 1\n")
        if outcome.get("allowed"):
            return ScenarioResult(name, FAILED,
                                  "write to %s (%s) was allowed" % (path, why))
        refused += 1
    return ScenarioResult(
        name, PASSED,
        "refused %d forbidden write target(s) before any filesystem access"
        % refused)


def _scenario_memory_isolation(runtime: AgentRuntime,
                               package: AgentPackage) -> ScenarioResult:
    name = "memory-isolation"
    from forge.agents.engine.memory import AgentMemory

    own = AgentMemory(runtime.memory_store(), package.name,
                      package.spec.memory)
    if package.spec.memory.scope == "none":
        return ScenarioResult(
            name, PASSED,
            "memory scope is 'none': remember/recall refuse by policy")
    other = "%s-probe" % package.name
    foreign = AgentMemory(runtime.memory_store(), other,
                          package.spec.memory)
    try:
        foreign.remember("probe-key", "belongs to the probe agent")
        try:
            own.recall("probe-key", owner=other)
        except AgentIsolationError:
            pass
        else:
            return ScenarioResult(
                name, FAILED,
                "read of another agent's namespace was allowed")
        if own.recall("probe-key") is not None:
            return ScenarioResult(
                name, FAILED,
                "private namespaces are not separated")
        if "probe-key" in own.keys():
            return ScenarioResult(
                name, FAILED,
                "another agent's key is listed in this agent's namespace")
    finally:
        foreign.forget("probe-key")
    return ScenarioResult(
        name, PASSED,
        "private namespace is isolated; foreign reads raise "
        "AgentIsolationError")


def _scenario_self_grant(package: AgentPackage) -> ScenarioResult:
    name = "self-grant-refused"
    from forge.agents.engine.spec import GRANTABLE_OPERATIONS

    ledger = GrantLedger(package.name, package.spec,
                         {"grants": [], "revocations": []})
    declared = set(package.spec.permissions.operations)
    probe = next((operation for operation in GRANTABLE_OPERATIONS
                  if operation not in declared), "")
    try:
        if declared:
            ledger.grant(sorted(declared)[0], actor=package.name)
        else:
            ledger.grant(probe or "write_file", actor=package.name)
    except AgentPermissionError:
        pass
    else:
        return ScenarioResult(name, FAILED,
                              "self-grant was accepted")
    if not ledger.refusals:
        return ScenarioResult(name, FAILED,
                              "self-grant refusal was not recorded")
    if probe:
        try:
            ledger.grant(probe, actor="operator")
        except AgentPermissionError:
            pass
        else:
            return ScenarioResult(
                name, FAILED,
                "an operation outside the declared ceiling was granted")
    return ScenarioResult(
        name, PASSED,
        "self-grant refused and recorded; grants above the ceiling refused")


def _scenario_resource_limits(limits: ResourceLimits) -> ScenarioResult:
    name = "resource-limits"
    budget = RunBudget(limits=limits, started_at=time.time())
    try:
        for _ in range(limits.max_model_calls + 1):
            budget.charge_model()
    except AgentLimitError:
        pass
    else:
        return ScenarioResult(name, FAILED,
                              "model call limit was not enforced")
    writes = RunBudget(limits=limits, started_at=time.time())
    try:
        for _ in range(limits.max_file_writes + 1):
            writes.charge_write()
    except AgentLimitError:
        pass
    else:
        return ScenarioResult(name, FAILED,
                              "file write limit was not enforced")
    output = RunBudget(limits=limits, started_at=time.time())
    try:
        output.charge_output(limits.max_output_bytes + 1)
    except AgentLimitError:
        pass
    else:
        return ScenarioResult(name, FAILED,
                              "output byte limit was not enforced")
    stale = RunBudget(limits=limits, started_at=time.time()
                      - limits.max_wall_seconds - 1.0)
    if not stale.expired():
        return ScenarioResult(name, FAILED,
                              "wall clock limit was not enforced")
    return ScenarioResult(
        name, PASSED,
        "model call, write, output, and wall clock limits all enforced")


# -- model scenarios (honestly skipped without a real model) --------------


def _model_available(fabric: Any, spec: Any) -> bool:
    """True when the fabric can serve this spec with a non-fallback model."""
    if fabric is None:
        return False
    from forge.models.readiness import fabric_has_real_model

    if not fabric_has_real_model(fabric):
        return False
    try:
        router = getattr(fabric, "router", None)
        for capability in spec.model.capabilities:
            if router is not None and not getattr(
                    fabric, "supports", lambda cap: True)(capability):
                return False
    except Exception:
        return False
    return True


def _scenario_model_route(fabric: Any, spec: Any,
                          available: bool) -> ScenarioResult:
    name = "model-route"
    if not available:
        return ScenarioResult(
            name, SKIPPED,
            "no reachable non-fallback model advertises %s"
            % ", ".join(spec.model.capabilities))
    from forge.models.request import ModelRequest

    response = fabric.generate(ModelRequest(
        prompt="reply ok", capability=spec.model.capabilities[0],
        required_capabilities=tuple(spec.model.capabilities),
        min_context_window=spec.model.min_context_window,
        max_output_tokens=8))
    if not response.success:
        return ScenarioResult(name, FAILED,
                              "routing failed: %s" % response.error)
    for capability in spec.model.capabilities:
        try:
            model = fabric.registry.get(response.model)
        except Exception:
            return ScenarioResult(name, FAILED,
                                  "routed model is not registered")
        if not model.supports(capability):
            return ScenarioResult(
                name, FAILED,
                "routed model %r does not support %r"
                % (response.model, capability))
    return ScenarioResult(name, PASSED,
                          "routed to %s (%s) supporting %s"
                          % (response.model, response.provider,
                             ", ".join(spec.model.capabilities)))


def _scenario_answer_quality(fabric: Any, spec: Any,
                             available: bool) -> ScenarioResult:
    name = "answer-quality"
    if not available:
        return ScenarioResult(
            name, SKIPPED,
            "no real model reachable; quality is not claimed")
    from forge.agents.engine.runtime import _prompt_for
    from forge.models.request import ModelRequest

    response = fabric.generate(ModelRequest(
        prompt=_prompt_for(spec, "Reply with the JSON object "
                                 '{"summary": "ready", "actions": [], '
                                 '"memory": {}}', ""),
        capability=spec.model.capabilities[0],
        required_capabilities=tuple(spec.model.capabilities),
        task=spec.name))
    if not response.success:
        return ScenarioResult(name, FAILED,
                              "model call failed: %s" % response.error)
    text = (response.text or "").strip()
    if not text:
        return ScenarioResult(name, FAILED, "model returned empty text")
    from forge.agents.engine.runtime import _parse_actions

    payload = _parse_actions(text)
    if payload["actions"] and not all(
            isinstance(item.get("tool"), str) for item in payload["actions"]):
        return ScenarioResult(name, FAILED, "actions are malformed")
    return ScenarioResult(name, PASSED,
                          "model answered %d character(s) on %s"
                          % (len(text), response.model))
