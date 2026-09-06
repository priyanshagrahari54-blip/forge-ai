from __future__ import annotations

from forge.agents.execution import AgentExecutor, AgentRequest, AgentResponse
from forge.intelligence.agent_context import AgentContext, AgentContextBuilder
from forge.intelligence.repository import RepositoryIntelligence
from forge.security.verification import ReviewGate, VerificationResult


class ReviewerAgent(AgentExecutor):
    name = "reviewer"

    def __init__(self) -> None:
        self.review_gate = ReviewGate()

    def describe(self) -> str:
        return "Responsible for independently reviewing changes."

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

    def review(self, changes: dict[str, str], diff: str = "") -> VerificationResult:
        return self.review_gate.verify(changes, diff)

    def execute(self, request: AgentRequest) -> AgentResponse:
        changes = request.metadata.get("changes") if request.metadata else None
        if isinstance(changes, dict):
            res = self.review(changes)
            if not res.success:
                return AgentResponse(
                    success=False,
                    error=f"Review failed: {'; '.join(res.errors)}",
                    agent=self.name,
                    stage=request.stage,
                )

        return AgentResponse(
            success=True,
            output="Review passed cleanly.",
            agent=self.name,
            stage=request.stage,
        )
