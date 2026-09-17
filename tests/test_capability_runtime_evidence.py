from forge.capabilities.runtime import provider_health_from_fabric
from forge.capabilities.reality import STATUS_BLOCKED, STATUS_LIVE, capability_snapshot


class _Provider:
    def __init__(self, payload):
        self._payload = payload

    def health(self):
        return self._payload


class _Fabric:
    def __init__(self, providers):
        self.providers = providers


def test_fabric_health_is_converted_without_inventing_verification():
    health = provider_health_from_fabric(_Fabric({
        "openai": _Provider({"configured": True, "available": True}),
        "ollama": _Provider({"configured": True, "available": False}),
    }))
    assert health["openai"]["configured"] is True
    assert health["openai"]["reachable"] is True
    assert health["openai"]["verified"] is False
    assert health["ollama"]["reachable"] is False


def test_snapshot_keeps_unverified_provider_non_live():
    snapshot = capability_snapshot(provider_health={
        "openai": {"configured": True, "reachable": True, "verified": False},
    })
    openai = next(item for item in snapshot["capabilities"]
                  if item["capability_id"] == "openai")
    assert openai["status"] != STATUS_LIVE
    assert openai["status"] == "CONFIGURED"


def test_snapshot_marks_verified_reachable_provider_live():
    snapshot = capability_snapshot(provider_health={
        "openai": {"configured": True, "reachable": True, "verified": True},
    })
    openai = next(item for item in snapshot["capabilities"]
                  if item["capability_id"] == "openai")
    assert openai["status"] == STATUS_LIVE


def test_unreachable_provider_is_blocked():
    snapshot = capability_snapshot(provider_health={
        "ollama": {"configured": True, "reachable": False, "verified": False},
    })
    ollama = next(item for item in snapshot["capabilities"]
                  if item["capability_id"] == "ollama")
    assert ollama["status"] == STATUS_BLOCKED
