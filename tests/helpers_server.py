"""Shared fixtures for the Forge Server (A81) test suites.

Servers here are real :class:`~forge.server.server.ForgeServer`
instances over temporary SQLite databases. Model work is either
scripted (deterministic executors) or driven through the real
Supervisor with the A34-style scripted provider/fabric, so the same
pipeline code runs as in production.
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

sys.path.insert(0, str(Path(__file__).parent))

from fastapi.testclient import TestClient  # noqa: E402

from forge.server import ForgeServer, ServerConfig, TaskStatus  # noqa: E402
from forge.server.executor import CallableExecutor  # noqa: E402

from helpers_a34 import (  # noqa: E402
    CSV_PAYLOAD,
    FAILING_PAYLOAD,
    BlockingProvider,
    ScriptedProvider,
    make_fabric,
    make_repo,
)

TEST_TOKEN = "test-bootstrap-token"


# -- executors ----------------------------------------------------------------

def success_executor(marker: str = "ok") -> CallableExecutor:
    """Completes immediately with a small scripted result."""

    def execute(ctx):
        ctx.log("scripted run (%s)" % marker)
        ctx.set_stage("CODE")
        ctx.emit("scripted.progress", {"marker": marker})
        return {"accepted": True,
                "result": {"summary": marker},
                "files": [],
                "model": "scripted", "provider": "scripted"}

    return CallableExecutor(execute)


def failing_executor(error: str = "boom",
                     times: Optional[int] = None) -> CallableExecutor:
    """Raises ``error``; ``times`` bounds how often before succeeding."""
    state = {"calls": 0}

    def execute(ctx):
        state["calls"] += 1
        ctx.log("attempt %d" % state["calls"])
        if times is None or state["calls"] <= times:
            raise RuntimeError(error)
        return {"accepted": True,
                "result": {"recovered_after": state["calls"]}}

    return CallableExecutor(execute)


def rejected_executor(error: str = "tests failed") -> CallableExecutor:
    """Returns an honest pipeline rejection (no exception, no retry)."""

    def execute(ctx):
        return {"accepted": False, "error": error,
                "result": {"rejected": True}}

    return CallableExecutor(execute)


class BlockingExecutor:
    """Blocks until released; honors cooperative pause/cancel boundaries.

    Callable, so it composes with :class:`CallableExecutor`. The control
    checkpoint inside the wait loop gives pause/cancel the same
    cooperative semantics the real Supervisor provides.
    """

    def __init__(self, result: Optional[Dict[str, Any]] = None) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.result = result or {"accepted": True,
                                 "result": {"blocked": True}}
        self.calls = 0
        self.ctx: Any = None

    def __call__(self, ctx) -> Dict[str, Any]:
        self.calls += 1
        self.ctx = ctx
        ctx.set_stage("CODE")
        self.entered.set()
        while not self.release.wait(timeout=0.05):
            # Raises TaskCancelled on cancel; blocks while paused.
            ctx.control.checkpoint("blocked")
        return dict(self.result)

    def as_executor(self) -> CallableExecutor:
        return CallableExecutor(self)


class ApprovalExecutor:
    """Requests one approval mid-run and records the minted token."""

    def __init__(self, path: str = "app.py") -> None:
        self.entered = threading.Event()
        self.token: Optional[str] = None
        self.path = path

    def __call__(self, ctx) -> Dict[str, Any]:
        from forge.tools.change_applier import ApprovalItem, ApprovalQuery

        self.entered.set()
        ctx.set_stage("CODE")
        query = ApprovalQuery(
            items=(ApprovalItem(
                operation="write_file", path=self.path, tool="coder",
                risk="LOW", reason="scripted change"),),
            agent="coder", task_id=ctx.task.task_id,
            capability="coding", fingerprint="fp-server-test",
            label="apply scripted change")
        token = ctx.request_approval(query)
        self.token = token
        if not token:
            return {"accepted": False,
                    "error": "Approval denied or expired.",
                    "result": {"approved": False}}
        return {"accepted": True,
                "result": {"approved": True, "token_id": token}}

    def as_executor(self) -> CallableExecutor:
        return CallableExecutor(self)


class FileWritingExecutor:
    """Creates a file in the project root (rollback tests)."""

    def __init__(self, name: str = "generated.txt",
                 content: str = "generated\n") -> None:
        self.name = name
        self.content = content

    def __call__(self, ctx) -> Dict[str, Any]:
        path = Path(ctx.root) / self.name
        path.write_text(self.content, encoding="utf-8")
        ctx.set_stage("COMMIT")
        return {"accepted": True,
                "result": {"files": [self.name]},
                "files": [self.name]}

    def as_executor(self) -> CallableExecutor:
        return CallableExecutor(self)


# -- server construction ---------------------------------------------------------

def make_server(tmp_path: Path, executor: Any = None, *,
                project_id: str = "demo",
                extra_projects: Optional[Dict[str, str]] = None,
                profile: str = "assisted",
                policy: Any = None,
                fabric: Any = None,
                with_repo: bool = True,
                start: bool = True,
                max_retries: int = 2,
                max_workers: int = 2,
                max_tasks_per_project: int = 1,
                db_path: Optional[Path] = None,
                **config_kwargs: Any) -> ForgeServer:
    """Build (and usually start) a Forge Server over a temporary state dir."""
    root = tmp_path / project_id
    root.mkdir(parents=True, exist_ok=True)
    if with_repo:
        make_repo(root)
    projects = {project_id: str(root)}
    for key, value in (extra_projects or {}).items():
        projects[key] = value
    if executor is None and fabric is None:
        # Fast scripted default for unit-style tests. An explicit fabric
        # (with executor=None) means "run the real SupervisorExecutor".
        executor = success_executor()
    config = ServerConfig(
        db_path=str(db_path or (tmp_path / "server" / "server.db")),
        projects=projects,
        bootstrap_token=TEST_TOKEN,
        profile=profile,
        policy=policy,
        fabric=fabric,
        executor=executor,
        default_max_retries=max_retries,
        max_workers=max_workers,
        max_tasks_per_project=max_tasks_per_project,
        retry_backoff_seconds=config_kwargs.pop(
            "retry_backoff_seconds", 0.05),
        poll_interval=config_kwargs.pop("poll_interval", 0.05),
        approval_timeout=config_kwargs.pop("approval_timeout", 60.0),
        **config_kwargs)
    server = ForgeServer(config)
    if start:
        server.start()
    return server


def make_client(server: ForgeServer) -> TestClient:
    return TestClient(server.create_app())


def auth_headers(token: str = TEST_TOKEN) -> Dict[str, str]:
    return {"Authorization": "Bearer %s" % token}


# -- waiting ------------------------------------------------------------------------

def _status_values(statuses: Iterable[Any]) -> set:
    return {status.value if isinstance(status, TaskStatus) else str(status)
            for status in statuses}


def wait_for_status(server: ForgeServer, task_id: str,
                    statuses: Iterable[Any],
                    timeout: float = 20.0):
    """Poll the durable task row until it reaches one of ``statuses``."""
    wanted = _status_values(statuses)
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = server.tasks.get(task_id)
        if task is not None and task.status.value in wanted:
            return task
        time.sleep(0.02)
    return server.tasks.get(task_id)


def wait_for_status_http(client: TestClient, headers: Dict[str, str],
                         task_id: str, statuses: Iterable[Any],
                         timeout: float = 20.0) -> Dict[str, Any]:
    wanted = _status_values(statuses)
    deadline = time.time() + timeout
    payload: Dict[str, Any] = {}
    while time.time() < deadline:
        response = client.get("/api/v1/tasks/%s" % task_id, headers=headers)
        assert response.status_code == 200, response.text
        payload = response.json()["task"]
        if payload["status"] in wanted:
            return payload
        time.sleep(0.03)
    return payload


def wait_for_pending_approval(client: TestClient, headers: Dict[str, str],
                              predicate: Callable[[Dict[str, Any]], bool],
                              timeout: float = 60.0
                              ) -> Optional[Dict[str, Any]]:
    """Poll GET /api/v1/approvals until a pending row matches ``predicate``.

    Status polling alone is racy across successive approval gates (the
    previous gate's ``waiting_for_approval`` can still be observable for
    a few milliseconds after its decision), so tests match on the
    durable pending-approval list itself.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = client.get("/api/v1/approvals", headers=headers)
        assert response.status_code == 200, response.text
        for approval in response.json()["approvals"]:
            if predicate(approval):
                return approval
        time.sleep(0.05)
    return None


