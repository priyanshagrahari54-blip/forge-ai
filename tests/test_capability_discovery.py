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
    assert not found[0].usable
    assert found[0].verification_status == "unverified"
    assert found[0].security_status == "unverified"


def test_hf_dataset_and_space_metadata_can_be_discovered(monkeypatch):
    discovery = CapabilityDiscovery(CapabilityRegistry())
    monkeypatch.setattr(discovery, "_json", lambda url: [{"id": "org/repo", "sha": "abc"}])
    datasets = discovery.discover_huggingface_datasets("data")
    spaces = discovery.discover_huggingface_spaces("app")
    assert datasets[0].capability == "dataset"
    assert spaces[0].capability == "space"
    assert datasets[0].usable and spaces[0].usable
