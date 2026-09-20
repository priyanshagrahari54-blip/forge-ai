from forge.agents.execution import AgentRequest, AgentResponse
from forge.agents.registry import AgentRegistry
from forge.agents.frontier_fleet import (
    DEFAULT_FLEET_SIZE, SPECIALIZATIONS, build_frontier_fleet,
    routing_capabilities)
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
    assert set(registry.capabilities())  # declared labels stay on the agents
    assert len(SPECIALIZATIONS) == 40
    # Every declared specialization normalizes into the canonical Fabric
    # capability vocabulary before routing.
    for _specialization, _role, declared in SPECIALIZATIONS:
        canonical = routing_capabilities(declared)
        assert canonical
        assert set(canonical) <= set(ALL_CAPABILITIES)

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
    # Only the specialization-defining capability is a hard requirement: the
    # router never relaxes one, and requiring a secondary preference (here
    # "reasoning", previously "tool_use" for many roles) made specialists
    # unroutable against models that could actually do their job.
    assert request.required_capabilities == ("planning",)
    assert request.metadata["preferred_capabilities"] == "reasoning"
    assert request.metadata["routing_capabilities"] == "planning,reasoning"
    assert request.caller == "frontier-agent:planner-01-0001"
    assert request.metadata["preferred_model"]
    # The assigned model travels as a request-level preference: it may order
    # eligible candidates but never bypass capability/health/policy gates.
    assert request.preferred_models == (request.metadata["preferred_model"],)


def test_default_fleet_is_1040_specialists_at_26_variants_per_family():
    """The canonical production fleet is 40 families x 26 variants = 1,040."""
    assert DEFAULT_FLEET_SIZE == 1040
    registry = build_frontier_fleet(_FakeFabric())
    assert len(registry) == 1040
    assert len(registry.names()) == 1040
    assert len(registry.roles()) == 40
    per_family = {}
    for name in registry.names():
        family = name.rsplit("-", 2)[0]
        per_family[family] = per_family.get(family, 0) + 1
    assert len(per_family) == 40
    assert set(per_family.values()) == {26}


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


def _deployment_like_fabric():
    """The capability set the shipped deployment's models actually declare."""
    provider = _RecordingProvider()
    declared = ("coding", "debugging", "documentation", "planning",
                "reasoning", "research", "review", "security",
                "structured_output", "testing")
    model = Model(name="deployment-model", provider="in-process",
                  capabilities=declared)
    model.metadata["runtime_verified"] = True
    fabric = ModelFabric(registry=ModelRegistry([model]),
                         providers=ProviderRegistry({"in-process": provider}))
    return fabric, declared


def test_a_specialist_only_fails_when_the_capability_is_really_absent():
    """Regression: no specialist may be unroutable for an available capability.

    Before the required/preferred split, 200 of 1,000 specialists failed with
    "no registered model supports capabilities [...]" against a model that
    declared every capability they actually needed, because a secondary
    preference (``tool_use``) had been turned into a hard requirement. The
    invariant is now: a routing failure names a capability that *no*
    registered model advertises, so a failure always means "this deployment
    truly cannot do it", never "the contract was too strict".
    """
    fabric, declared = _deployment_like_fabric()
    registry = build_frontier_fleet(fabric, minimum_size=1000)
    engine = TaskEngine()
    executed, blocked = 0, {}
    for name in registry.names():
        registration = registry.get(name)
        task = engine.add("deploy-" + name, "specialist smoke task")
        response = registration.executor.execute(
            AgentRequest(task, TaskStatus.CODING, instructions="do it"))
        if response.success:
            executed += 1
            continue
        required = registration.executor.required_capabilities[0]
        assert required not in declared, (name, required, response.error)
        assert "no registered model supports capabilities" in response.error
        blocked.setdefault(required, 0)
        blocked[required] += 1
    # Only genuinely-missing capabilities (vision/audio/browser/computer-use
    # in this deployment) may block a specialist: 100 of 1,000, not 200.
    assert executed == 900, blocked
    assert blocked == {"audio": 25, "browser": 25, "computer_use": 25,
                       "vision": 25}


def test_every_requirement_capability_selects_a_registered_specialist():
    """The planner's whole vocabulary must be staffed by the fleet.

    ``TaskRequirementExtractor`` can emit ten capabilities and maps each to a
    role. A capability that no specialist advertises — or whose role no
    specialist holds — is dropped silently by the planner, so the task runs
    under-covered while the plan looks complete. This pins one selectable
    specialist per capability, and pins that the gap is *reported* when it
    exists.
    """
    from forge.agents.planner import CapabilityAgentPlanner
    from forge.agents.requirements import TaskRequirementExtractor

    phrases = {
        "debugging": "fix the crashing bug",
        "coding": "implement the feature",
        "testing": "add unit tests",
        "review": "review the module",
        "security": "audit security of the login",
        "documentation": "document the api",
        "research": "research the best approach",
        "architecture": "design the architecture",
        "performance": "improve query performance",
        "git": "commit and merge the branch",
    }
    extractor = TaskRequirementExtractor()
    assert set(phrases) == {cap for cap, _ in extractor.RULES}
    registry = build_frontier_fleet(_FakeFabric(), minimum_size=1000)
    planner = CapabilityAgentPlanner(registry)
    for capability, phrase in phrases.items():
        plan = planner.plan(phrase)
        selected = [agent for agent in plan.agents
                    if agent.capability == capability]
        assert selected, (capability, plan.names)
        assert not plan.unmet, (capability, plan.unmet)


