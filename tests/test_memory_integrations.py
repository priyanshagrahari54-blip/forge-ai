"""Long-term memory integration points: planner, context engine, debugger,
reviewer, model router, and the desktop backend memory viewer API."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from helpers_a34 import ScriptedProvider, make_fabric  # noqa: E402

from forge.agents.debugger import TestDebugLoop  # noqa: E402
from forge.agents.reviewer import ReviewerAgent  # noqa: E402
from forge.control.db import Database  # noqa: E402
from forge.core.planner import Planner  # noqa: E402
from forge.desktop_app.backend import DesktopBackend  # noqa: E402
from forge.intelligence.agent_context import AgentContextBuilder  # noqa: E402
from forge.intelligence.repository import RepositoryIntelligence  # noqa: E402
from forge.memory import LongTermMemory, MemoryType  # noqa: E402
from forge.memory.integrations import (  # noqa: E402
    memory_context_text,
    recall_for_planning,
    remember_failure,
    remember_model_performance,
)
from forge.models.request import ModelRequest  # noqa: E402
from forge.security.review import FindingSeverity, ReviewFinding  # noqa: E402


def make_store(tmp_path, project="demo"):
    return LongTermMemory(Database(tmp_path / "memory.db"), project=project)


# -- planner ---------------------------------------------------------------

def test_planner_prepends_recall_step_with_memory(tmp_path):
    store = make_store(tmp_path)
    store.remember(MemoryType.PROJECT,
                   "The cache module lives in cache.py and uses Redis.",
                   source="coder")
    plan = Planner().create_plan("fix the cache invalidation",
                                 memory=store, project="demo")
    assert plan[0].id == "0"
    assert "cache" in plan[0].description.lower()


def test_planner_without_memory_is_unchanged():
    plan = Planner().create_plan("add a feature")
    assert plan[0].id == "1"  # canonical template starts at step 1
    assert len(plan) == 5


# -- context engine ---------------------------------------------------------

def test_context_builder_injects_memory_items(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "cache.py").write_text("def get(key):\n    return None\n")
    store = make_store(tmp_path)
    store.remember(MemoryType.PROJECT,
                   "cache.py get() must fall back to the database.",
                   source="coder")
    intelligence = RepositoryIntelligence.build(root)
    context = AgentContextBuilder(intelligence).build(
        task="fix cache.py", memory=store, project="demo")
    memory_items = [item for item in context.pack.items
                    if item.kind == "memory"]
    assert memory_items
    assert all(item.path.startswith("memory:") for item in memory_items)


def test_context_builder_without_memory_has_no_memory_items(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "app.py").write_text("def health():\n    return True\n")
    intelligence = RepositoryIntelligence.build(root)
    context = AgentContextBuilder(intelligence).build(task="add a feature")
    assert all(item.kind != "memory" for item in context.pack.items)


# -- debugger ---------------------------------------------------------------

def test_debugger_records_failure_memory(tmp_path):
    store = make_store(tmp_path)
    TestDebugLoop._record_failure_memory(
        store, "demo", "t-1", "fix the bug", "assertion failed on line 3")
    results = store.search("assertion failed", memory_type=MemoryType.FAILURE)
    assert results
    assert results[0].record.source == "debugger"


def test_remember_failure_redacts_secrets(tmp_path):
    store = make_store(tmp_path)
    result = remember_failure(
        store, "demo", "failed with token=abcd1234efgh5678 in the log",
        task_id="t-1")
    assert result is not None
    stored = store.get(result.record.id)
    assert "abcd1234efgh5678" not in stored.content


# -- reviewer ---------------------------------------------------------------

def test_reviewer_records_blocking_findings_as_decisions(tmp_path):
    store = make_store(tmp_path)
    findings = [
        ReviewFinding(severity=FindingSeverity.HIGH,
                      message="unsafe shell=True in app.py",
                      file="app.py"),
        ReviewFinding(severity=FindingSeverity.INFO,
                      message="naming nit", file="app.py"),
    ]
    ReviewerAgent._record_findings(findings, store, "demo", "deploy feature")
    decisions = store.list(project="demo",
                           memory_type=MemoryType.DECISION)
    assert len(decisions) == 1
    assert "shell=True" in decisions[0].content


def test_reviewer_ignores_low_severity_findings(tmp_path):
    store = make_store(tmp_path)
    ReviewerAgent._record_findings(
        [ReviewFinding(severity=FindingSeverity.LOW, message="minor")],
        store, "demo", "task")
    assert store.list(project="demo", memory_type=MemoryType.DECISION) == []


# -- model router -----------------------------------------------------------

def test_fabric_records_model_performance_memory(tmp_path):
    store = make_store(tmp_path)
    fabric = make_fabric(ScriptedProvider())
    fabric.attach_memory(store, project="demo")
    response = fabric.generate(ModelRequest(prompt="reply ok",
                                            capability="coding"))
    assert response.success
    records = store.list(project="demo",
                         memory_type=MemoryType.MODEL_PERFORMANCE)
    assert records
    assert "m/a34" in records[0].content
    assert "ok" in records[0].content


def test_fabric_without_memory_records_nothing(tmp_path):
    store = make_store(tmp_path)
    fabric = make_fabric(ScriptedProvider())
    fabric.generate(ModelRequest(prompt="reply ok", capability="coding"))
    assert store.list(project="demo",
                      memory_type=MemoryType.MODEL_PERFORMANCE) == []


# -- integration helpers ----------------------------------------------------

def test_recall_and_render_helpers(tmp_path):
    store = make_store(tmp_path)
    store.remember(MemoryType.PROJECT, "Redis cache with 5 minute TTL.",
                   source="coder")
    recalled = recall_for_planning(store, "demo", "cache")
    assert recalled
    text = memory_context_text(recalled)
    assert "cache" in text.lower()


def test_remember_model_performance_roundtrip(tmp_path):
    store = make_store(tmp_path)
    result = remember_model_performance(
        store, "demo", "m/x", provider="p", capability="coding",
        success=False, latency_ms=1200.0, error="timeout")
    assert result is not None
    record = store.get(result.record.id)
    assert "m/x" in record.content
    assert "timeout" in record.content


# -- desktop backend ---------------------------------------------------------

@pytest.fixture()
def backend(tmp_path):
    root = tmp_path / "demo"
    root.mkdir()
    (root / "app.py").write_text("def health(): return True\n")
    app = DesktopBackend(actor="tester",
                         db_path=str(tmp_path / "desktop.db"),
                         fabric=make_fabric(ScriptedProvider()),
                         approval_timeout=30.0)
    app.start({"demo": str(root)})
    yield app
    app.stop()


def test_desktop_memory_viewer_api(backend):
    assert backend.list_memory("demo") == []
    backend._long_term_memory().remember(
        MemoryType.PROJECT, "The desktop viewer shows memory.", project="demo",
        source="desktop")
    listed = backend.list_memory("demo")
    assert len(listed) == 1
    assert listed[0]["type"] == "project"

    hits = backend.search_memory("demo", "viewer")
    assert hits and hits[0]["record"]["id"] == listed[0]["id"]

    stats = backend.memory_stats("demo")
    assert stats["total"] == 1

    deleted = backend.delete_memory("demo", listed[0]["id"])
    assert deleted["deleted"] is True
    assert backend.list_memory("demo") == []
