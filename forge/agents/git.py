from __future__ import annotations

from forge.agents.execution import AgentExecutor, AgentRequest, AgentResponse
from forge.intelligence.agent_context import (
    AgentContext,
    AgentContextBuilder,
)
from forge.intelligence.repository import RepositoryIntelligence
from forge.tools.git import SafeGit


class GitAgent:
    """Metadata and context building for the git automation agent."""

    name = "git"

    def describe(self) -> str:
        return (
            "Responsible for safe Git operations: branch creation, "
            "committing, pushing, and pull-request automation."
        )

    def build_context(
        self,
        intelligence: RepositoryIntelligence,
        task: str,
        target_files: tuple[str, ...] = (),
        target_symbols: tuple[str, ...] = (),
        max_tokens: int = 4000,
    ) -> AgentContext:
        return AgentContextBuilder(
            intelligence,
            max_tokens=max_tokens,
        ).build(
            task=task,
            target_files=target_files,
            target_symbols=target_symbols,
        )


class GitExecutor(AgentExecutor):
    """Deterministic, model-independent git automation executor.

    Interprets a task description as a request for a safe git workflow
    and returns a structured plan describing which operations would run.
    When invoked with an explicit ``safe_git`` instance it can execute
    the workflow; otherwise it produces a dry-run plan.

    Executes entirely locally and never calls an external provider.
    """

    name = "git"

    def __init__(
        self,
        safe_git: SafeGit | None = None,
        intelligence: RepositoryIntelligence | None = None,
    ) -> None:
        self.safe_git = safe_git
        self.intelligence = intelligence

    def execute(self, request: AgentRequest) -> AgentResponse:
        try:
            plan = self._compile_plan(request)
        except Exception as exc:
            return AgentResponse(
                success=False,
                error=str(exc),
                agent=self.name,
                stage=request.stage,
                context_fingerprint=(
                    request.context.fingerprint
                    if request.context
                    else ""
                ),
            )

        return AgentResponse(
            success=True,
            output=plan,
            agent=self.name,
            stage=request.stage,
            context_fingerprint=(
                request.context.fingerprint
                if request.context
                else ""
            ),
        )

    def _compile_plan(self, request: AgentRequest) -> str:
        task = request.task.description.lower()
        lines: list[str] = [
            "# Git Automation Plan",
            "",
            f"Task: {request.task.description}",
            "",
        ]

        if self.safe_git is not None:
            report = self.safe_git.status_report()
            lines.extend(
                [
                    "## Current repository state",
                    f"- branch: {report['branch']}",
                    f"- uncommitted changes: {report['uncommitted']}",
                    "",
                ]
            )

        operations = self._detect_operations(task)

        if not operations:
            lines.append(
                "## Result",
            )
            lines.append(
                "No git operations were requested. Mention a branch, "
                "commit, push, or pull request to trigger automation.",
            )
            return "\n".join(lines)

        lines.append("## Planned operations")
        for op in operations:
            lines.append(f"- {op}")

        if self.safe_git is not None and not self.safe_git.dry_run:
            lines.extend(
                [
                    "",
                    "Note: attach a SafeGit instance with dry_run=True "
                    "to preview without side effects.",
                ]
            )

        return "\n".join(lines)

    @staticmethod
    def _detect_operations(task: str) -> list[str]:
        """Detect which git operations the task is requesting."""
        operations: list[str] = []

        branch_keywords = (
            "branch",
            "checkout",
            "feature branch",
            "create branch",
            "new branch",
        )
        if any(keyword in task for keyword in branch_keywords):
            operations.append(
                "create_branch: create and switch to a new feature branch"
            )

        commit_keywords = (
            "commit",
            "stage and commit",
            "save changes",
        )
        if any(keyword in task for keyword in commit_keywords):
            operations.append(
                "commit: stage all changes and commit on the current branch"
            )

        push_keywords = (
            "push",
            "upload",
            "publish branch",
        )
        if any(keyword in task for keyword in push_keywords):
            operations.append(
                "push: push the current branch to origin"
            )

        pr_keywords = (
            "pull request",
            "pr ",
            "create pr",
            "open pr",
            "submit pr",
            "pull-request",
        )
        if any(keyword in task for keyword in pr_keywords):
            operations.append(
                "create_pull_request: open a pull request via gh CLI"
            )

        return operations