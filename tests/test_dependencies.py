from pathlib import Path

from forge.intelligence.dependencies import (
    DependencyGraph,
    DependencyIndexer,
)


def test_dependency_graph_queries():
    graph = DependencyGraph()

    graph.add("app.py", "models")
    graph.add("app.py", "utils")
    graph.add("service.py", "models")

    assert graph.dependencies_of("app.py") == [
        "models",
        "utils",
    ]

    assert graph.dependents_of("models") == [
        "app.py",
        "service.py",
    ]


def test_dependency_graph_deduplicates():
    graph = DependencyGraph()

    graph.add("app.py", "models")
    graph.add("app.py", "models")

    assert len(graph.dependencies) == 1


def test_dependency_indexer(tmp_path: Path):
    (tmp_path / ".gitignore").write_text(
        "__pycache__/\n",
        encoding="utf-8",
    )

    (tmp_path / "app.py").write_text(
        """
import os
import json
from forge.core import state
from forge.intelligence import symbols
""",
        encoding="utf-8",
    )

    graph = DependencyIndexer(tmp_path).build()

    dependencies = graph.dependencies_of("app.py")

    assert "os" in dependencies
    assert "json" in dependencies
    assert "forge.core" in dependencies
    assert "forge.intelligence" in dependencies
