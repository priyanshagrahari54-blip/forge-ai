from forge.agents.execution import AgentRequest, AgentResponse
from forge.agents.frontier_fleet import build_frontier_fleet
from forge.core.task_engine import TaskEngine, TaskStatus


class _FakeFabric:
    def __init__(self):
        self.calls = []

    def generate(self, request):
        self.calls.append(request)
        return AgentResponse(success=True, output="ok", agent="fake")


def test_frontier_fleet_registers_1000_plus_executable_specialists():
    fabric = _FakeFabric()
    registry = build_frontier_fleet(fabric, minimum_size=1000)

    assert len(registry) == 1000
    assert len(registry.names()) == 1000
    assert len(registry.roles()) == 40
    assert all(registry.get(name).executor is not None for name in registry.names())

    task = TaskEngine().add("fleet-smoke", "perform a specialist smoke task")
    selected = registry.get("planner-01-0001")
    response = selected.executor.execute(
        AgentRequest(task, TaskStatus.CODING, instructions="return a short result")
    )

    assert response.success is True
    assert response.output == "ok"
    assert fabric.calls
    assert fabric.calls[-1].metadata["agent"] == "planner-01-0001"
    assert fabric.calls[-1].metadata["preferred_model"]
