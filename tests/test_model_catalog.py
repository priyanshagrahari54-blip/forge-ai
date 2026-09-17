from forge.models.model_catalog import PROVIDER_ECOSYSTEMS, catalog, catalog_snapshot


def test_catalog_has_provider_ecosystems_without_claiming_live_models():
    entries = catalog()
    assert len(PROVIDER_ECOSYSTEMS) >= 20
    assert entries
    assert all(item.status == "unverified" for item in entries)
    assert all(item.model.endswith(":runtime-discovery") for item in entries)


def test_catalog_snapshot_declares_runtime_verification_policy():
    snapshot = catalog_snapshot()
    assert snapshot["schema_version"] == 1
    assert snapshot["provider_count"] == len(PROVIDER_ECOSYSTEMS)
    assert snapshot["availability_policy"] == "runtime-verified-only"
