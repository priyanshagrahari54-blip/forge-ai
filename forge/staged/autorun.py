"""Durable sequential stage runner for Forge City.

A City mission can be launched once and continue without a browser tab.
The runner's intent is persisted in the control-plane SQLite database so a
server restart can reconstruct the runner when its session is still active.
The actual stage work still runs through the existing ControlPlane pipeline;
this module only advances the next stage after the previous stage is verified.

Ordinary stages auto-advance after a configurable cooldown (five minutes by
default). Completion notification is best-effort and never changes execution
success/failure.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict

from forge.staged.models import StageStatus
from forge.staged.service import StagedBuilds

_LOCK_ATTR = "_forge_city_autorun_lock"
_RUNS_ATTR = "_forge_city_autoruns"
_TABLE_READY_ATTR = "_forge_city_autorun_table_ready"


def _state(plane: Any):
    lock = getattr(plane, _LOCK_ATTR, None)
    if lock is None:
        lock = threading.RLock()
        setattr(plane, _LOCK_ATTR, lock)
    runs = getattr(plane, _RUNS_ATTR, None)
    if runs is None:
        runs = {}
        setattr(plane, _RUNS_ATTR, runs)
    return lock, runs


def _ensure_store(plane: Any) -> None:
    if getattr(plane, _TABLE_READY_ATTR, False):
        return
    plane._db.execute(
        """
        CREATE TABLE IF NOT EXISTS staged_autoruns (
            key TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            build_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            mode TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1,
            updated_at REAL NOT NULL
        )
        """
    )
    plane._db.execute(
        "CREATE INDEX IF NOT EXISTS idx_staged_autoruns_active "
        "ON staged_autoruns(active, project_id)"
    )
    setattr(plane, _TABLE_READY_ATTR, True)


def _key(session: Any, build_id: str) -> str:
    return "%s:%s" % (session.project_id, build_id)


def _persist_start(plane: Any, session: Any, build_id: str, mode: str) -> None:
    _ensure_store(plane)
    plane._db.execute(
        "INSERT OR REPLACE INTO staged_autoruns "
        "(key, project_id, build_id, session_id, mode, active, updated_at) "
        "VALUES (?, ?, ?, ?, ?, 1, ?)",
        (_key(session, build_id), session.project_id, build_id,
         session.id, mode or "", time.time()))


def _persist_stop(plane: Any, session: Any, build_id: str) -> None:
    _ensure_store(plane)
    plane._db.execute(
        "UPDATE staged_autoruns SET active = 0, updated_at = ? "
        "WHERE key = ?",
        (time.time(), _key(session, build_id)))


def forget(plane: Any, session: Any, build_id: str) -> Dict[str, Any]:
    """Disable persisted auto-run intent for one build."""
    StagedBuilds(plane).get_board(session, build_id)
    _persist_stop(plane, session, build_id)
    lock, runs = _state(plane)
    key = _key(session, build_id)
    with lock:
        thread = runs.get(key)
        running = bool(thread and thread.is_alive())
    return {"build_id": build_id, "forgotten": True,
            "running": running,
            "note": "current runner continues" if running else ""}


def start(plane: Any, session: Any, build_id: str, *, mode: str = "") -> Dict[str, Any]:
    """Start a non-blocking sequential runner and persist its intent."""
    service = StagedBuilds(plane)
    board = service.get_board(session, build_id)
    if board.get("all_complete"):
        _persist_stop(plane, session, build_id)
        return {"build_id": build_id, "started": False, "status": "already_complete"}
    lock, runs = _state(plane)
    key = _key(session, build_id)
    with lock:
        existing = runs.get(key)
        if existing and existing.is_alive():
            _persist_start(plane, session, build_id, mode)
            return {"build_id": build_id, "started": False,
                    "status": "already_running"}
        _persist_start(plane, session, build_id, mode)
        thread = threading.Thread(
            target=_loop,
            args=(plane, session, build_id, mode),
            name="forge-city-%s" % build_id[:12],
            daemon=True,
        )
        runs[key] = thread
        thread.start()
    return {"build_id": build_id, "started": True, "status": "running"}


def resume_active(plane: Any) -> Dict[str, Any]:
    """Reconstruct persisted autorunners whose sessions are still active."""
    from forge.control.checkpoint_recovery import restore_available_checkpoints

    restore_available_checkpoints(plane)
    _ensure_store(plane)
    rows = plane._db.query(
        "SELECT * FROM staged_autoruns WHERE active = 1 "
        "ORDER BY updated_at ASC"
    )
    resumed: list[str] = []
    skipped: list[str] = []
    for row in rows:
        session = plane.sessions.get(row["session_id"])
        if session is None or not session.active:
            plane._db.execute(
                "UPDATE staged_autoruns SET active = 0, updated_at = ? "
                "WHERE key = ?",
                (time.time(), row["key"]))
            skipped.append(row["build_id"])
            continue
        try:
            board = StagedBuilds(plane).get_board(session, row["build_id"])
            if board.get("all_complete"):
                _persist_stop(plane, session, row["build_id"])
                continue
            result = start(plane, session, row["build_id"], mode=row["mode"] or "")
            if result.get("started") or result.get("status") == "already_running":
                resumed.append(row["build_id"])
        except Exception:
            skipped.append(row["build_id"])
    return {"resumed": resumed, "skipped": skipped}


def status(plane: Any, session: Any, build_id: str) -> Dict[str, Any]:
    """Return live and persisted autorun state for a build."""
    StagedBuilds(plane).get_board(session, build_id)
    _ensure_store(plane)
    lock, runs = _state(plane)
    key = _key(session, build_id)
    with lock:
        thread = runs.get(key)
        row = plane._db.query_one(
            "SELECT active, updated_at FROM staged_autoruns WHERE key = ?",
            (key,))
        return {"build_id": build_id,
                "running": bool(thread and thread.is_alive()),
                "persistent": bool(row and row["active"])}


def _notify_complete(plane: Any, session: Any, build_id: str, board: Dict[str, Any]) -> None:
    """Send an optional completion email without affecting the run."""
    try:
        from forge.notifications.email import notify_build_complete
        notify_build_complete(
            project_id=session.project_id,
            build_id=build_id,
            stages=board.get("stages") or [],
        )
    except Exception:
        # Notification is auxiliary. Never turn a successful build into a
        # failed build because email configuration is absent or unavailable.
        return


def _loop(plane: Any, session: Any, build_id: str, mode: str) -> None:
    service = StagedBuilds(plane)
    key = _key(session, build_id)
    try:
        while True:
            board = service.get_board(session, build_id)
            if board.get("all_complete"):
                _persist_stop(plane, session, build_id)
                _notify_complete(plane, session, build_id, board)
                return
            current = board.get("current_position")
            if current is None:
                _persist_stop(plane, session, build_id)
                return
            stages = board.get("stages") or []
            current_stage = next(
                (item for item in stages if item.get("position") == current), None)
            if current_stage and current_stage.get("status") == StageStatus.FAILED.value:
                _persist_stop(plane, session, build_id)
                return
            if current_stage and current_stage.get("status") == StageStatus.RUNNING.value:
                time.sleep(1.0)
                continue

            try:
                service.run_next(session, build_id, mode=mode)
            except Exception:
                time.sleep(1.0)
    finally:
        lock, runs = _state(plane)
        with lock:
            thread = runs.get(key)
            if thread is threading.current_thread():
                runs.pop(key, None)
