"""A81: agent benchmark testing and the first-party templates."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from forge.models.provider import ProviderRegistry  # noqa: E402
from forge.models.registry import Model, ModelRegistry  # noqa: E402
from forge.models.fabric import ModelFabric  # noqa: E402
from forge.models.capabilities import ALL_CAPABILITIES  # noqa: E402


def make_fabric(provider) -> ModelFabric:
    """A fabric whose single model advertises every capability."""
    return ModelFabric(
        registry=ModelRegistry([
            Model(name="m/a81", provider="p",
                  capabilities=tuple(ALL_CAPABILITIES),
                  free=True, local=True),
        ]),
        providers=ProviderRegistry({"p": provider}),
    )

from forge.agent_engine.benchmark import AgentBenchmark  # noqa: E402
from forge.agent_engine.engine import AgentCreationEngine  # noqa: E402
from forge.agent_engine.factory import AgentFactoryEngine  # noqa: E402
from forge.agent_engine.spec import AgentSpec, SpecError  # noqa: E402
from forge.agent_engine.templates import (  # noqa: E402
    TEMPLATE_NAMES,
    all_specs,
    from_template,
)
from forge.models.request import ModelResponse  # noqa: E402


class ReadyProvider:
    """Deterministic provider: behaves like a model, judged by code."""

    name = "ready"

    def generate(self, prompt, **kwargs):
        return ModelResponse(text="READY", model="m/a81", provider="p",
                             input_tokens=10, output_tokens=2)

    def is_available(self) -> bool:
        return True


class EmptyProvider(ReadyProvider):
    def generate(self, prompt, **kwargs):
        return ModelResponse(text="", model="m/a81", provider="p")


class LeakyProvider(ReadyProvider):
    def generate(self, prompt, **kwargs):
        return ModelResponse(text="api_key = sk-abcdefghij0123456789",
                             model="m/a81", provider="p")


def test_every_first_party_template_is_valid_and_benchmarks_clean():
    assert set(TEMPLATE_NAMES) == {"coding", "research", "security",
                                   "gamedev", "osdev", "documentation"}
    engine = AgentCreationEngine()
    for template in TEMPLATE_NAMES:
        package = engine.create(template=template)
        result = engine.validate(package.name)
        assert result["valid"], (template, result["findings"])
        report = engine.test(package.name, include_behavioural=False)
        assert report["passed"], (template, report["failures"])
        assert engine.enable(package.name)["state"] == "enabled"


def test_templates_are_conservative_by_default():
    for spec in all_specs():
        assert spec.permissions.require_approval_for_writes
        assert "security" in spec.verification.required_gates
        assert not spec.permissions.allow_terminal
        assert not spec.permissions.allow_git_commit
        assert spec.memory_policy.allow_secrets is False
        if spec.permissions.allow_network:
            assert spec.permissions.domains


def test_read_only_templates_declare_no_write_scope():
    for name in ("research", "security"):
        spec = from_template(name)
        assert spec.permissions.write_paths == ()
        assert spec.resource_limits.max_bytes_written == 0


def test_template_overrides_are_revalidated():
    spec = from_template("coding", {"name": "my-coder"})
    assert spec.name == "my-coder"
    with pytest.raises(SpecError):
        from_template("coding", {"tools": ["grant_permission"]})
    with pytest.raises(SpecError):
        from_template("nope")


def test_template_aliases_resolve():
    assert from_template("Coding Agent").template == "coding"
    assert from_template("game-development").template == "gamedev"
    assert from_template("docs").template == "documentation"


def test_benchmark_is_judged_by_code_not_by_the_agent():
    package = AgentFactoryEngine().build(from_template("documentation"))
    report = AgentBenchmark().run(package, include_behavioural=False)
    names = {check.name for check in report.checks}
    assert {"no-self-grant", "scope-isolation", "undeclared-tool-refused",
            "memory-rejects-secrets", "security-gate-declared"} <= names
    assert report.passed
    assert report.score == 1.0


def test_behavioural_checks_run_through_the_model_fabric():
    package = AgentFactoryEngine().build(from_template("documentation"))
    report = AgentBenchmark().run(package, fabric=make_fabric(
        ReadyProvider()))
    behavioural = [check for check in report.checks
                   if check.category == "behavioural"]
    assert len(behavioural) == 2
    assert report.passed


def test_a_silent_model_fails_the_benchmark_honestly():
    package = AgentFactoryEngine().build(from_template("documentation"))
    report = AgentBenchmark().run(package, fabric=make_fabric(
        EmptyProvider()))
    assert not report.passed
    assert any(check["name"] == "routes-a-request"
               for check in report.failures())


def test_a_leaking_model_fails_the_secret_check():
    package = AgentFactoryEngine().build(from_template("documentation"))
    report = AgentBenchmark().run(package, fabric=make_fabric(
        LeakyProvider()))
    assert not report.passed
    assert any(check["name"] == "response-secret-free"
               for check in report.failures())


def test_a_broken_package_fails_the_benchmark_and_cannot_be_enabled():
    engine = AgentCreationEngine()
    spec = AgentSpec.from_dict({
        "name": "sloppy-agent",
        "purpose": "Write everywhere with no approval.",
        "capabilities": ["coding"],
        "tools": ["write_file"],
        "permissions": {"write_paths": ["**"],
                        "require_approval_for_writes": False},
    })
    package = engine.create(spec)
    # Tamper with the built package the way a bad actor would.
    package.runtime["policy_gate"]["self_grant"] = True
    result = engine.validate(package.name)
    assert not result["valid"]
    assert any(f["code"] == "self_grant" for f in result["blocking"])
    report = engine.test(package.name, include_behavioural=False)
    assert not report.get("passed")


def test_benchmark_report_is_serializable_and_honest():
    package = AgentFactoryEngine().build(from_template("security"))
    report = AgentBenchmark().run(package, include_behavioural=False)
    payload = report.to_dict()
    assert payload["agent"] == "security-agent"
    assert payload["total"] == len(payload["checks"])
    assert payload["passed_count"] == sum(
        1 for check in payload["checks"] if check["passed"])
    assert payload["threshold"] == 1.0


def test_a_crashing_check_counts_as_a_failure():
    package = AgentFactoryEngine().build(from_template("documentation"))

    class Exploding:
        def validate(self, _package):
            raise RuntimeError("boom")

    report = AgentBenchmark(validator=Exploding()).run(
        package, include_behavioural=False)
    assert not report.passed
    failure = next(check for check in report.failures()
                   if check["name"] == "package-valid")
    assert "boom" in failure["details"]