def wait_until(predicate: Callable[[], bool],
               timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# -- HTTP helpers ---------------------------------------------------------------------

def create_task(client: TestClient, headers: Dict[str, str],
                requirement: str = "Do the thing",
                project_id: str = "demo", **extra: Any) -> Dict[str, Any]:
    payload = {"project_id": project_id, "requirement": requirement}
    payload.update(extra)
    response = client.post("/api/v1/tasks", headers=headers, json=payload)
    assert response.status_code == 200, response.text
    return response.json()["task"]


def http_status(client: TestClient, headers: Dict[str, str],
                task_id: str) -> str:
    response = client.get("/api/v1/tasks/%s" % task_id, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["task"]["status"]


def approve_until_terminal(client: TestClient, headers: Dict[str, str],
                           task_id: str, timeout: float = 180.0,
                           approve: bool = True) -> Dict[str, Any]:
    """Approve (or deny) every pending approval until the task ends."""
    terminal = {"completed", "failed", "cancelled", "rolled_back"}
    deadline = time.time() + timeout
    state: Dict[str, Any] = {}
    while time.time() < deadline:
        listing = client.get("/api/v1/approvals", headers=headers)
        if listing.status_code == 200:
            for approval in listing.json()["approvals"]:
                client.post(
                    "/api/v1/approvals/%s/decide"
                    % approval["approval_id"],
                    headers=headers, json={"approved": approve})
        response = client.get("/api/v1/tasks/%s" % task_id,
                              headers=headers)
        if response.status_code == 200:
            state = response.json()["task"]
            if state["status"] in terminal:
                return state
        time.sleep(0.1)
    return state


def git_log(root: Path) -> str:
    return subprocess.run(["git", "log", "--oneline"], cwd=str(root),
                          text=True, capture_output=True).stdout


def event_types(events: List[Dict[str, Any]]) -> List[str]:
    return [event["type"] for event in events]
