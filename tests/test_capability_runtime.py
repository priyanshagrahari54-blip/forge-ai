from forge.capabilities.runtime import provider_health_from_fabric, runtime_capability_snapshot


class _Provider:
    def __init__(self, name, payload):
        self.name = name
        self._payload = payload

    def health(self):
        return self._payload


class _Fabric:
    def __init__(self, providers):
        self._providers = providers

    def providers(self):
        return self._providers


def test_runtime_adapter_uses_explicit_provider_health() -> None:
    fabric = _Fabric({
        "openai": _Provider("openai", {
            "configured": True, "reachable": True, "verified": True
        }),
        "ollama": _Provider("ollama", {
            "available": False, "configured": True, "verified": False
        }),
    })
    health = provider_health_from_fabric(fabric)
    assert health["openai"]["verified"] is True
    assert health["ollama"]["reachable"] is False
    assert health["ollama"]["verified"] is False


def test_runtime_snapshot_never_invents_provider_readiness() -> None:
    fabric = _Fabric({
        "openai": _Provider("openai", {"configured": True, "available": False})
    })
    snapshot = runtime_capability_snapshot(fabric)
    openai = next(item for item in snapshot["capabilities"]
                  if item["capability_id"] == "openai")
    assert openai["verified"] is False
    assert openai["live"] is False
