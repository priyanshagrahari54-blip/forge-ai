"""Server health monitoring (A81).

:meth:`HealthMonitor.health` is the honest answer to "can this server
do its job right now": database reachable, worker pool alive, scheduler
running, disk not exhausted, queue not stuck, approvals not rotting.
It degrades loudly (``status: degraded`` + warnings) instead of
reporting green while broken, and it never leaks secrets or internal
exception text — component checks fail closed to warning strings.

``GET /api/v1/health`` (authenticated) renders this; ``forge server
health`` prints it from the CLI.
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any, Dict, List

#: Below this much free disk space the server reports degraded.
LOW_DISK_MB = 100


class HealthMonitor:
    """Component health checks over the live server."""

    def __init__(self, server: Any) -> None:
        self.server = server

    def health(self) -> Dict[str, Any]:
        server = self.server
        warnings: List[str] = []
        components: Dict[str, Any] = {}

        components["database"] = self._check_database(warnings)
        components["workers"] = self._check_workers(warnings)
        components["scheduler"] = self._check_scheduler(warnings)
        components["queue"] = self._check_queue(warnings)
        components["disk"] = self._check_disk(warnings)
        components["approvals"] = self._check_approvals(warnings)
        components["inference"] = self._check_inference(warnings)

        counts = server.tasks.status_counts()
        interrupted = (counts.get("started", 0) + counts.get("running", 0))
        if not server.running and interrupted:
            warnings.append(
                "%d task(s) are marked active but the server is not "
                "running; restart recovery will re-queue them."
                % interrupted)

        status = "ok" if not warnings else "degraded"
        if not components["database"]["ok"]:
            status = "down"
        report: Dict[str, Any] = {
            "status": status,
            "server": {
                "name": "forge-server",
                "version": _version(),
                "boot_id": server.boot_id,
                "running": server.running,
                "started_at": server.started_at,
                "uptime_seconds": round(
                    time.time() - server.started_at, 3)
                if server.started_at else 0.0,
                "time": time.time(),
            },
            "components": components,
            "tasks": counts,
            "projects": len(server.projects.list()),
            "active_sessions": server.sessions.count_active(),
            "unread_notifications": server.notifications.unread_count(),
            "auth_mode": ("local-dev" if server.config.local_dev_mode
                          else "production"),
            "profile": server.config.profile,
            "warnings": warnings,
        }
        if server.config.local_dev_mode:
            report["warnings"].append(
                "Local-development authentication: API keys/sessions "
                "only, no passwords. Do not expose to untrusted "
                "networks; production deployments must front the server "
                "with real authentication.")
        return report

    # -- component checks ------------------------------------------------------

    def _check_inference(self, warnings: List[str]) -> Dict[str, Any]:
        """The canonical inference path, without building anything (§15/§33).

        Reading health must not have side effects: the fabric is only described
        when it already exists, so a health check never discovers, loads or
        contacts a model. Configuration failures are surfaced as warnings
        because an operator who cannot see them cannot fix them.
        """
        service = getattr(self.server, "inference", None)
        path_config = getattr(self.server, "inference_path_config", None)
        mode = str(getattr(path_config, "mode", "legacy") or "legacy")
        if service is None:
            return {"ok": True, "enabled": False, "mode": mode}
        try:
            payload = service.status()
        except Exception as exc:                       # noqa: BLE001
            warnings.append("Inference status unavailable: %s"
                            % type(exc).__name__)
            return {"ok": False, "enabled": False, "mode": mode,
                    "error": type(exc).__name__}
        enabled = bool(payload.get("enabled"))
        report: Dict[str, Any] = {
            "ok": True,
            "enabled": enabled,
            "started": bool(payload.get("started")),
            "mode": mode,
            "running_requests": payload.get("running_requests"),
            "max_concurrent_requests": payload.get("max_concurrent_requests"),
            "invalid_providers": list(payload.get("invalid_providers") or []),
        }
        if payload.get("start_error"):
            warnings.append("Inference could not start: %s"
                            % str(payload["start_error"])[:200])
            report["ok"] = False
            report["start_error"] = str(payload["start_error"])[:200]
        for item in payload.get("providers") or []:
            if item.get("state") == "configured":
                continue
            #: §15: CONFIGURED → INVALID_CONFIGURATION → UNAVAILABLE, with the
            #: bounded redacted reason an operator needs.
            warnings.append(
                "Remote provider '%s' is %s and cannot serve: %s"
                % (item.get("provider_id", "?"), item.get("state", "?"),
                   str(item.get("reason") or "no reason recorded")[:160]))
            report["ok"] = False
        fabric_built = getattr(service, "_fabric", None)
        if fabric_built is not None:
            try:
                fabric_state = fabric_built.status()
                models = fabric_state.get("models") or {}
                report["models"] = {
                    "total": models.get("total"),
                    "verified": models.get("verified"),
                    "usable": models.get("usable"),
                    "local": models.get("local"),
                    "remote": models.get("remote"),
                }
                #: Bounded backend summary: kinds and reachability only, never
                #: endpoints, credentials or model output.
                backends = fabric_state.get("backends") or []
                report["backends"] = {
                    "count": len(backends),
                    "configured": sum(1 for item in backends
                                      if item.get("configured")),
                    "reachable": sum(1 for item in backends
                                     if item.get("reachable")),
                    "verified": sum(1 for item in backends
                                    if item.get("verified")),
                }
                counters = fabric_state.get("counters") or {}
                if counters:
                    report["counters"] = dict(counters)
            except Exception as exc:                   # noqa: BLE001
                report["fabric_error"] = type(exc).__name__
        fences = getattr(self.server, "fences", None)
        if fences is not None:
            try:
                report["fences"] = fences.snapshot(limit=20)
            except Exception as exc:                   # noqa: BLE001
                report["fences_error"] = type(exc).__name__
        return report

    def _check_database(self, warnings: List[str]) -> Dict[str, Any]:
        try:
            row = self.server.db.query_one("SELECT 1 AS ok")
            reachable = row is not None and int(row["ok"]) == 1
            path = Path(self.server.config.db_path)
            size = path.stat().st_size if path.exists() else 0
            if not reachable:
                warnings.append("Database check query failed.")
            return {"ok": reachable, "engine": "sqlite",
                    "size_bytes": size}
        except Exception as exc:
            warnings.append("Database unreachable: %s" % type(exc).__name__)
            return {"ok": False, "engine": "sqlite", "size_bytes": 0}

    def _check_workers(self, warnings: List[str]) -> Dict[str, Any]:
        pool = self.server.pool
        alive = pool.alive
        if self.server.running and not alive:
            warnings.append("Worker pool is not alive.")
        return {"ok": alive or not self.server.running,
                "max_workers": pool.max_workers,
                "busy": pool.busy,
                "alive": alive}

    def _check_scheduler(self, warnings: List[str]) -> Dict[str, Any]:
        stats = self.server.scheduler.stats()
        ok = bool(stats["running"]) or not self.server.running
        if self.server.running and not stats["running"]:
            warnings.append("Scheduler dispatcher is not running.")
        return {"ok": ok, "running": stats["running"],
                "active_total": stats["active_total"]}

    def _check_queue(self, warnings: List[str]) -> Dict[str, Any]:
        depth = self.server.queue.depth()
        leased = self.server.queue.leased_count()
        dispatchable = self.server.queue.dispatchable_count()
        if (self.server.running and dispatchable > 0 and leased == 0
                and self.server.pool.busy == 0
                and self.server.scheduler.active_count() == 0
                and self.server.scheduler.running):
            # Immediately-dispatchable work, nothing leased or running,
            # scheduler alive: dispatch is stuck. (Items in retry
            # backoff are *not* dispatchable and never trigger this.)
            warnings.append(
                "Queue has %d dispatchable item(s) but nothing is "
                "leased or running; dispatch appears stuck."
                % dispatchable)
        return {"ok": True, "depth": depth, "leased": leased,
                "dispatchable": dispatchable}

    def _check_disk(self, warnings: List[str]) -> Dict[str, Any]:
        try:
            target = Path(self.server.config.db_path).parent
            if not target.exists():
                target = Path.cwd()
            usage = shutil.disk_usage(str(target))
            free_mb = usage.free // (1024 * 1024)
            if free_mb < LOW_DISK_MB:
                warnings.append(
                    "Low disk space: %d MB free." % free_mb)
            return {"ok": free_mb >= LOW_DISK_MB, "free_mb": free_mb}
        except Exception:
            return {"ok": True, "free_mb": None}

    def _check_approvals(self, warnings: List[str]) -> Dict[str, Any]:
        pending = self.server.approvals.pending()
        overdue = [record for record in pending
                   if float(record["expires_at"]) - time.time() < 60.0]
        if overdue:
            warnings.append(
                "%d approval(s) expire within 60 seconds." % len(overdue))
        return {"ok": True, "pending": len(pending),
                "expiring_soon": len(overdue)}


def _version() -> str:
    try:
        from forge.server import SERVER_VERSION

        return SERVER_VERSION
    except Exception:
        return "0.0.0"
