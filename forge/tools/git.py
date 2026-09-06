import subprocess
from dataclasses import dataclass
from typing import Callable


class GitTool:
    """Low-level git command executor.

    Every command goes through ``run`` so that subclasses and tests can
    intercept execution in one place.
    """

    def __init__(self, repo: str = ".") -> None:
        self.repo = repo

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=self.repo,
            text=True,
            capture_output=True,
            check=False,
        )

    def status(self) -> str:
        result = self.run("status", "--short")
        return result.stdout.strip()


# Branches that must never be committed to or pushed to directly.
DEFAULT_PROTECTED_BRANCHES: tuple[str, ...] = ("main", "master", "release")


@dataclass(frozen=True)
class GitResult:
    """Structured result of a safe git operation."""

    success: bool
    operation: str
    branch: str = ""
    message: str = ""
    error: str = ""
    dry_run: bool = False


@dataclass(frozen=True)
class PullRequest:
    """Description of a pull request to be created."""

    title: str
    body: str = ""
    base: str = "main"
    head: str = ""
    draft: bool = False


class SafeGitError(Exception):
    """Raised when a git operation violates a safety policy."""


class SafeGit:
    """Safety-aware Git wrapper.

    Enforces branch protection, audit logging, and dry-run mode while
    exposing the common operations Forge needs: branch creation, commit,
    push, and PR creation.
    """

    def __init__(
        self,
        repo: str = ".",
        tool: GitTool | None = None,
        protected_branches: tuple[str, ...] | None = None,
        audit: Callable[[str, str, str, str], None] | None = None,
        dry_run: bool = False,
    ) -> None:
        self.repo = repo
        self.tool = tool or GitTool(repo)
        self.protected_branches = protected_branches or DEFAULT_PROTECTED_BRANCHES
        self.audit = audit
        self.dry_run = dry_run

    def _record(
        self,
        operation: str,
        target: str,
        result: str,
        message: str = "",
    ) -> None:
        if self.audit is not None:
            self.audit(operation, target, result, message)

    def current_branch(self) -> str:
        result = self.tool.run("rev-parse", "--abbrev-ref", "HEAD")
        return result.stdout.strip()

    def branch_exists(self, branch: str) -> bool:
        result = self.tool.run("rev-parse", "--verify", branch)
        return result.returncode == 0

    def has_uncommitted_changes(self) -> bool:
        result = self.tool.run("status", "--porcelain")
        return bool(result.stdout.strip())

    def _is_protected(self, branch: str) -> bool:
        return branch in self.protected_branches

    def create_branch(
        self,
        branch: str,
        *,
        base: str | None = None,
        switch: bool = True,
    ) -> GitResult:
        """Create a new branch, optionally switching to it.

        Refuses to create a branch with a protected name.
        """
        if self._is_protected(branch):
            self._record("create_branch", branch, "blocked", "protected branch name")
            return GitResult(
                success=False,
                operation="create_branch",
                branch=branch,
                error=f"Cannot create branch with protected name: {branch}",
            )

        if self.dry_run:
            self._record("create_branch", branch, "dry_run")
            return GitResult(
                success=True,
                operation="create_branch",
                branch=branch,
                message=f"[dry-run] would create branch {branch}",
                dry_run=True,
            )

        if self.branch_exists(branch):
            self._record("create_branch", branch, "failed", "already exists")
            return GitResult(
                success=False,
                operation="create_branch",
                branch=branch,
                error=f"Branch already exists: {branch}",
            )

        if base is not None:
            result = self.tool.run("checkout", "-b", branch, base)
        else:
            result = self.tool.run("checkout", "-b", branch)

        if result.returncode != 0:
            error = result.stderr.strip() or result.stdout.strip()
            self._record("create_branch", branch, "failed", error)
            return GitResult(
                success=False,
                operation="create_branch",
                branch=branch,
                error=error,
            )

        self._record("create_branch", branch, "success")
        return GitResult(
            success=True,
            operation="create_branch",
            branch=branch,
            message=f"Created and switched to branch: {branch}",
        )

    def stage_all(self) -> GitResult:
        """Stage all changes in the working tree."""
        if not self.has_uncommitted_changes():
            return GitResult(
                success=True,
                operation="stage_all",
                message="No changes to stage.",
            )

        if self.dry_run:
            self._record("stage_all", "", "dry_run")
            return GitResult(
                success=True,
                operation="stage_all",
                message="[dry-run] would stage all changes",
                dry_run=True,
            )

        result = self.tool.run("add", "-A")
        if result.returncode != 0:
            error = result.stderr.strip()
            self._record("stage_all", "", "failed", error)
            return GitResult(
                success=False,
                operation="stage_all",
                error=error,
            )

        self._record("stage_all", "", "success")
        return GitResult(
            success=True,
            operation="stage_all",
            message="Staged all changes.",
        )

    def commit(
        self,
        message: str,
        *,
        allow_empty: bool = False,
    ) -> GitResult:
        """Commit staged changes on the current branch.

        Refuses to commit directly to a protected branch.
        """
        current = self.current_branch()

        if self._is_protected(current):
            self._record("commit", current, "blocked", "protected branch")
            raise SafeGitError(
                f"Refusing to commit directly to protected branch: {current}"
            )

        if self.dry_run:
            self._record("commit", current, "dry_run", message)
            return GitResult(
                success=True,
                operation="commit",
                branch=current,
                message=f"[dry-run] would commit on {current}: {message}",
                dry_run=True,
            )

        args = ["commit", "-m", message]
        if allow_empty:
            args.append("--allow-empty")

        result = self.tool.run(*args)
        if result.returncode != 0:
            error = result.stderr.strip() or result.stdout.strip()
            self._record("commit", current, "failed", error)
            return GitResult(
                success=False,
                operation="commit",
                branch=current,
                error=error,
            )

        self._record("commit", current, "success", message)
        return GitResult(
            success=True,
            operation="commit",
            branch=current,
            message=f"Committed on {current}: {message}",
        )

    def push(
        self,
        remote: str = "origin",
        branch: str | None = None,
        *,
        force: bool = False,
    ) -> GitResult:
        """Push the current or specified branch.

        Refuses to push to protected branches or to force-push.
        """
        target_branch = branch or self.current_branch()

        if self._is_protected(target_branch):
            self._record("push", target_branch, "blocked", "protected branch")
            raise SafeGitError(
                f"Refusing to push to protected branch: {target_branch}"
            )

        if force:
            self._record("push", target_branch, "blocked", "force push denied")
            raise SafeGitError("Force pushes are not permitted.")

        if self.dry_run:
            self._record("push", target_branch, "dry_run")
            return GitResult(
                success=True,
                operation="push",
                branch=target_branch,
                message=f"[dry-run] would push {target_branch} to {remote}",
                dry_run=True,
            )

        result = self.tool.run("push", remote, target_branch)
        if result.returncode != 0:
            error = result.stderr.strip() or result.stdout.strip()
            self._record("push", target_branch, "failed", error)
            return GitResult(
                success=False,
                operation="push",
                branch=target_branch,
                error=error,
            )

        self._record("push", target_branch, "success")
        return GitResult(
            success=True,
            operation="push",
            branch=target_branch,
            message=f"Pushed {target_branch} to {remote}",
        )

    def create_pull_request(self, pr: PullRequest) -> GitResult:
        """Create a pull request using the GitHub CLI (``gh``).

        Falls back gracefully when ``gh`` is not available or the repo
        is not a GitHub remote.
        """
        current = self.current_branch()
        head = pr.head or current

        if self._is_protected(head):
            self._record("create_pr", head, "blocked", "protected head branch")
            return GitResult(
                success=False,
                operation="create_pr",
                branch=head,
                error=f"Cannot create PR from protected branch: {head}",
            )

        if self.dry_run:
            self._record("create_pr", head, "dry_run", pr.title)
            return GitResult(
                success=True,
                operation="create_pr",
                branch=head,
                message=f"[dry-run] would create PR: {pr.title}",
                dry_run=True,
            )

        cmd = [
            "gh",
            "pr",
            "create",
            "--title",
            pr.title,
            "--body",
            pr.body,
            "--base",
            pr.base,
            "--head",
            head,
        ]
        if pr.draft:
            cmd.append("--draft")

        result = subprocess.run(
            cmd,
            cwd=self.repo,
            text=True,
            capture_output=True,
            check=False,
        )

        if result.returncode != 0:
            error = result.stderr.strip() or result.stdout.strip()
            if "not installed" in error.lower() or result.returncode == 127:
                error = (
                    "GitHub CLI (gh) is not installed or not available. "
                    "Install it to create pull requests."
                )
            self._record("create_pr", head, "failed", error)
            return GitResult(
                success=False,
                operation="create_pr",
                branch=head,
                error=error,
            )

        self._record("create_pr", head, "success", pr.title)
        return GitResult(
            success=True,
            operation="create_pr",
            branch=head,
            message=result.stdout.strip() or f"Created PR: {pr.title}",
        )

    def status_report(self) -> dict[str, str]:
        """Return a summary of the current repo state for agents."""
        return {
            "branch": self.current_branch(),
            "status": self.tool.status(),
            "uncommitted": str(self.has_uncommitted_changes()),
        }
