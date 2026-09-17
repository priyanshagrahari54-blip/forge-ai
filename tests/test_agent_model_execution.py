from forge.agents.fleet import AgentSlot
from forge.agents.model_execution import AgentModelExecutor
from forge.models.registry import Model, ModelRegistry


def slot():
    return AgentSlot(
        name="backend-builder",
        role="builder",
        domain="backend",
        specialty="builder",
        capabilities=("coding",),
        priority=1,
    )


def verified(name, provider):
    return Model(
        name=name,
        provider=provider,
        capabilities=("coding",),
        available=True,
        local=False,
        free=True,
        metadata={"runtime_verified": True},
    )


def test_agent_routes_only_to_verified_model():
    registry = ModelRegistry([verified("model-a", "provider-a")])
    executor = AgentModelExecutor(registry)
    result = executor.execute(slot(), operation=lambda model, provider, request: model)
    assert result.success
    assert result.selected_model == "model-a"
    assert result.attempts[0].provider == "provider-a"


def test_unverified_model_is_not_executed():
    registry = ModelRegistry([
        Model("model-a", "provider-a", capabilities=("coding",), available=True, metadata={})
    ])
    executor = AgentModelExecutor(registry)
    result = executor.execute(slot(), operation=lambda *args: "must-not-run")
    assert not result.success
    assert result.attempts == []


def test_retryable_failure_fails_over_to_next_provider():
    registry = ModelRegistry([
        verified("model-a", "provider-a"),
        verified("model-b", "provider-b"),
    ])
    executor = AgentModelExecutor(registry)
    calls = []

    class RetryableError(RuntimeError):
        reason = "rate_limited"

    def operation(model, provider, request):
        calls.append(provider)
        if provider == "provider-a":
            raise RetryableError("quota window")
        return "ok"

    result = executor.execute(slot(), operation=operation)
    assert result.success
    assert result.value == "ok"
    assert calls == ["provider-a", "provider-b"]
    assert result.attempts[0].reason == "rate_limited"
    assert result.attempts[1].success


def test_failover_does_not_bypass_non_retryable_error():
    registry = ModelRegistry([
        verified("model-a", "provider-a"),
        verified("model-b", "provider-b"),
    ])
    executor = AgentModelExecutor(registry)
    calls = []

    class AuthError(RuntimeError):
        reason = "authentication"

    def operation(model, provider, request):
        calls.append(provider)
        raise AuthError("bad credential")

    result = executor.execute(slot(), operation=operation)
    assert not result.success
    assert calls == ["provider-a"]
    assert len(result.attempts) == 1
