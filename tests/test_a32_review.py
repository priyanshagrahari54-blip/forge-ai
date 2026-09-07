"""Independent review gate tests (A32.8)."""
import json

from forge.agents.reviewer import ReviewerAgent
from forge.models.fabric import ModelFabric
from forge.models.provider import ModelResult, ProviderRegistry
from forge.models.registry import Model, ModelRegistry
from forge.security.review import FindingSeverity, ReviewFinding, ReviewGate, ReviewVerdict


def _gate(tmp_path):
    return ReviewGate(tmp_path)


def test_review_approves_clean_material(tmp_path):
    decision = _gate(tmp_path).review(diff="+def ok(): return 1\n", changed_files=["app.py"])
    assert decision.verdict == ReviewVerdict.APPROVE
    assert decision.approved


def test_review_blocks_conflict_marker(tmp_path):
    decision = _gate(tmp_path).review(diff="<<<<<<< HEAD\n", changed_files=["app.py"])
    assert decision.verdict == ReviewVerdict.BLOCK
    assert any(f.severity == FindingSeverity.CRITICAL for f in decision.findings)


def test_review_blocks_dynamic_execution(tmp_path):
    decision = _gate(tmp_path).review(diff="+value = eval('1')\n", changed_files=["app.py"])
    assert decision.verdict == ReviewVerdict.BLOCK
    assert any("dynamic code execution" in f.message for f in decision.findings)


def test_review_blocks_incomplete_implementation(tmp_path):
    decision = _gate(tmp_path).review(diff="+def health():\n+    pass\n", changed_files=["app.py"])
    assert decision.verdict == ReviewVerdict.BLOCK


def test_review_blocks_test_weakening(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_x():\n    assert True\n")
    decision = _gate(tmp_path).review(diff="", changed_files=["tests/test_x.py"])
    assert decision.verdict == ReviewVerdict.BLOCK


def test_review_network_access_is_low_not_blocking(tmp_path):
    decision = _gate(tmp_path).review(diff="+import requests\n+requests.get('http://x')\n", changed_files=["app.py"])
    assert decision.verdict == ReviewVerdict.APPROVE
    assert any(f.severity == FindingSeverity.LOW for f in decision.findings)


def test_review_merges_model_findings(tmp_path):
    model_findings = [ReviewFinding(FindingSeverity.HIGH, "architecture regression", file="app.py", rule="model-review")]
    decision = _gate(tmp_path).review(diff="+def ok(): return 1\n", changed_files=["app.py"], model_findings=model_findings)
    assert decision.verdict == ReviewVerdict.BLOCK


# -- model-driven reviewer --------------------------------------------------

class ScriptedReviewProvider:
    name = "scripted-review"

    def __init__(self, response):
        self.response = response

    def generate(self, prompt, *, context="", task="", instructions="", max_output_tokens=None, temperature=None):
        return ModelResult(self.response, self.name)


def _review_fabric(response_text):
    return ModelFabric(
        registry=ModelRegistry([Model(name="rev/m", provider="rev", capabilities=("review",))]),
        providers=ProviderRegistry({"rev": ScriptedReviewProvider(response_text)}),
    )


def test_reviewer_agent_parses_structured_findings():
    payload = json.dumps({"findings": [{"severity": "HIGH", "message": "bad", "file": "app.py"}], "verdict": "BLOCK"})
    findings = ReviewerAgent(fabric=_review_fabric(payload)).review("review app", "diff", ("app.py",))
    assert findings == [ReviewFinding(FindingSeverity.HIGH, "bad", file="app.py", rule="model-review")]


def test_reviewer_agent_returns_none_without_fabric():
    assert ReviewerAgent().review("review app", "diff", ("app.py",)) is None


def test_reviewer_agent_ignores_malformed_model_output():
    fabric = _review_fabric("not json at all")
    assert ReviewerAgent(fabric=fabric).review("review app", "diff", ("app.py",)) == []


def test_reviewer_agent_returns_none_when_no_review_model():
    fabric = ModelFabric(
        registry=ModelRegistry([Model(name="code/m", provider="rev", capabilities=("coding",))]),
        providers=ProviderRegistry({"rev": ScriptedReviewProvider("{}")}),
    )
    assert ReviewerAgent(fabric=fabric).review("review app", "diff", ("app.py",)) is None
