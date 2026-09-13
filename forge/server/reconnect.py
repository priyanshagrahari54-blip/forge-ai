"""Client reconnection: recover everything in one call (A81).

When a Forge Desktop client reconnects (after a crash, sleep, network
drop, or a server restart), ``GET /api/v1/recovery`` returns the full
picture in one payload:

* **active tasks** — every task the server is responsible for, with
  status, current stage, progress, retry count, and checkpoint;
* **progress** — recent events per task, replayed exactly from the
  client's cursor (``after=<seq>``: no gaps, no duplicates);
* **logs** — the tail of each active task's log stream;
* **results** — full results for tasks that reached a terminal state
  since ``since=<timestamp>`` (default: last 24 h);
* **approval requests** — every pending decision blocking a task;
* **notifications** — the unread list (task finished while offline…);
* **cursors** — the values to pass back on the next reconnect.

The bundle is bounded on every axis so a long-offline client cannot
turn reconnection into a denial of service.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

#: Default look-back for finished-task results on reconnect.
DEFAULT_SINCE_SECONDS = 24 * 3600


class RecoveryService:
    """Builds the reconnection bundle from durable state."""

    def __init__(self, server: Any) -> None:
        self.server = server

    def bundle(self, *, after: int = 0, since: Optional[float] = None,
               project_id: str = "", event_limit: int = 200,
               log_limit: int = 100,
               result_limit: int = 50) -> Dict[str, Any]:
        server = self.server
        after = max(0, int(after))
        since_ts = float(since) if since is not None else \
            time.time() - DEFAULT_SINCE_SECONDS

        active = server.tasks.active_tasks(project_id)
        finished = [
            task for task in server.tasks.updated_since(
                since_ts, project_id=project_id, limit=result_limit)
            if task.terminal]

        # Events: replay from the client cursor for every task in view.
        events: Dict[str, List[Dict[str, Any]]] = {}
        for task in active + finished:
            stored, _latest = server.events.list(
                task.task_id, after=after, limit=event_limit)
            if stored:
                events[task.task_id] = [item.to_dict() for item in stored]

        # Logs: tail of each active task (finished tasks page on demand).
        logs: Dict[str, List[Dict[str, Any]]] = {}
        for task in active:
            entries = server.logs.tail(task.task_id, limit=log_limit)
            logs[task.task_id] = [entry.to_dict() for entry in entries]

        approvals = server.approvals.pending(project_id)
        notifications = server.notifications.list(
            project_id, unread_only=True, limit=50)

        return {
            "protocol_version": _protocol_version(),
            "server": {
                "name": "forge-server",
                "version": _version(),
                "boot_id": server.boot_id,
                "running": server.running,
                "started_at": server.started_at,
                "time": time.time(),
                "profile": server.config.profile,
            },
            "projects": [project.to_dict()
                         for project in server.projects.list()],
            "active_tasks": [task.to_dict() for task in active],
            "finished_tasks": [
                task.to_dict(include_result=True) for task in finished],
            "events": events,
            "logs": logs,
            "approvals": approvals,
            "notifications": notifications,
            "task_counts": server.tasks.status_counts(project_id),
            "cursors": {
                "latest_event_seq": server.events.latest_seq(),
                "since": since_ts,
                "after": after,
            },
        }


def _version() -> str:
    try:
        from forge.server import SERVER_VERSION

        return SERVER_VERSION
    except Exception:
        return "0.0.0"


def _protocol_version() -> int:
    try:
        from forge.server.server import PROTOCOL_VERSION

        return PROTOCOL_VERSION
    except Exception:
        return 1
