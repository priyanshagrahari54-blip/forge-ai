from forge.models.router import ModelInfo, ModelRouter


def test_model_router_advanced_filtering():
    router = ModelRouter()

    m1 = ModelInfo(
        name="local-llama",
        capability="coding",
        available=True,
        context_size=8192,
        historical_success_rate=0.9,
        latency=0.5,
        task_complexity=1.0,
    )
    m2 = ModelInfo(
        name="local-qwen",
        capability="coding",
        available=True,
        context_size=16384,
        historical_success_rate=0.98,
        latency=0.3,
        task_complexity=2.0,
    )
    m3 = ModelInfo(
        name="unavailable-model",
        capability="coding",
        available=False,
    )

    router.register(m1)
    router.register(m2)
    router.register(m3)

    # Standard selection backward compatibility
    selected = router.select("coding")
    assert selected is not None
    # Should pick higher historical success rate m2
    assert selected.name == "local-qwen"

    # Selection with min_context_size filter
    selected_large = router.select("coding", min_context_size=10000)
    assert selected_large is not None
    assert selected_large.name == "local-qwen"

    # Selection with high complexity requirement
    selected_complex = router.select("coding", task_complexity=1.5)
    assert selected_complex is not None
    assert selected_complex.name == "local-qwen"
