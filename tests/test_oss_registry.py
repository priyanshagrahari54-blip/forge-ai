from forge.models.oss_registry import VERIFIED_SEEDS, OSSModel, oss_catalog, oss_catalog_snapshot
from forge.agents.oss_adapters import OSS_AGENT_ADAPTERS, agent_adapter_catalog


def test_real_seed_ids_are_concrete_and_unique():
    ids = [item.model_id for item in VERIFIED_SEEDS]
    assert len(ids) == len(set(ids))
    assert all("/" in model_id for model_id in ids)
    assert not any(model_id.endswith(":runtime-discovery") for model_id in ids)


def test_discovered_models_are_deduplicated():
    item = OSSModel("example/real-model", "example", "huggingface", status="discovered")
    result = oss_catalog(discovered=[item, item])
    assert [x.model_id for x in result].count("example/real-model") == 1


def test_1000_target_is_not_falsely_claimed_without_discovery():
    snapshot = oss_catalog_snapshot()
    assert snapshot["real_model_count"] < 1000
    assert snapshot["meets_1000_target"] is False


def test_agent_adapter_catalog_is_real_project_metadata():
    names = {item.name for item in OSS_AGENT_ADAPTERS}
    assert {"OpenHands", "SWE-agent", "Aider", "Goose", "OpenCode", "Cline"} <= names
    assert all(item.repository.count("/") == 1 for item in OSS_AGENT_ADAPTERS)
    assert len(agent_adapter_catalog()) == len(OSS_AGENT_ADAPTERS)
