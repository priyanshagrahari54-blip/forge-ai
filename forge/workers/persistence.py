"""SQLite persistence for registered workers.

Worker registration is durable metadata, not execution authority. Endpoints and
capabilities are restored on restart, while liveness is re-established only by
a fresh heartbeat. This prevents a restarted control plane from trusting stale
workers as live.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import List, Optional

from forge.workers.registry import WorkerRecord, WorkerRegistry


class WorkerStore:
    """Small SQLite store for worker identity and resource metadata."""

    def __init__(self, path: str = ".forge/workers.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS workers (
                    worker_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    capabilities TEXT NOT NULL DEFAULT '',
                    cpu_threads INTEGER NOT NULL,
                    ram_mb INTEGER NOT NULL,
                    gpu INTEGER NOT NULL,
                    platform TEXT NOT NULL,
                    endpoint TEXT NOT NULL DEFAULT '',
                    registered_at REAL NOT NULL,
                    revoked INTEGER NOT NULL DEFAULT 0
                )"""
            )

    def save(self, worker: WorkerRecord) -> None:
        capabilities = "\n".join(worker.capabilities)
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO workers
                (worker_id,name,capabilities,cpu_threads,ram_mb,gpu,platform,
                 endpoint,registered_at,revoked)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(worker_id) DO UPDATE SET
                  name=excluded.name, capabilities=excluded.capabilities,
                  cpu_threads=excluded.cpu_threads, ram_mb=excluded.ram_mb,
                  gpu=excluded.gpu, platform=excluded.platform,
                  endpoint=excluded.endpoint, revoked=excluded.revoked""",
                (worker.worker_id, worker.name, capabilities,
                 worker.cpu_threads, worker.ram_mb, int(worker.gpu),
                 worker.platform, worker.endpoint, worker.registered_at,
                 int(worker.revoked)),
            )

    def load_all(self) -> List[WorkerRecord]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM workers ORDER BY registered_at").fetchall()
        now = time.time()
        # A restored worker starts with last_heartbeat=0: it must heartbeat
        # again before admission. Never revive stale execution authority.
        return [WorkerRecord(
            worker_id=row["worker_id"], name=row["name"],
            capabilities=tuple(x for x in row["capabilities"].splitlines() if x),
            cpu_threads=row["cpu_threads"], ram_mb=row["ram_mb"],
            gpu=bool(row["gpu"]), platform=row["platform"],
            endpoint=row["endpoint"], registered_at=row["registered_at"],
            last_heartbeat=0.0, active_jobs=0, revoked=bool(row["revoked"]),
        ) for row in rows]

    def restore(self, registry: WorkerRegistry) -> int:
        count = 0
        for worker in self.load_all():
            # Reuse the registry's lock through its controlled map insertion;
            # restored workers are intentionally not made live.
            with registry._lock:  # type: ignore[attr-defined]
                registry._workers[worker.worker_id] = worker  # type: ignore[attr-defined]
            count += 1
        return count
