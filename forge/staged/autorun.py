"""Persistent-in-process sequential stage runner for Forge City.

A City mission can be launched once and then continue without a browser tab.
The actual work still runs through the existing ControlPlane worker pipeline;
this module only advances the next stage after the previous stage has been
verified. It deliberately stops on a failed stage so a bad stage cannot cause
the remaining stages to run blindly.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict

from forge.staged.models import StageStatus
from forge.staged.service import StagedBuilds


_LOCK_ATTR = "_forge_city_autorun_lock"
_RUNS_ATTR = "_forge_city_autoruns"


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


def start(plane: Any, session: Any, build_id: str, *, mode: str = "") -> Dict[str, Any]:
    """Start a non-blocking sequential runner and return its state."""
    lock, runs = _state(plane)
    key = "%s:%s" % (session.project_id, build_id)
    with lock:
        existing = runs.get(key)
        if existing and existing.is_alive():
            return {"build_id": build_id, "started": False,
                    "status": "already_running"}
        thread = threading.Thread(
            target=_loop,
            args=(plane, session, build_id, mode),
            name="forge-city-%s" % build_id[:12],
            daemon=True,
        )
        runs[key] = thread
        thread.start()
    return {"build_id": build_id, "started": True, "status": "running"}


def status(plane: Any, session: Any, build_id: str) -> Dict[str, Any]:
    lock, runs = _state(plane)
    key = "%s:%s" % (session.project_id, build_id)
    with lock:
        thread = runs.get(key)
        return {"build_id": build_id,
                "running": bool(thread and thread.is_alive())}


def _loop(plane: Any, session: Any, build_id: str, mode: str) -> None:
    service = StagedBuilds(plane)
    key = "%s:%s" % (session.project_id, build_id)
    try:
        while True:
            board = service.get_board(session, build_id)
            if board.get("all_complete"):
                return
            current = board.get("current_position")
            if current is None:
                return
            stages = board.get("stages") or []
            current_stage = next(
                (item for item in stages if item.get("position") == current), None)
            if current_stage and current_stage.get("status") == StageStatus.FAILED.value:
                # Stop rather than silently retrying a verified failure.
                return
            if current_stage and current_stage.get("status") == StageStatus.RUNNING.value:
                time.sleep(1.0)
                continue
            try:
                service.run_next(session, build_id, mode=mode)
            except Exception:
                # The board is the source of truth; transient conflicts are
                # retried, but terminal stage failures stop the loop above.
                time.sleep(1.0)
    finally:
        lock, runs = _state(plane)
        with lock:
            thread = runs.get(key)
            if thread is threading.current_thread():
                runs.pop(key, None)