def test_plan_reports_capabilities_it_could_not_staff():
    """An unstaffable requirement is reported, never silently dropped."""
    from forge.agents.planner import CapabilityAgentPlanner

    registry = build_frontier_fleet(_FakeFabric(), minimum_size=1000)
    # An empty registry cannot staff anything the extractor asks for.
    plan = CapabilityAgentPlanner(AgentRegistry()).plan("document the api")
    assert plan.unmet == ("documentation",)
    assert plan.names == ()


def test_specialist_requests_carry_a_bounded_output_budget():
    """A specialist is a caller, not a chat user.

    Before this was enforced, every specialist call omitted ``max_tokens``:
    the local runtime then generated to its own ceiling (512 tokens) for each
    of 1,000 specialists, turning a short fleet sweep into hours of
    unbounded generation. The budget must reach the fabric request, and must
    be configurable for the whole fleet.
    """
    fabric = _FakeFabric()
    registry = build_frontier_fleet(fabric, minimum_size=1000)
    task = TaskEngine().add("budget", "produce a short answer")

    defaults = {name: registry.get(name).executor.max_output_tokens
                for name in registry.names()}
    assert set(defaults.values()) == {512}

    selected = registry.get("coder-01-0004")
    selected.executor.execute(
        AgentRequest(task, TaskStatus.CODING, instructions="answer briefly")
    )
    assert fabric.calls[-1].max_output_tokens == 512

    tight = build_frontier_fleet(fabric, minimum_size=1000, max_output_tokens=48)
    tight.get("coder-01-0004").executor.execute(
        AgentRequest(task, TaskStatus.CODING, instructions="answer briefly")
    )
    assert fabric.calls[-1].max_output_tokens == 48
    assert tight.get("tester-01-0010").executor.max_output_tokens == 48

    # A nonsensical budget must not become an unbounded one.
    floor = build_frontier_fleet(fabric, minimum_size=1000, max_output_tokens=0)
    assert floor.get("coder-01-0004").executor.max_output_tokens >= 16


def test_specialist_prompt_carries_no_routing_metadata():
    """The model must receive the job, not Forge's internal routing facts.

    Specialists used to prepend "Preferred model target: …", "Route through
    the shared ModelFabric" and the declared capability list to every prompt.
    A model cannot act on any of that, it burns context, and small instruct
    models echo it back instead of answering. The same facts must still be
    present in the request metadata for routing and the audit trail.
    """
    fabric = _FakeFabric()
    registry = build_frontier_fleet(fabric, minimum_size=1000)
    task = TaskEngine().add("hygiene", "add pagination to the users endpoint")

    registry.get("backend-01-0006").executor.execute(
        AgentRequest(task, TaskStatus.CODING)
    )
    request = fabric.calls[-1]

    for leak in ("Preferred model target", "Route through the shared ModelFabric",
                 "Do not claim tool execution", "frontier-1000-plus",
                 "required capabilities"):
        assert leak not in request.prompt, leak
    # The job itself survives, and the identity is a role, not a routing table.
    assert "add pagination to the users endpoint" in request.prompt
    assert "backend" in request.prompt

    # Explicit instructions override the task text, as before.
    registry.get("backend-01-0006").executor.execute(
        AgentRequest(task, TaskStatus.CODING, instructions="answer briefly"))
    assert fabric.calls[-1].prompt.endswith("answer briefly")
    assert "Route through" not in fabric.calls[-1].prompt

    # Routing facts did not disappear; they moved to where routing reads them.
    assert request.metadata["preferred_model"]
    assert request.metadata["routing_capabilities"]
    assert request.metadata["agent"] == "backend-01-0006"
    assert request.caller == "frontier-agent:backend-01-0006"


def test_specialist_sampling_is_deterministic_by_default_and_configurable():
    fabric = _FakeFabric()
    registry = build_frontier_fleet(fabric, minimum_size=1000)
    task = TaskEngine().add("sampling", "answer the question")

    executor = registry.get("researcher-01-0003").executor
    assert executor.temperature == 0.2
    executor.execute(AgentRequest(task, TaskStatus.CODING, instructions="brief"))
    assert fabric.calls[-1].temperature == 0.2

    loose = build_frontier_fleet(fabric, minimum_size=1000, temperature=0.9)
    assert loose.get("researcher-01-0003").executor.temperature == 0.9
    unset = build_frontier_fleet(fabric, minimum_size=1000, temperature=None)
    unset.get("researcher-01-0003").executor.execute(
        AgentRequest(task, TaskStatus.CODING, instructions="brief"))
    assert fabric.calls[-1].temperature is None
