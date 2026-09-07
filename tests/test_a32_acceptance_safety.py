"""Acceptance evidence, checkpoint exactness, and safe-Git tests (A32.7-9).

The acceptance decision names failed gates and carries measured metrics;
checkpoints record exact restore metadata; autonomous commits stage only the
accepted paths and are impossible without a successful acceptance gate.
"""
import hashlib
import stat
import subprocess

import pytest

from forge.core.acceptance import AcceptanceEngine, GateOutcome
from forge.security.review import FindingSeverity, ReviewDecision, ReviewFinding, ReviewVerdict
from forge.tools.checkpoint import CheckpointManager
from forge.tools.git import GitTool


def _pass(name, evidence=None):
    return GateOutcome(name, True, "", evidence or {})


def _fail(name, details="failed"):
    return GateOutcome(name, False, details)


def _review(verdict, findings=()):
    return ReviewDecision(verdict=verdict, findings=list(findings), reason="test")


def _decide(**overrides):
    params = dict(
        tests=_pass("tests"), build=_pass("build"), lint=_pass("lint"),
        review=_review(ReviewVerdict.APPROVE), security=_pass("security"),
        changed_files=["app.py"],
    )
    params.update(overrides)
    return AcceptanceEngine().decide(**params)


# -- failed gates + metrics --------------------------------------------------

def test_failed_gates_empty_when_accepted():
    decision = _decide()
    assert decision.accepted
    assert decision.failed_gates == []
    assert decision.reasons == []


def test_failed_gates_name_every_failure_in_order():
    decision = _decide(
        tests=_fail("tests"), security=_fail("security"),
        review=_review(ReviewVerdict.BLOCK,
                       [ReviewFinding(FindingSeverity.CRITICAL, "boom")]),
    )
    assert not decision.accepted
    assert decision.failed_gates == ["tests", "review", "security"]
    assert len(decision.reasons) == 3


def test_metrics_carry_measured_evidence():
    security = GateOutcome("security", False, "leak",
                           {"findings": [{"file": "a.py"}, {"file": "b.py"}]})
    decision = _decide(
        security=security,
        review=_review(ReviewVerdict.REQUEST_CHANGES,
                       [ReviewFinding(FindingSeverity.MEDIUM, "m")]),
        changed_files=["a.py", "b.py"],
        lint=GateOutcome("lint/type", True, "no checker", {"commands": []}),
    )
    assert decision.metrics["changed_file_count"] == 2
    assert decision.metrics["security_findings"] == 2
    assert decision.metrics["review_findings"] == 1
    assert decision.metrics["review_verdict"] == "REQUEST_CHANGES"
    assert decision.metrics["lint_executed"] is False
    assert decision.metrics["tests_no_tests"] is False


def test_unexecuted_lint_policy_is_explicit_not_silent():
    lint = GateOutcome("lint/type", True, "No configured lint/type checker",
                       {"commands": []})
    decision = _decide(lint=lint)
    assert decision.accepted  # documented pass-when-unconfigured policy
    assert decision.metrics["lint_executed"] is False
    executed = GateOutcome("lint/type", True, "configured checks executed",
                           {"commands": [{"command": ["ruff"]}]})
    assert _decide(lint=executed).metrics["lint_executed"] is True


def test_to_dict_carries_failed_gates_and_metrics():
    decision = _decide(tests=_fail("tests"))
    payload = decision.to_dict()
    assert payload["failed_gates"] == ["tests"]
    assert payload["metrics"]["changed_file_count"] == 1


# -- checkpoint exactness ----------------------------------------------------

def test_checkpoint_records_hashes_sizes_modes_and_existence(tmp_path):
    target = tmp_path / "app.py"
    target.write_text("original\n")
    target.chmod(0o755)
    checkpoint = CheckpointManager(tmp_path).create(
        "meta", declared=["app.py", "ghost.py"])
    entry = checkpoint.meta["app.py"]
    assert entry["existed"] is True
    assert entry["sha256"] == hashlib.sha256(b"original\n").hexdigest()
    assert entry["size"] == len(b"original\n")
    assert entry["mode"] == stat.S_IMODE(target.stat().st_mode)
    assert checkpoint.meta["ghost.py"] == {"existed": False}
    assert checkpoint.files["app.py"] == entry["sha256"]
    CheckpointManager(tmp_path).cleanup(checkpoint)


def test_rollback_restores_exact_bytes_and_mode(tmp_path):
    target = tmp_path / "app.py"
    target.write_text("original\n")
    target.chmod(0o755)
    manager = CheckpointManager(tmp_path)
    checkpoint = manager.create("exact")
    target.write_text("modified\n")
    target.chmod(0o644)
    manager.rollback(checkpoint, ["app.py"])
    assert target.read_text() == "original\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o755


def test_rollback_restores_only_declared_candidates(tmp_path):
    (tmp_path / "candidate.py").write_text("before\n")
    (tmp_path / "user.txt").write_text("user before\n")
    manager = CheckpointManager(tmp_path)
    checkpoint = manager.create("scope")
    (tmp_path / "candidate.py").write_text("after\n")
    (tmp_path / "user.txt").write_text("user after\n")
    (tmp_path / "user_new.txt").write_text("untracked user work\n")
    manager.rollback(checkpoint, ["candidate.py"])
    assert (tmp_path / "candidate.py").read_text() == "before\n"
    assert (tmp_path / "user.txt").read_text() == "user after\n"
    assert (tmp_path / "user_new.txt").read_text() == "untracked user work\n"


# -- safe git ------------------------------------------------------------------

def _git_repo(root):
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=root, check=True)
    (root / "app.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "--", "app.py"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=root, check=True,
                   capture_output=True)


@pytest.mark.parametrize("name", ["deploy.pem", "id_rsa", "key.key", "store.p12"])
def test_stage_rejects_key_material(tmp_path, name):
    _git_repo(tmp_path)
    (tmp_path / name).write_text("placeholder\n")
    with pytest.raises(ValueError):
        GitTool(tmp_path).stage_files([name])
    assert GitTool(tmp_path).run("diff", "--cached", "--name-only").stdout == ""


def test_commit_refused_without_acceptance_and_stages_nothing(tmp_path):
    _git_repo(tmp_path)
    (tmp_path / "app.py").write_text("x = 2\n")
    git = GitTool(tmp_path)
    with pytest.raises(RuntimeError, match="acceptance gate did not succeed"):
        git.commit_accepted(["app.py"], "nope", {"accepted": False})
    assert git.run("diff", "--cached", "--name-only").stdout == ""
    assert git.run("log", "--oneline").stdout.count("initial") == 1


def test_commit_accepted_with_successful_acceptance(tmp_path):
    _git_repo(tmp_path)
    (tmp_path / "app.py").write_text("x = 2\n")
    git = GitTool(tmp_path)
    decision = _decide()
    assert decision.accepted
    result = git.commit_accepted(["app.py"], "feat: ok", decision)
    assert result.returncode == 0
    assert git.run("show", "--format=", "--name-only", "HEAD").stdout.splitlines() == ["app.py"]


def test_commit_accepted_accepts_mapping_and_boolean(tmp_path):
    _git_repo(tmp_path)
    git = GitTool(tmp_path)
    (tmp_path / "app.py").write_text("x = 2\n")
    assert git.commit_accepted(["app.py"], "m", {"accepted": True}).returncode == 0
    (tmp_path / "app.py").write_text("x = 3\n")
    assert git.commit_accepted(["app.py"], "m", True).returncode == 0
    with pytest.raises(RuntimeError):
        git.commit_accepted(["app.py"], "m", False)
