from typing import Any, Callable, Dict, Optional
from forge.core.planner import Planner
from forge.core.state import ForgeState


class Supervisor:
    def __init__(self, project_name: str) -> None:
        self.state = ForgeState(project_name)
        self.planner = Planner()
        self.current_stage: str = "IDLE"
        self.stage_history: list[dict[str, Any]] = []

    def create_plan(self, request: str):
        return self.planner.create_plan(request)

    def start(self, task: str) -> None:
        self.state.start_task(task)

    def complete(self) -> None:
        self.state.complete_task()

    def set_stage(self, stage: str, details: Optional[Dict[str, Any]] = None) -> None:
        """Transitions self-development task stage.

        Valid self-development stages include:
        ANALYZE, PLAN, CODE, TEST, DEBUG, REVIEW, SECURITY, BENCHMARK, ACCEPT, REJECT.
        """
        self.current_stage = stage
        record = {"stage": stage, "details": details or {}}
        self.stage_history.append(record)

    def execute_self_development_stage(
        self, stage: str, stage_fn: Callable[[], Any], details: Optional[Dict[str, Any]] = None
    ) -> Any:
        self.set_stage(stage, details)
        return stage_fn()
