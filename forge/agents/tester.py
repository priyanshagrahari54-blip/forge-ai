from __future__ import annotations

from forge.agents.execution import AgentExecutor, AgentRequest, AgentResponse


class TesterAgent(AgentExecutor):
    name = "tester"

    def describe(self) -> str:
        return "Responsible for running and evaluating tests."

    def execute(self, request: AgentRequest) -> AgentResponse:
        return AgentResponse(
            success=True,
            output=f"Tester completed tests for '{request.task.description}'.",
            agent=self.name,
            stage=request.stage,
        )
