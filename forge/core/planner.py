from dataclasses import dataclass


@dataclass
class PlanStep:
    id: str
    description: str


class Planner:
    def create_plan(self, request: str) -> list[PlanStep]:
        return [
            PlanStep(
                "1",
                f"Understand the requirements: {request}",
            ),
            PlanStep(
                "2",
                "Inspect the existing project.",
            ),
            PlanStep(
                "3",
                "Implement the required changes.",
            ),
            PlanStep(
                "4",
                "Run tests and validate the result.",
            ),
            PlanStep(
                "5",
                "Review the implementation.",
            ),
        ]
