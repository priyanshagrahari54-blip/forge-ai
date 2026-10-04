from forge.control.database_backends import DatabaseBackendCatalog

def test_sqlite_is_the_only_required_default_backend():
    catalog = DatabaseBackendCatalog()
    assert catalog.choose_default().name == "sqlite"
    assert catalog.available(embedded_only=True)[0].name == "sqlite"
