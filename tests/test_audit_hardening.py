"""Regression tests for the A01-A31 audit/hardening pass.

Each test pins a concrete behavior fixed during the audit: honest telemetry,
memory path safety, no fabricated metrics, coder syntax validation, security
scan exclusions, checkpoint exclusions, terminal output bounds, capability
verification levels, deterministic consensus, and planner validation.
"""
import json
import sys

import pytest

from forge.agents.coder import CoderAgent
from forge.agents.debugger import DebuggerAgent, TestDebugLoop
from forge.core.planner import Planner
from forge.memory.store import MemoryStore
from forge.models.consensus import ConsensusStrategy, consensus
from forge.models.config import FabricConfig
from forge.models.fabric import ModelFabric
from forge.models.provider import MockProvider
from forge.models.request import ModelResponse
from forge.models.router import ModelInfo, ModelRouter
from forge.self_development.analyzer import ForgeSelfAnalyzer
from forge.security.permissions import PermissionManager
from forge.security.verification import VerificationPipeline
from forge.tools.checkpoint import CheckpointManager
from forge.tools.terminal import TerminalTool


# -- A10: successful final test attempt is recorded -----------------------

def test_debug_loop_records_successful_final_attempt(tmp_path):
    (tmp_path / "app.py").write_text("def value(): return 1\n")
    (tmp_path / "test_app.py").write_text("import app\ndef test_value():\n    assert app.value() == 2\n")
    provider = MockProvider(json.dumps({"changes": {"app.py": "def value(): return 2\n"}, "explanation": "fix"}))
    router = ModelRouter([ModelInfo("fixer", "debugging", available=True, provider=provider,
                                    capabilities=("debugging", "coding"))])
    loop = TestDebugLoop(tmp_path, max_retries=2, debugger=DebuggerAgent(str(tmp_path), router=router))
    result = loop.run("make value 2", approved=True)
    assert result.success
    assert result.attempts
    # The failed repair attempt and the passing final retest are both present.
    assert result.attempts[-1].test_passed is True
    assert result.attempts[-1].model == "fixer"
    assert any(not attempt.test_passed for attempt in result.attempts)


# -- A05: memory path safety and bounds -----------------------------------

def test_memory_store_rejects_traversal(tmp_path):
    store = MemoryStore(str(tmp_path / "memory"))
    for bad in ("../../escape.txt", "/etc/passwd", "a/../../b.txt", ""):
        with pytest.raises(ValueError):
            store.save(bad, "x")


def test_memory_store_size_bound_and_lifecycle(tmp_path):
    store = MemoryStore(str(tmp_path / "memory"), max_bytes=10)
    store.save("note", "short")
    assert store.load("note") == "short"
    with pytest.raises(ValueError):
        store.save("big", "x" * 100)
    assert store.exists("note")
    assert store.list() == ["note"]
    assert store.delete("note") is True
    assert store.delete("note") is False
    assert store.load("note") is None


# -- A26/A29: no fabricated metrics ---------------------------------------

def test_self_analyzer_reports_real_permission_counts(tmp_path):
    (tmp_path / "forge").mkdir()
    (tmp_path / "forge" / "__init__.py").write_text("")
    analysis = ForgeSelfAnalyzer(str(tmp_path)).analyze()
    rules = PermissionManager().rules
    sec = analysis["security_metrics"]
    assert sec["permission_rules_count"] == len(rules)
    assert sec["blocked_operations"] == sum(1 for v in rules.values() if v.value == "blocked")
    # Static analysis never claims tests passed.
    assert analysis["test_metrics"]["passed_tests"] == 0
    assert analysis["test_metrics"]["failed_tests"] == 0


# -- A08: coder syntax validation -----------------------------------------

def test_coder_rejects_invalid_python(tmp_path):
    coder = CoderAgent(root=str(tmp_path), router=ModelRouter())
    payload = json.dumps({"changes": {"app.py": "def broken(:\n"}, "explanation": "oops"})
    with pytest.raises(ValueError):
        coder._changes(payload)
    good = json.dumps({"changes": {"app.py": "def ok(): return 1\n"}, "explanation": "fine"})
    assert coder._changes(good) == {"app.py": "def ok(): return 1\n"}


def test_coder_syntax_check_ignores_non_python(tmp_path):
    coder = CoderAgent(root=str(tmp_path), router=ModelRouter())
    payload = json.dumps({"changes": {"README.md": "no python here"}, "explanation": "ok"})
    assert coder._changes(payload) == {"README.md": "no python here"}


