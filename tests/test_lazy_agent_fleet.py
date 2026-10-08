from forge.agents.frontier_fleet import build_frontier_fleet


class _Fabric:
    pass


def test_frontier_fleet_can_be_lazy_and_role_scoped():
    registry = build_frontier_fleet(
        _Fabric(),
        requested_roles=("security", "research"),
    )
    roles = set(registry.roles())
    assert roles == {"security", "research"}
    assert len(registry) == 2


def test_explicit_catalog_mode_remains_available():
    registry = build_frontier_fleet(_Fabric(), minimum_size=1000)
    assert len(registry) >= 1000
