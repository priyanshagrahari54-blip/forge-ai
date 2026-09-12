"""Native AI startup, status surface, snapshot persistence, capabilities."""
from __future__ import annotations

import json
import time
from pathlib import Path

from helpers_native_ai import write_repo

from forge.native.capabilities import (
    CAPABILITIES,
    capability_for,
    free_capabilities,
    neural_capabilities,
    requires_neural,
)
from forge.native.engine import NativeAIEngine
from forge.native.state import (
    EngineState,
    FRESHNESS_SECONDS,
    NativeStatusSnapshot,
    TaskState,
    read_snapshot,
    snapshot_age_seconds,
    snapshot_is_fresh,
    write_snapshot,
)

DOCS = Path(__file__).resolve().parent.parent / "docs" / \
    "A81-NATIVE-AI-ENGINE.md"


def test_engine_starts_idle_with_complete_status(tmp_path):
    write_repo(tmp_path)
    engine = NativeAIEngine(tmp_path, project="p", persist_status=False)
    status = engine.status()
    assert status["engine"] == "forge-native-ai"
    assert status["state"]["engine_state"] == EngineState.IDLE.value
    assert status["state"]["task"]["state"] == TaskState.NONE.value
    assert status["mode"] == "assisted"
    assert "reasoning" in status and "model_backend" in status
    assert [c["id"] for c in status["capabilities"]] == \
        [c.id for c in CAPABILITIES]


def test_status_without_fabric_reports_honestly(tmp_path):
    write_repo(tmp_path)
    engine = NativeAIEngine(tmp_path, persist_status=False)
    state = engine.status()["model_backend"]
    assert state["configured"] is False
    assert "refused" in state["detail"]
    reasoning = engine.status()["reasoning"]
    assert reasoning["generative_ready"] is False
    assert reasoning["generative_backend"] is None
    assert reasoning["active_backend"]["name"] == "native-deterministic"


def test_status_with_fabric_marks_registration_as_unverified(tmp_path):
    from helpers_native_ai import scripted_fabric

    write_repo(tmp_path)
    engine = NativeAIEngine(tmp_path, fabric=scripted_fabric({}),
                            persist_status=False)
    state = engine.status()["model_backend"]
    assert state["configured"] is True and state["real_model"] is True
    assert "scripted-model" in state["models"]
    # Registration is never presented as verified liveness:
    assert "unverified" in state["live"] or "evidence_from_calls" in \
        state["live"]
    assert engine.status()["reasoning"]["generative_ready"] is True


def test_snapshot_roundtrip_atomic_and_readable(tmp_path):
    snapshot = NativeStatusSnapshot(project="p", root=str(tmp_path),
                                     engine_state=EngineState.VERIFYING.value,
                                     stage="test", stage_index=3,
                                     stage_total=7)
    path = write_snapshot(tmp_path, snapshot)
    assert path.is_file() and not (tmp_path / ".forge/native/state.json.tmp"
                                   ).exists()
    data = read_snapshot(tmp_path)
    assert data["stage"] == {"kind": "test", "index": 3, "total": 7}
    assert data["version"] == 1
    restored = NativeStatusSnapshot.from_dict(data)
    assert restored.engine_state == EngineState.VERIFYING.value
    assert restored.task_state == TaskState.NONE.value


def test_snapshot_corrupt_file_reads_as_absent(tmp_path):
    target = tmp_path / ".forge" / "native"
    target.mkdir(parents=True)
    (target / "state.json").write_text("{not json", encoding="utf-8")
    assert read_snapshot(tmp_path) is None


def test_snapshot_age_and_freshness(tmp_path):
    snapshot = NativeStatusSnapshot(updated_at=time.time() - 5)
    write_snapshot(tmp_path, snapshot)
    data = read_snapshot(tmp_path)
    assert 4 < snapshot_age_seconds(data, now=time.time()) < 60
    assert snapshot_is_fresh(data)
    stale = dict(data, updated_at=time.time() - FRESHNESS_SECONDS - 60)
    assert not snapshot_is_fresh(stale)
    assert snapshot_age_seconds({"updated_at": 0}) == float("inf")


def test_tracker_persists_state_transitions(tmp_path):
    write_repo(tmp_path)
    engine = NativeAIEngine(tmp_path, persist_status=True)
    engine.tracker.set_state(EngineState.PLANNING)
    data = read_snapshot(tmp_path)
    assert data is not None
    assert data["engine_state"] == "planning"
    engine.tracker.set_retry(2, 3, True, "assertion")
    assert read_snapshot(tmp_path)["retry"] == {
        "cycle": 2, "max": 3, "active": True, "last_reason": "assertion"}
    engine.tracker.record_files_changed(["a.py", "b.py", "a.py"])
    assert read_snapshot(tmp_path)["files_changed"] == ["a.py", "b.py"]


def test_capability_matrix_labels_are_complete_and_unique():
    ids = [c.id for c in CAPABILITIES]
    assert len(ids) == len(set(ids))
    for capability in CAPABILITIES:
        assert capability.label
        if capability.requires_neural:
            assert capability in neural_capabilities()
        else:
            assert capability in free_capabilities()
    assert requires_neural("code_generation") is True
    assert requires_neural("repository_analysis") is False
    # unknown ids fail closed toward "needs a model"
    assert requires_neural("does-not-exist") is True
    assert capability_for("memory") is not None


def test_capability_matrix_is_documented_in_a81_docs():
    if not DOCS.exists():
        raise AssertionError("docs/A81-NATIVE-AI-ENGINE.md must document the "
                             "engine (it is part of A81)")
    text = DOCS.read_text(encoding="utf-8")
    for capability in CAPABILITIES:
        assert capability.id in text, \
            "capability %r missing from A81 docs" % capability.id


def test_selftest_module_passes_all_checks():
    from forge.native.selftest import run_native_selftest

    report = run_native_selftest()
    assert report["passed"], json.dumps(report["checks"], indent=1)[:2000]
    assert report["passed_count"] == report["total"] >= 10
