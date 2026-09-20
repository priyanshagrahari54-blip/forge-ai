from forge.agents.execution import AgentRequest, AgentResponse
from forge.agents.frontier_fleet import SPECIALIZATION_CAPABILITY_MAP, SPECIALIZATIONS, build_frontier_fleet
from forge.models.capabilities import ALL_CAPABILITIES
from forge.core.task_engine import TaskEngine, TaskStatus


class _FakeFabric:
    def __init__(self):
        self.calls = []

    def generate(self, request):
        self.calls.append(request)
        return type(
            "Response",
            (),
            {"success": True, "text": "ok", "model": "fake-model", "provider": "fake", "metadata": {}},
        )()


def test_frontier_fleet_registers_1000_plus_executable_specialists():
    fabric = _FakeFabric()
    registry = build_frontier_fleet(fabric, minimum_size=1000)

    assert len(registry) == 1040
    assert len(registry.names()) == 1000
    assert len(registry.roles()) == 40
    assert all(registry.get(name).executor is not None for name in registry.names())
    assert set(registry.capabilities()).issubset(set(ALL_CAPABILITIES))
    assert {specialization for specialization, _role, _declared in SPECIALIZATIONS} == set(SPECIALIZATION_CAPABILITY_MAP)

    task = TaskEngine().add("fleet-smoke", "perform a specialist smoke task")
    selected = registry.get("planner-01-0001")
    response = selected.executor.execute(
        AgentRequest(task, TaskStatus.CODING, instructions="return a short result")
    )

    assert response.success is True
    assert response.output == "ok"
    assert fabric.calls
    request = fabric.calls[-1]
    assert request.metadata["agent"] == "planner-01-0001"
    assert request.metadata["fleet"] == "frontier-1000-plus"
    assert request.capability == "planning"
    assert request.required_capabilities == ("planning", "reasoning")
    assert request.caller == "frontier-agent:planner-01-0001"
    assert request.metadata["preferred_model"]
    assert request.preferred_models == (request.metadata["preferred_model"],)
