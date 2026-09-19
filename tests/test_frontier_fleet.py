from forge.agents.execution import AgentRequest, AgentResponse
from forge.agents.frontier_fleet import build_frontier_fleet
from forge.core.task_engine import TaskEngine, TaskStatus
from forge.models.capabilities import ALL_CAPABILITIES
from forge.models.fabric import ModelFabric
from forge.models.provider import ModelResult, ProviderRegistry
from forge.models.registry import Model, ModelRegistry


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
    request = fabric.calls[-1]
    assert request.metadata["agent"] == "planner-01-0001"
    assert request.metadata["fleet"] == "frontier-1000-plus"
    assert request.capability == "planning"
    assert request.required_capabilities == ("planning", "reasoning")
    assert request.caller == "frontier-agent:planner-01-0001"
    assert request.metadata["preferred_model"]


def test_every_registered_specialist_routes_with_canonical_capabilities():
    """Registered is not enough: every specialist must be *routable*.

    A specialist that asks the fabric for a label no model can advertise
    (``database``, ``web``, ``multilingual`` …) can never be served: ``Model``
    rejects unknown capabilities and the router refuses to relax capability
    requirements, so the request fails closed with "no registered model
    supports capabilities [...]". Only canonical capabilities are ever
    required from a model; the declared labels stay on the registration (for
    agent-level selection) and on the request metadata.
    """
    registry = build_frontier_fleet(_FakeFabric(), minimum_size=1000)
    canonical = set(ALL_CAPABILITIES)
    for name in registry.names():
        registration = registry.get(name)
        executor = registration.executor
        assert registration.capabilities == executor.declared_capabilities, name
        assert executor.capabilities, name
        assert set(executor.capabilities) <= canonical, name
        assert set(executor.capabilities) <= set(executor.declared_capabilities) \
            or "reasoning" in executor.capabilities, name


def _fabric_with_one_verified_model():
    provider = _RecordingProvider()
    model = Model(name="full-capability-model", provider="in-process",
                  capabilities=tuple(ALL_CAPABILITIES))
    model.metadata["runtime_verified"] = True
    fabric = ModelFabric(registry=ModelRegistry([model]),
                         providers=ProviderRegistry({"in-process": provider}))
    return fabric, provider


class _RecordingProvider:
    name = "in-process"

    def __init__(self):
        self.prompts = []

    def generate(self, prompt, **kwargs):
        self.prompts.append(prompt)
        return ModelResult("specialist ok", "full-capability-model")


def test_representative_specialists_execute_through_model_fabric():
    """One representative per specialization really executes end to end."""
    fabric, provider = _fabric_with_one_verified_model()
    registry = build_frontier_fleet(fabric, minimum_size=1000)
    engine = TaskEngine()
    roles = registry.roles()
    assert len(roles) == 40
    executed = []
    for role in roles:
        registration = registry.get_by_role(role)[0]
        task = engine.add("fleet-" + registration.name,
                          "perform a specialist smoke task")
        response = registration.executor.execute(
            AgentRequest(task, TaskStatus.CODING, instructions="do it"))
        assert response.success is True, (registration.name, response.error)
        assert response.metadata["routed_model"] == "full-capability-model"
        assert response.metadata["routed_provider"] == "in-process"
        executed.append(registration.name)
    assert len(executed) == len(roles)
    assert provider.prompts
