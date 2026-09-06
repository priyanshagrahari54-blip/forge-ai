from __future__ import annotations

from forge.agents.execution import AgentExecutor, AgentRequest, AgentResponse
from forge.consensus.engine import ConsensusEngine, ConsensusResult


class ConsensusAgent:
    """Metadata and context building for the consensus agent."""

    name = "consensus"

    def describe(self) -> str:
        return (
            "Responsible for orchestrating multi-agent review and "
            "determining consensus across agent responses."
        )

    def build_context(self, intelligence, task, **kwargs):
        from forge.intelligence.agent_context import AgentContextBuilder

        return AgentContextBuilder(intelligence, **kwargs).build(
            task=task,
        )


class ConsensusExecutor(AgentExecutor):
    """Deterministic, model-independent consensus executor.

    Interprets a task description as a request for consensus evaluation
    and returns a structured report. When supplied with a list of
    responses to evaluate, it runs the consensus vote and reports the
    outcome. Otherwise, it explains how to use the consensus system.

    Executes entirely locally and never calls an external provider.
    """

    name = "consensus"

    def __init__(
        self,
        engine: ConsensusEngine | None = None,
    ) -> None:
        self.engine = engine or ConsensusEngine()

    def execute(self, request: AgentRequest) -> AgentResponse:
        try:
            report = self._compile_report(request)
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
            output=report,
            agent=self.name,
            stage=request.stage,
            context_fingerprint=(
                request.context.fingerprint
                if request.context
                else ""
            ),
        )

    def _compile_report(self, request: AgentRequest) -> str:
        metadata = request.metadata or {}
        responses_key = "consensus_responses"

        lines: list[str] = [
            "# Consensus Report",
            "",
            f"Task: {request.task.description}",
            f"Strategy: {self.engine.strategy_name}",
            f"Quorum: {self.engine.quorum}",
            "",
        ]

        if responses_key in metadata:
            lines.extend(self._report_on_responses(metadata[responses_key]))
        elif "responses" in metadata:
            lines.extend(self._report_on_responses(metadata["responses"]))
        else:
            lines.extend(
                [
                    "## Usage",
                    "",
                    "To evaluate consensus, provide a list of agent "
                    "responses in the request metadata under the key "
                    "`consensus_responses` or `responses`.",
                    "",
                    "Each response should be a dict with `agent`, "
                    "`output`, and `success` fields.",
                    "",
                    f"Available strategies: {', '.join(ConsensusEngine.available_strategies())}",
                ]
            )

        return "\n".join(lines)

    def _report_on_responses(self, raw_responses) -> list[str]:
        responses = self._parse_responses(raw_responses)

        if not responses:
            return [
                "## Result",
                "",
                "No valid responses provided for consensus evaluation.",
            ]

        result = self.engine.vote(responses)

        lines = [
            "## Vote Results",
            "",
            f"- Strategy: {result.strategy}",
            f"- Total votes: {result.total_votes}",
            f"- Winning votes: {result.winning_votes}",
            f"- Consensus reached: {result.agreed}",
            f"- Tie: {result.tie}",
            "",
            "## Vote Distribution",
        ]

        for output, count in sorted(
            result.vote_counts.items(),
            key=lambda item: item[1],
            reverse=True,
        ):
            truncated = output[:80].replace("\n", " ")
            lines.append(f"- [{count} votes] {truncated}")

        if result.agreed:
            lines.extend(
                [
                    "",
                    "## Winning Output",
                    "",
                    result.winning_output,
                ]
            )

        return lines

    @staticmethod
    def _parse_responses(raw_responses) -> list[AgentResponse]:
        """Parse raw response dicts into AgentResponse objects."""
        parsed: list[AgentResponse] = []

        for raw in raw_responses:
            if isinstance(raw, AgentResponse):
                parsed.append(raw)
            elif isinstance(raw, dict):
                parsed.append(
                    AgentResponse(
                        success=raw.get("success", True),
                        output=raw.get("output", ""),
                        agent=raw.get("agent", ""),
                    )
                )

        return parsed
