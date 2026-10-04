from forge.capabilities.discovery import CapabilityDiscovery
from forge.capabilities.registry import CapabilityRegistry


def test_unverified_mcp_candidates_are_not_registered(monkeypatch):
    discovery = CapabilityDiscovery(CapabilityRegistry())
    monkeypatch.setattr(discovery, "_json", lambda _: {"servers": [{"server": {"name": "x", "version": "1"}}]})
    found = discovery.discover_mcp("test")
    assert len(found) == 1
    assert not found[0].usable
    assert discovery.registry.all() == []


def test_hf_model_metadata_can_be_discovered(monkeypatch):
    discovery = CapabilityDiscovery(CapabilityRegistry())
    monkeypatch.setattr(discovery, "_json", lambda _: [{"id": "org/model", "sha": "abc"}])
    found = discovery.discover_huggingface_models("model")
    assert found[0].name == "org/model"
    assert found[0].usable
