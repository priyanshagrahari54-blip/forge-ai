from forge.core.planner import Planner
from forge.core.state import ForgeState


class Supervisor:
    def __init__(self, project_name: str) -> None:
        self.state = ForgeState(project_name)
        self.planner = Planner()

    def create_plan(self, request: str):
        return self.planner.create_plan(request)

    def start(self, task: str) -> None:
        self.state.start_task(task)

    def complete(self) -> None:
        self.state.complete_task()
