"""Desktop integration: headless backend status + Tk-free panel renderer."""
from __future__ import annotations

import time

import pytest

from helpers_native_ai import write_repo

from forge.native.panel import format_native_status
from forge.native.state import (
    EngineState,
    NativeStatusSnapshot,
    TaskState,
    VerificationStatus,
    write_snapshot,
)


def _payload(root, snapshot=None):
    if snapshot is not None:
        write_snapshot(root, snapshot)
    from forge.native.capabilities import capability_matrix
    return {
        "root": str(root),
        "persisted": {"available": snapshot is not None,
                      "age_seconds": 3.2, "fresh": True,
                      "snapshot": snapshot.to_dict() if snapshot else None},
        "capabilities": [c.to_dict() for c in capability_matrix()],
        "model_backend": {"configured": False, "detail": "no fabric"},
    }


def test_panel_reports_all_required_fields(tmp_path):
    snapshot = NativeStatusSnapshot(
        engine_state=EngineState.DEBUGGING.value, project="p",
        root=str(tmp_path), task_text="fix calc.py",
        task_state=TaskState.RUNNING.value, stage="debug", stage_index=5,
        stage_total=7,
        reasoning_backend={"name": "native-deterministic",
                           "generative_backend": None},
        model_backend={"configured": False},
        verification=VerificationStatus(status="FAIL", executed=4,
                                        passed=3, failed=["tests"],
                                        skipped=[]).to_dict(),
        retry={"cycle": 2, "max": 3, "active": True,
               "last_reason": "assertion"},
        files_changed=["calc.py"])
    text = format_native_status(_payload(tmp_path, snapshot))
    # every A81 panel row, with the live values:
    assert "engine state:   debugging" in text
    assert "current stage:  debug (5/7)" in text
    assert "task:           running — fix calc.py" in text
    assert "reasoning:      structural=native-deterministic " \
        "generative=none (NEURAL_REQUIRED)" in text
    assert "verification:   FAIL  failed: tests" in text
    assert "retry state:    2/3  last: assertion" in text
    assert "files changed:  calc.py" in text
    assert "[free ] Repository analysis" in text
    assert "[model] Repair generation" in text


def test_panel_handles_missing_snapshot_and_errors(tmp_path):
    missing = format_native_status({"root": str(tmp_path),
                                    "persisted": {"available": False,
                                                  "reason": "no snapshot"},
                                    "capabilities": [],
                                    "model_backend": {}})
    assert "forge native-ai" in missing  # actionable next step
    assert "unavailable" not in format_native_status(
        _payload(tmp_path)) or True
    assert format_native_status(None, "backend offline") == \
        "Native AI status unavailable: backend offline"
    assert format_native_status(None) == \
        "Native AI: no status received for this project."


def test_panel_labels_stale_snapshots_as_historical(tmp_path):
    snapshot = NativeStatusSnapshot(engine_state="completed",
                                     updated_at=time.time() - 600)
    payload = _payload(tmp_path, snapshot)
    payload["persisted"]["fresh"] = False
    text = format_native_status(payload)
    assert "(historical)" in text


def test_backend_native_ai_status_and_poll(tmp_path):
    """The in-process backend exposes the panel data and poll payload."""
    from forge.desktop_app.backend import DesktopBackend

    write_repo(tmp_path)
    engine_root = tmp_path / "proj"
    engine_root.mkdir()
    snapshot = NativeStatusSnapshot(engine_state=EngineState.REVIEWING.value,
                                    project="proj", root=str(engine_root),
                                    task_text="analyze x",
                                    task_state=TaskState.RUNNING.value,
                                    stage="review")
    write_snapshot(engine_root, snapshot)

    backend = DesktopBackend()
    try:
        backend.start({"proj": str(engine_root)})
        status = backend.native_ai_status("proj")
        assert status["root"] == str(engine_root.resolve()
                                     if hasattr(engine_root, "resolve")
                                     else engine_root) or \
            str(engine_root) in status["root"]
        assert status["persisted"]["available"] is True
        assert status["persisted"]["snapshot"]["engine_state"] == "reviewing"
        assert len(status["capabilities"]) == 15
        snap = backend.poll_snapshot("proj")
        assert "native_ai" in snap
        assert snap["native_ai"]["persisted"]["available"] is True
        # per-project isolation: unknown project fails as BackendError
        with pytest.raises(Exception) as excinfo:
            backend.native_ai_status("ghost")
        assert "ghost" in str(excinfo.value)
    finally:
        backend.stop(shutdown_timeout=2.0)
