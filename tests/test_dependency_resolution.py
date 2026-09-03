from pathlib import Path

from forge.intelligence.dependencies import DependencyIndexer


def test_resolves_module_file(tmp_path: Path):
    (tmp_path / "forge").mkdir()
    (tmp_path / "forge" / "core.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )

    indexer = DependencyIndexer(tmp_path)

    kind, resolved = indexer._resolve_import("forge.core")

    assert kind == "internal"
    assert resolved == "forge/core.py"


def test_resolves_package(tmp_path: Path):
    package = tmp_path / "forge" / "core"
    package.mkdir(parents=True)

    (package / "__init__.py").write_text(
        "",
        encoding="utf-8",
    )

    indexer = DependencyIndexer(tmp_path)

    kind, resolved = indexer._resolve_import("forge.core")

    assert kind == "internal"
    assert resolved == "forge/core/__init__.py"


def test_resolves_from_import_module(tmp_path: Path):
    package = tmp_path / "forge" / "core"
    package.mkdir(parents=True)

    (package / "__init__.py").write_text(
        "",
        encoding="utf-8",
    )

    (package / "state.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )

    indexer = DependencyIndexer(tmp_path)

    kind, resolved = indexer._resolve_import("forge.core.state")

    assert kind == "internal"
    assert resolved == "forge/core/state.py"


def test_keeps_external_import_external(tmp_path: Path):
    indexer = DependencyIndexer(tmp_path)

    kind, resolved = indexer._resolve_import("requests")

    assert kind == "external"
    assert resolved is None


def test_indexer_resolves_internal_imports(tmp_path: Path):
    package = tmp_path / "forge" / "core"
    package.mkdir(parents=True)

    (tmp_path / "forge" / "__init__.py").write_text(
        "",
        encoding="utf-8",
    )

    (package / "__init__.py").write_text(
        "",
        encoding="utf-8",
    )

    (package / "state.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )

    app = tmp_path / "app.py"
    app.write_text(
        "from forge.core import state\n",
        encoding="utf-8",
    )

    graph = DependencyIndexer(tmp_path).build()

    internal = graph.internal_dependencies_of("app.py")

    assert len(internal) == 1
    assert internal[0].kind == "internal"
    assert internal[0].resolved_path == "forge/core/state.py"