# -- A12: security exclusions and env-file detection -----------------------

def test_security_flags_env_files(tmp_path):
    (tmp_path / ".env").write_text("DATABASE_URL=postgres://x\n")
    gate = VerificationPipeline(tmp_path).security()
    assert not gate.passed
    assert any("environment file" in finding["rule"] for finding in gate.evidence["findings"])


def test_security_full_scan_excludes_vendored_dirs(tmp_path):
    (tmp_path / ".venv" / "site-packages" / "pkg").mkdir(parents=True)
    (tmp_path / ".venv" / "site-packages" / "pkg" / "leak.py").write_text("api_key = 'super-secret-value'\n")
    (tmp_path / "app.py").write_text("def fine(): return 1\n")
    gate = VerificationPipeline(tmp_path).security()
    assert gate.passed  # the venv leak is out of scope; app.py is clean


# -- A21: checkpoint excludes vendored dirs --------------------------------

def test_checkpoint_excludes_vendored_directories(tmp_path):
    (tmp_path / ".venv" / "big").mkdir(parents=True)
    (tmp_path / ".venv" / "big" / "mod.py").write_text("x = 1\n")
    (tmp_path / "app.py").write_text("y = 1\n")
    checkpoint = CheckpointManager(tmp_path).create("audit")
    assert "app.py" in checkpoint.files
    assert not any(".venv" in path for path in checkpoint.files)
    checkpoint_manager = CheckpointManager(tmp_path)
    checkpoint_manager.cleanup(checkpoint)


# -- A02: terminal output bounds -------------------------------------------

def test_terminal_truncates_large_output(tmp_path):
    result = TerminalTool(str(tmp_path)).run([sys.executable, "-c", "print('x' * 300000)"])
    assert result.success
    assert len(result.output) < 250000
    assert "truncated" in result.output


# -- A31: capability verification levels -----------------------------------

def test_capability_status_defaults_to_declared():
    from forge.models.registry import Model
    model = Model(name="m", provider="p", capabilities=("coding", "vision"))
    assert model.capability_status_for("coding") == "declared"
    assert model.capability_status_for("vision") == "declared"


def test_ollama_vision_capability_is_detected_not_verified():
    fabric = ModelFabric.from_defaults(FabricConfig.from_dict({"ollama_model": "llava:7b"}))
    model = fabric.registry.get("ollama/llava:7b")
    assert model.capability_status_for("vision") == "detected"
    assert model.capability_status_for("coding") == "declared"


def test_model_capability_status_roundtrip():
    from forge.models.registry import Model
    model = Model(name="m", provider="p", capabilities=("vision",), capability_status={"vision": "verified"})
    restored = Model.from_dict(model.to_dict())
    assert restored.capability_status_for("vision") == "verified"


# -- A18: deterministic consensus ------------------------------------------

def _resp(text, model="m", success=True):
    return ModelResponse(text=text, model=model, provider="p", success=success)


def test_consensus_majority_agrees():
    result = consensus([_resp("a"), _resp("a"), _resp("b")], ConsensusStrategy.MAJORITY)
    assert result.agreed
    assert result.selected == "a"
    assert result.agreeing == 2


def test_consensus_unanimous_requires_identity():
    assert consensus([_resp("a"), _resp("a")], ConsensusStrategy.UNANIMOUS).agreed
    not_unanimous = consensus([_resp("a"), _resp("b")], ConsensusStrategy.UNANIMOUS)
    assert not not_unanimous.agreed
    assert "not unanimous" in not_unanimous.reason


def test_consensus_weighted_and_no_success():
    weighted = consensus([_resp("a"), _resp("b")], ConsensusStrategy.WEIGHTED, weights=[3.0, 1.0])
    assert weighted.agreed and weighted.selected == "a"
    empty = consensus([_resp("x", success=False)])
    assert not empty.agreed
    assert "no successful" in empty.reason


# -- A06: planner validation -----------------------------------------------

def test_planner_rejects_empty_and_orders_steps():
    with pytest.raises(ValueError):
        Planner().create_plan("   ")
    steps = Planner().create_plan("add CSV export")
    assert len(steps) == 5
    assert steps[0].depends_on == ()
    assert all(step.depends_on for step in steps[1:])
    assert [s.id for s in steps] == ["1", "2", "3", "4", "5"]
    assert "add CSV export" in steps[0].description
