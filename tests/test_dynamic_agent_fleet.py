from forge.agents.factory import AgentFactory


def test_runtime_agent_factory_has_no_fixed_product_ceiling():
    factory = AgentFactory("dynamic")
    for index in range(64):
        agent = factory.create(
            f"specialist-{index:02d}",
            "coding",
            ("coding",),
        )
        assert agent.status == "active"
    assert len(factory.list()) == 64


def test_runtime_limit_is_optional_policy_not_product_limit():
    factory = AgentFactory("bounded", max_agents=2)
    factory.create("one", "coding", ("coding",))
    factory.create("two", "coding", ("coding",))
    try:
        factory.create("three", "coding", ("coding",))
    except ValueError as exc:
        assert "Agent limit reached (2)" in str(exc)
    else:
        raise AssertionError("expected optional runtime policy limit")
