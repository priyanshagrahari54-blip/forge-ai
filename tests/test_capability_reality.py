from forge.capabilities.reality import (
    STATUS_ARCHITECTURE,
    STATUS_BLOCKED,
    STATUS_CONFIGURED,
    STATUS_LIVE,
    STATUS_SIMULATED,
    capability_snapshot,
    default_capabilities,
)


def test_snapshot_has_honesty_contract_and_stable_shape() -> None:
    snapshot = capability_snapshot()
    assert snapshot["schema_version"] == 1
    assert snapshot["honesty_contract"]["live_requires_verified"] is True
    assert snapshot["honesty_contract"]["configured_is_not_live"] is True
    assert snapshot["honesty_contract"]["simulation_is_never_live"] is True
    assert isinstance(snapshot["counts"], dict)
    assert isinstance(snapshot["capabilities"], list)
    assert snapshot["capabilities"]


def test_configured_provider_never_becomes_live_without_verification() -> None:
    caps = default_capabilities(
        provider_health={
            "openai": {
                "configured": True,
                "reachable": True,
                "verified": False,
            }
        }
    )
    item = caps["openai"]
    assert item.status == STATUS_CONFIGURED
    assert item.live is False


def test_verified_provider_is_live() -> None:
    caps = default_capabilities(
        provider_health={
            "ollama": {
                "configured": True,
                "reachable": True,
                "verified": True,
            }
        }
    )
    item = caps["ollama"]
    assert item.status == STATUS_LIVE
    assert item.live is True


def test_unreachable_configured_provider_is_blocked() -> None:
    caps = default_capabilities(
        provider_health={
            "openai": {
                "configured": True,
                "reachable": False,
                "verified": False,
            }
        }
    )
    assert caps["openai"].status == STATUS_BLOCKED


def test_architecture_and_simulation_are_not_live() -> None:
    caps = default_capabilities()
    assert caps["remote-compute"].status == STATUS_ARCHITECTURE
    assert caps["remote-compute"].live is False
    assert caps["voice"].status == STATUS_SIMULATED
    assert caps["voice"].live is False
