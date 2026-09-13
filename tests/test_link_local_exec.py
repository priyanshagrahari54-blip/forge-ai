"""A81 local executor tests: bounded, closed-vocabulary LOCAL work."""
from __future__ import annotations

import subprocess

import pytest

from forge.client.config import LocalPolicy
from forge.client.local_exec import OPERATIONS, LocalExecutor
from forge.link.errors import LocalExecutionRefused


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "app.py").write_text("def f():\n    return 1\n# TODO fix\n")
    (root / "util.py").write_text("x = 1\n# FIXME later\n# TODO more\n")
    (root / "readme.md").write_text("docs\n")
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    subprocess.run(["git", "init"], cwd=root, capture_output=True)
    return root


def _executor(**policy_kwargs) -> LocalExecutor:
    return LocalExecutor(LocalPolicy(**policy_kwargs))


def test_vocabulary_is_closed_and_small():
    assert OPERATIONS == ("repo_summary", "git_status", "todo_scan")
    assert not _executor().supported("run_tests")
    assert not _executor().supported("rm -rf /")
    assert not _executor().supported("")


def test_unknown_operation_refused(repo):
    with pytest.raises(LocalExecutionRefused):
        _executor().run("import os; os.system('x')", str(repo))


def test_missing_root_refused(tmp_path):
    with pytest.raises(LocalExecutionRefused):
        _executor().run("repo_summary", str(tmp_path / "nope"))


def test_file_root_refused(tmp_path):
    target = tmp_path / "file.txt"
    target.write_text("x")
    with pytest.raises(LocalExecutionRefused):
        _executor().run("repo_summary", str(target))


def test_repo_summary_counts_and_skips(repo):
    report = _executor().run("repo_summary", str(repo))
    assert report["operation"] == "repo_summary"
    assert not report["truncated"]
    extensions = {item["ext"]: item["count"]
                  for item in report["top_extensions"]}
    assert extensions[".py"] == 2
    assert extensions[".md"] == 1
    assert ".git" not in str(report)  # skipped directory
    assert report["python_files"] == 2


def test_git_status_uses_argv_list(repo):
    report = _executor().run("git_status", str(repo))
    assert report["operation"] == "git_status"
    assert report["exit_code"] == 0
    assert report["available"]


def test_git_status_missing_git(tmp_path, monkeypatch):
    (tmp_path / "p").mkdir()
    executor = _executor()

    import forge.client.local_exec as module

    real_run = module.subprocess.run

    def fake_run(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    report = executor.run("git_status", str(tmp_path / "p"))
    assert report["available"] is False
    assert "git not installed" in report["reason"]
    del real_run


def test_todo_scan_counts(repo):
    report = _executor().run("todo_scan", str(repo))
    assert report["todo"] == 2
    assert report["fixme"] == 1
    assert report["files_scanned"] == 2


def test_walk_bounds_are_enforced(tmp_path):
    root = tmp_path / "many"
    root.mkdir()
    for i in range(12):
        (root / f"f{i:02d}.py").write_text("x = 1\n")
    executor = LocalExecutor(LocalPolicy(max_files_walked=5))
    report = executor.run("repo_summary", str(root))
    assert report["files_seen"] == 5
    assert report["truncated"] is True
    todos = executor.run("todo_scan", str(root))
    assert todos["files_scanned"] == 5
    assert todos["truncated"] is True
