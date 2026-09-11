"""Agent benchmark testing (A81).

Benchmarks are deterministic, offline, code-judged scenarios that
exercise a *package*, not a model: every scenario drives the
:class:`AgentRuntime` through its declared surface and checks real
observable behavior. Nothing grades itself — each check is plain code
inspecting outcomes. Model quality is deliberately out of scope here
(the runtime's Model Fabric requests are checked for capability
correctness against a recorded stand-in fabric, never for answer
quality), so benchmark runs never need a live model and never fabricate
model output.

A benchmark result gates the lifecycle: the manager moves an agent to
``TESTED`` only when the spec's benchmark passed at the spec's minimum
score.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from forge.agent_engine.errors import AgentEngineError

BENCHMARK_IDS: tuple[str, ...] = (
    "generic", "coding", "research", "security",
    "game-dev", "os-dev", "documentation",
)


@dataclass(frozen=True)
class BenchmarkScenario:
    id: str
    description: str
    check: Callable[["AgentRuntime", Any], tuple[bool, str]]


@dataclass
class BenchmarkResult:
    benchmark: str
    agent: str
    version: int
    total: int = 0
    passed: int = 0
    failed: int = 0
    scenarios: list[dict[str, Any]] = field(default_factory=list)
    duration_ms: float = 0.0
    started_at: str = ""

    @property
    def score(self) -> float:
        return (self.passed / self.total) if self.total else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "benchmark": self.benchmark,
            "agent": self.agent,
            "version": self.version,
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "score": round(self.score, 4),
            "duration_ms": round(self.duration_ms, 2),
            "started_at": self.started_at,
            "scenarios": self.scenarios,
        }


# ---------------------------------------------------------------------------
# Scenario checks
# ---------------------------------------------------------------------------

def _recorded_fabric() -> Any:
    """A deterministic stand-in fabric that records routed requests.

    It answers every request honestly as a recording: the response text
    states it is a benchmark stand-in. Capability requirements are
    recorded so scenarios can assert routing correctness without a live
    model.
    """

    class _BenchmarkFabric:
        def __init__(self) -> None:
            self.requests: list[dict[str, Any]] = []

        def generate(self, request) -> Any:
            self.requests.append({
                "capability": getattr(request, "capability", ""),
                "required_capabilities": tuple(getattr(
                    request, "required_capabilities", ())),
                "min_context_window": getattr(request,
                                              "min_context_window", 0),
                "prompt": getattr(request, "prompt", ""),
            })

            class _Response:
                success = True
                model = "benchmark-standin"
                provider = "benchmark"
                text = "benchmark stand-in response (no live model)"
                error = ""

            return _Response()

    return _BenchmarkFabric()


def _scenario_spec_integrity() -> BenchmarkScenario:
    def check(runtime: Any, _ctx: Any) -> tuple[bool, str]:
        spec = runtime.manifest.spec
        roundtrip = spec.__class__.from_dict(spec.to_dict())
        if roundtrip.fingerprint() != spec.fingerprint():
            return False, "spec round-trip changed its fingerprint"
        if runtime.manifest.permission_digest != spec.permission_digest():
            return False, "manifest permission digest != spec digest"
        if sorted(runtime.manifest.permissions) != sorted(spec.permissions):
            return False, "manifest permissions != spec permissions"
        return True, "spec round-trips; digests and permissions agree"

    return BenchmarkScenario("spec-integrity",
                             "Spec round-trips and package digests agree.",
                             check)


def _scenario_memory_scope() -> BenchmarkScenario:
    def check(runtime: Any, _ctx: Any) -> tuple[bool, str]:
        if not runtime.spec.memory.enabled:
            return True, "memory disabled by policy; scenario skipped"

        def stored_files() -> set[str]:
            root = runtime._memory.root
            return {path.relative_to(root).as_posix()
                    for path in root.rglob("*") if path.is_file()}

        before = stored_files()
        probe_key = f"probe-{time.time_ns()}"  # unique per run
        runtime.remember(probe_key, "value-1")
        if runtime.recall(probe_key) != "value-1":
            return False, "remember/recall round-trip failed"
        if runtime.recall(f"never-written-{time.time_ns()}") is not None:
            return False, "recall invented an entry"
        # Confinement: everything this runtime wrote must live under the
        # engine-owned namespace for this exact agent. Other agents'
        # namespaces in the shared store root are their own business.
        written = stored_files() - before
        if not written:
            return False, "remember wrote nothing to the store"
        namespace = f"agents/{runtime.name}/"
        leaked = [path for path in written
                  if not path.startswith(namespace)]
        if leaked:
            return False, f"memory leaked outside the agent namespace: " \
                          f"{', '.join(sorted(leaked))}"
        return True, "memory confined to the agent's own namespace"

    return BenchmarkScenario("memory-scope",
                             "Memory stays inside the agent's namespace.",
                             check)


def _scenario_tool_boundary(undeclared: str) -> BenchmarkScenario:
    def check(runtime: Any, _ctx: Any) -> tuple[bool, str]:
        if undeclared in runtime.spec.tools:
            return True, "tool is declared; boundary not applicable"
        result = runtime.use_tool(undeclared, path="src/anything.py")
        if getattr(result, "success", False):
            return False, f"undeclared tool {undeclared!r} executed"
        if "not granted" not in str(getattr(result, "error", "")):
            return False, "denial did not name the isolation boundary"
        return True, f"undeclared tool {undeclared!r} refused"

    return BenchmarkScenario(
        "tool-boundary",
        f"Tools outside the spec ({undeclared!r}) do not exist for the agent.",
        check)


def _scenario_permission_boundary() -> BenchmarkScenario:
    def check(runtime: Any, ctx: Any) -> tuple[bool, str]:
        # A granted write tool still goes through PolicyGate: a sibling
        # runtime with NO operator approver must refuse the write —
        # approval is something only the operator can supply.
        write_tool = next(
            (tool for tool in runtime.spec.tools
             if tool in ("write_file", "delete_file", "run_command")),
            None)
        if write_tool is None:
            return True, "no write tools declared; boundary not applicable"
        from forge.agent_engine.runtime import AgentRuntime

        unapproved = AgentRuntime(
            runtime.manifest, workspace=ctx, enabled=True, approver=None)
        working_dirs = runtime.spec.limits.working_dirs
        probe_dir = (working_dirs[0] if working_dirs else "src")
        probe_path = f"{probe_dir}/probe.py"
        kwargs = ({"path": probe_path, "content": "x = 1\n"}
                  if write_tool == "write_file"
                  else {"path": probe_path})
        result = unapproved.use_tool(write_tool, **kwargs)
        if getattr(result, "success", False):
            return False, "a write executed without operator approval"
        reason = str(getattr(result, "error", ""))
        if "approval" not in reason.lower():
            return False, f"refusal did not mention approval: {reason}"
        return True, f"{write_tool} requires operator approval"

    return BenchmarkScenario(
        "permission-boundary",
        "Writes never self-approve; PolicyGate approval is required.",
        check)


def _scenario_model_routing() -> BenchmarkScenario:
    def check(runtime: Any, _ctx: Any) -> tuple[bool, str]:
        response = runtime.call_model("benchmark probe")
        if not response.get("success"):
            return False, f"fabric request failed: {response.get('error')}"
        fabric = runtime._fabric
        if fabric is None or not getattr(fabric, "requests", None):
            return False, "no fabric request was recorded"
        requested = fabric.requests[-1]
        required = requested.get("required_capabilities") or ()
        if required:
            missing = [cap for cap in required
                       if cap not in set(runtime.spec.model.capabilities or ())
                       | set(runtime.spec.capabilities)]
            if missing:
                return False, f"requested capabilities outside the spec: " \
                              f"{missing}"
        return True, "model request routed through the fabric with the " \
                     "spec's requirements"

    return BenchmarkScenario(
        "model-routing",
        "Model requests route through the Model Fabric with the spec's "
        "capability requirements.",
        check)


def _scenario_checkpoint_rollback() -> BenchmarkScenario:
    def check(runtime: Any, ctx: Any) -> tuple[bool, str]:
        if "write_file" not in runtime.spec.tools:
            return True, "write_file not declared; scenario skipped"
        target = ctx / "src" / "rollback-probe.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("before\n", encoding="utf-8")
        checkpoint = runtime.checkpoint("benchmark-probe",
                                        declared=["src/rollback-probe.txt"])
        target.write_text("after\n", encoding="utf-8")
        runtime.rollback(checkpoint, changed_files=["src/rollback-probe.txt"])
        restored = target.read_text(encoding="utf-8")
        if restored != "before\n":
            return False, f"rollback left {restored!r} instead of original"
        return True, "checkpoint captured and restored the original bytes"

    return BenchmarkScenario(
        "checkpoint-rollback",
        "Checkpoints restore the exact pre-change worktree.",
        check)


def _scenario_resource_accounting() -> BenchmarkScenario:
    def check(runtime: Any, _ctx: Any) -> tuple[bool, str]:
        state = runtime.state()
        if state["requests"] < 1:
            return False, "model requests were not accounted"
        if state["tokens_used"] < 1:
            return False, "tokens were not accounted"
        limits = state["limits"]
        if limits["max_requests"] < 1 or limits["max_tokens_per_request"] < 1:
            return False, "limits are not bounded"
        return True, "resource accounting is live and bounded"

    return BenchmarkScenario(
        "resource-accounting",
        "Requests, tokens, and limits are accounted.",
        check)


# ---------------------------------------------------------------------------
# Template-specific scenarios
# ---------------------------------------------------------------------------

def _scenario_layout(workdir: str, label: str) -> BenchmarkScenario:
    def check(runtime: Any, ctx: Any) -> tuple[bool, str]:
        if "write_file" not in runtime.spec.tools:
            return True, "write_file not declared; scenario skipped"
        target = f"{workdir}/layout-probe.txt"
        result = runtime.use_tool("write_file", path=target,
                                  content="layout ok\n")
        if not getattr(result, "success", False):
            return False, f"write into {workdir!r} refused: " \
                          f"{getattr(result, 'error', '')}"
        written = ctx / workdir / "layout-probe.txt"
        if not written.exists():
            return False, "file was not actually written"
        return True, f"writes land inside {workdir!r}"

    return BenchmarkScenario(f"{label}-layout",
                             f"Writes are confined to {workdir!r}.", check)


def _scenario_secret_guard() -> BenchmarkScenario:
    def check(runtime: Any, _ctx: Any) -> tuple[bool, str]:
        if "write_file" not in runtime.spec.tools:
            return True, "write_file not declared; scenario skipped"
        result = runtime.use_tool("write_file", path=".env",
                                  content="SECRET=value\n")
        if getattr(result, "success", False):
            return False, "a write to .env was allowed (protected path)"
        return True, "protected paths are denied even to write tools"

    return BenchmarkScenario("secret-guard",
                             "Credential paths are protected from every agent.",
                             check)


def _scenario_command_boundary() -> BenchmarkScenario:
    def check(runtime: Any, ctx: Any) -> tuple[bool, str]:
        if "run_command" not in runtime.spec.tools:
            return True, "run_command not declared; scenario skipped"
        # A sibling runtime with no operator approver must refuse the
        # command; approval is something only the operator can supply.
        from forge.agent_engine.runtime import AgentRuntime

        unapproved = AgentRuntime(
            runtime.manifest, workspace=ctx, enabled=True, approver=None)
        result = unapproved.use_tool("run_command",
                                     command=["echo", "probe"])
        if getattr(result, "success", False):
            return False, "a command ran without operator approval"
        reason = str(getattr(result, "error", ""))
        if "approval" not in reason.lower():
            return False, f"refusal did not mention approval: {reason}"
        return True, "commands require operator approval"

    return BenchmarkScenario(
        "command-boundary",
        "Shell commands never execute without operator approval.",
        check)


def _scenario_notes_memory() -> BenchmarkScenario:
    def check(runtime: Any, _ctx: Any) -> tuple[bool, str]:
        runtime.remember("notes/probe", "researched")
        if runtime.recall("notes/probe") != "researched":
            return False, "note did not round-trip through memory"
        return True, "research notes persist in the agent's namespace"

    return BenchmarkScenario("notes-memory",
                             "Notes round-trip through agent memory.", check)


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

class AgentBenchmarkHarness:
    """Builds and runs the benchmark for a spec's benchmark id."""

    def build(self, benchmark_id: str):
        scenarios: list[BenchmarkScenario] = [
            _scenario_spec_integrity(),
            _scenario_memory_scope(),
            _scenario_tool_boundary("run_command"),
            _scenario_tool_boundary("git_push"),
            _scenario_permission_boundary(),
            _scenario_model_routing(),
            _scenario_checkpoint_rollback(),
            _scenario_resource_accounting(),
        ]
        template_scenarios: dict[str, list[BenchmarkScenario]] = {
            "coding": [_scenario_layout("src", "code")],
            "research": [_scenario_notes_memory()],
            "security": [_scenario_secret_guard()],
            "game-dev": [_scenario_layout("assets", "assets"),
                         _scenario_layout("scenes", "scenes")],
            "os-dev": [_scenario_layout("scripts", "scripts"),
                       _scenario_command_boundary()],
            "documentation": [_scenario_layout("docs", "docs")],
        }
        scenarios.extend(template_scenarios.get(benchmark_id, []))
        return scenarios

    def run(self, benchmark_id: str, runtime: "AgentRuntime",
            workspace: Any) -> BenchmarkResult:
        from forge.agent_engine.runtime import AgentRuntime  # type check aid

        if benchmark_id not in BENCHMARK_IDS:
            raise AgentEngineError(f"unknown benchmark: {benchmark_id!r}")
        scenarios = self.build(benchmark_id)
        fabric = _recorded_fabric()
        runtime._fabric = fabric
        result = BenchmarkResult(
            benchmark=benchmark_id, agent=runtime.name,
            version=runtime.version, total=len(scenarios),
            started_at=datetime.now(timezone.utc).isoformat())
        started = time.perf_counter()
        for scenario in scenarios:
            try:
                passed, detail = scenario.check(runtime, workspace)
            except Exception as exc:  # a crashing scenario is a failure
                passed, detail = False, f"scenario raised: {exc}"
            result.scenarios.append({
                "id": scenario.id,
                "description": scenario.description,
                "passed": bool(passed),
                "detail": detail,
            })
            if passed:
                result.passed += 1
            else:
                result.failed += 1
        result.duration_ms = (time.perf_counter() - started) * 1000.0
        return result

    def meets_requirements(self, result: BenchmarkResult,
                           spec_verification: Any) -> tuple[bool, str]:
        """Did the run satisfy the spec's verification requirements?"""
        if result.failed:
            details = "; ".join(
                f"{scenario['id']}: {scenario['detail']}"
                for scenario in result.scenarios if not scenario["passed"])
            return False, f"failed scenarios: {details}"
        if result.score < spec_verification.min_score:
            return False, (
                f"score {result.score:.2f} below the required "
                f"{spec_verification.min_score:.2f}")
        if result.passed < spec_verification.min_scenarios:
            return False, (
                f"only {result.passed} scenarios passed; "
                f"{spec_verification.min_scenarios} required")
        return True, "benchmark requirements satisfied"


DEFAULT_HARNESS = AgentBenchmarkHarness()


def run_benchmark(benchmark_id: str, runtime: "AgentRuntime",
                  workspace: Any) -> BenchmarkResult:
    return DEFAULT_HARNESS.run(benchmark_id, runtime, workspace)
