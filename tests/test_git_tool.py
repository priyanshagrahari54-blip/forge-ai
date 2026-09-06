from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from forge.tools.git import (
    DEFAULT_PROTECTED_BRANCHES,
    GitResult,
    GitTool,
    PullRequest,
    SafeGit,
    SafeGitError,
)


def make_git_tool(
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> GitTool:
    """Create a GitTool whose run() returns canned output."""
    tool = GitTool(repo=".")
    tool.run = MagicMock(return_value=MagicMock(
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    ))
    return tool


class TestProtectedBranches:
    def test_default_protected_branches(self):
        assert "main" in DEFAULT_PROTECTED_BRANCHES
        assert "master" in DEFAULT_PROTECTED_BRANCHES

    def test_custom_protected_branches(self):
        tool = make_git_tool(stdout="feature-x\n")
        git = SafeGit(tool=tool, protected_branches=("production",))

        assert git._is_protected("production")
        assert not git._is_protected("main")


class TestCreateBranch:
    def test_create_branch_blocked_for_protected_name(self):
        tool = make_git_tool()
        git = SafeGit(tool=tool)

        result = git.create_branch("main")

        assert not result.success
        assert result.operation == "create_branch"
        assert "protected" in result.error.lower()

    def test_create_branch_blocked_when_exists(self):
        tool = make_git_tool(returncode=0)
        git = SafeGit(tool=tool)
        git.branch_exists = MagicMock(return_value=True)

        result = git.create_branch("feature-x")

        assert not result.success
        assert "already exists" in result.error.lower()

    def test_create_branch_dry_run(self):
        tool = make_git_tool()
        git = SafeGit(tool=tool, dry_run=True)

        result = git.create_branch("feature-x")

        assert result.success
        assert result.dry_run
        assert "[dry-run]" in result.message

    def test_create_branch_success(self):
        tool = make_git_tool(returncode=0)
        git = SafeGit(tool=tool)
        git.branch_exists = MagicMock(return_value=False)

        result = git.create_branch("feature-x")

        assert result.success
        assert result.branch == "feature-x"
        tool.run.assert_called_with("checkout", "-b", "feature-x")

    def test_create_branch_from_base(self):
        tool = make_git_tool(returncode=0)
        git = SafeGit(tool=tool)
        git.branch_exists = MagicMock(return_value=False)

        git.create_branch("feature-y", base="develop")

        tool.run.assert_called_with("checkout", "-b", "feature-y", "develop")


class TestCommit:
    def test_commit_blocked_on_protected_branch(self):
        tool = make_git_tool(stdout="main\n")
        git = SafeGit(tool=tool)

        with pytest.raises(SafeGitError, match="protected branch"):
            git.commit("add feature")

    def test_commit_dry_run(self):
        tool = make_git_tool(stdout="feature-x\n")
        git = SafeGit(tool=tool, dry_run=True)

        result = git.commit("add feature")

        assert result.success
        assert result.dry_run
        assert "[dry-run]" in result.message

    def test_commit_success(self):
        tool = make_git_tool(returncode=0, stdout="feature-x\n")
        git = SafeGit(tool=tool)

        result = git.commit("add feature")

        assert result.success
        assert result.branch == "feature-x"
        assert "add feature" in result.message

    def test_commit_failure(self):
        tool = make_git_tool(
            returncode=1,
            stdout="feature-x\n",
            stderr="nothing to commit",
        )
        git = SafeGit(tool=tool)

        result = git.commit("add feature")

        assert not result.success
        assert "nothing to commit" in result.error


class TestPush:
    def test_push_blocked_to_protected_branch(self):
        tool = make_git_tool(stdout="main\n")
        git = SafeGit(tool=tool)

        with pytest.raises(SafeGitError, match="protected branch"):
            git.push(branch="main")

    def test_push_force_blocked(self):
        tool = make_git_tool(stdout="feature-x\n")
        git = SafeGit(tool=tool)

        with pytest.raises(SafeGitError, match="(?i)force"):
            git.push(force=True)

    def test_push_dry_run(self):
        tool = make_git_tool(stdout="feature-x\n")
        git = SafeGit(tool=tool, dry_run=True)

        result = git.push()

        assert result.success
        assert result.dry_run
        assert "[dry-run]" in result.message

    def test_push_success(self):
        tool = make_git_tool(returncode=0, stdout="feature-x\n")
        git = SafeGit(tool=tool)

        result = git.push()

        assert result.success
        assert result.branch == "feature-x"
        tool.run.assert_called_with("push", "origin", "feature-x")


class TestAuditLogging:
    def test_audit_records_branch_creation(self):
        tool = make_git_tool(returncode=0)
        records: list[tuple[str, str, str, str]] = []
        git = SafeGit(
            tool=tool,
            audit=lambda op, target, result, msg: records.append(
                (op, target, result, msg)
            ),
        )
        git.branch_exists = MagicMock(return_value=False)

        git.create_branch("feature-x")

        assert any(
            op == "create_branch" and target == "feature-x"
            for op, target, *_ in records
        )

    def test_audit_records_blocked_commit(self):
        tool = make_git_tool(stdout="main\n")
        records: list[tuple[str, str, str, str]] = []
        git = SafeGit(
            tool=tool,
            audit=lambda op, target, result, msg: records.append(
                (op, target, result, msg)
            ),
        )

        with pytest.raises(SafeGitError):
            git.commit("bad commit")

        assert any(
            op == "commit" and result == "blocked"
            for op, _, result, *_ in records
        )


class TestStatusReport:
    def test_status_report_returns_dict(self):
        tool = make_git_tool(returncode=0, stdout="feature-x\n")
        tool.status = MagicMock(return_value=" M file.py")
        git = SafeGit(tool=tool)

        report = git.status_report()

        assert report["branch"] == "feature-x"
        assert report["uncommitted"] == "True"
        assert "file.py" in report["status"]


class TestStageAll:
    def test_stage_all_no_changes(self):
        tool = make_git_tool(returncode=0, stdout="")
        git = SafeGit(tool=tool)

        result = git.stage_all()

        assert result.success
        assert "no changes" in result.message.lower()

    def test_stage_all_dry_run(self):
        tool = make_git_tool(returncode=0, stdout=" M file.py\n")
        git = SafeGit(tool=tool, dry_run=True)

        result = git.stage_all()

        assert result.success
        assert result.dry_run

    def test_stage_all_success(self):
        tool = make_git_tool(returncode=0, stdout=" M file.py\n")
        git = SafeGit(tool=tool)

        result = git.stage_all()

        assert result.success
        tool.run.assert_called_with("add", "-A")