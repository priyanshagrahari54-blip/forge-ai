"""Forge Server (A81): the standalone task backend for Forge AI.

Architecture::

    Client
      → Authentication   forge.server.auth        (API keys, sessions, bootstrap)
      → Authorization    forge.server.authorization (scopes + A33 policy bridge)
      → API Gateway      forge.server.api          (strict, bounded, fail-closed)
      → Task Queue       forge.server.queue        (persistent SQLite, leased)
      → Scheduler        forge.server.scheduler    (dispatch loop, caps, backoff)
      → Workers          forge.server.workers      (background threads)
      → Agents           forge.server.executor     (Supervisor + agent registry)
      → Model Runtime    forge.models.fabric       (Model Fabric + native engine)
      → Verification     forge.security.*          (gates inside the run)
      → Result Store     forge.server.storage      (tasks/events/logs/results)

Everything durable lives in one SQLite database, so the server survives
restarts and Forge Desktop clients can disconnect and reconnect later,
recovering active tasks, progress, logs, results, and pending approval
requests in a single ``GET /api/v1/recovery`` call.

The API is a task backend, never a remote shell: the operation set is
closed, no field accepts executable text, and every disk effect flows
through the A33-gated Supervisor transaction. See
:mod:`forge.server.authorization`.

Quick start::

    from forge.server import ForgeServer, ServerConfig

    server = ForgeServer(ServerConfig(projects={"demo": "./demo"}))
    server.start()
    app = server.create_app()      # FastAPI app (uvicorn/TestClient)
    # ... later:
    server.close()

CLI::

    forge server                   # run the server (uvicorn)
    forge server status            # live status of a running server
    forge server health            # health report of a running server
"""
from forge.server.models import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    ProjectInfo,
    ServerTask,
    TaskStatus,
)
from forge.server.server import PROTOCOL_VERSION, ForgeServer, ServerConfig

#: Version of the Forge Server component itself.
SERVER_VERSION = "0.1.0"

__all__ = [
    "ACTIVE_STATUSES",
    "TERMINAL_STATUSES",
    "ForgeServer",
    "PROTOCOL_VERSION",
    "ProjectInfo",
    "SERVER_VERSION",
    "ServerConfig",
    "ServerTask",
    "TaskStatus",
]
