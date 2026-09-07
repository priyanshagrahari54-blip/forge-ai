"""Shared fixtures for the A34 cockpit test suites."""
from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path

from contextlib import contextmanager

from fastapi.testclient import TestClient

from forge.api.app import create_app
from forge.control import ControlPlane, ControlConfig
from forge.models.fabric import ModelFabric
from forge.models.provider import ModelResult, ProviderRegistry
from forge.models.registry import Model, ModelRegistry

CSV_PAYLOAD = json.dumps({
    "summary": "Add CSV export",
    "changes": [
        {"path": "app.py", "action": "modify",
         "content": "def health(): return True\n\ndef export_csv():\n    return 'x'\n"},
        {"path": "tests/test_csv.py", "action": "create",
         "content": "from app import export_csv\ndef test_csv():\n    assert export_csv() == 'x'\n"},
    ],
    "tests_to_run": ["tests/test_csv.py"],
    "reasoning_summary": "added export",
    "risk_level": "low",
})


FAILING_PAYLOAD = json.dumps({
    "summary": "Add broken export",
    "changes": [
        {"path": "app.py", "action": "modify",
         "content": "def health(): return True\n\ndef export_csv():\n    return 'wrong'\n"},
        {"path": "tests/test_csv.py", "action": "create",
         "content": "from app import export_csv\ndef test_csv():\n    assert export_csv() == 'x'\n"},
    ],
    "tests_to_run": ["tests/test_csv.py"],
    "reasoning_summary": "broken on purpose",
    "risk_level": "low",
})


def make_repo(root: Path) -> None:
    (root / "app.py").write_text("def health(): return True\n")
    (root / ".gitignore").write_text("__pycache__/\n.forge/\n")
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "test_app.py").write_text(
        "from app import health\ndef test_health():\n    assert health()\n")
    subprocess.run(["git", "init"], cwd=root, check=True,
                   capture_output=True)
    subprocess.run(["git", "config", "user.name", "Forge Test"], cwd=root,
                   check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"],
                   cwd=root, check=True)
    subprocess.run(
        ["git", "add", "--", ".gitignore", "app.py", "tests/test_app.py"],
        cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=root, check=True,
                   capture_output=True)


class ScriptedProvider:
    """Deterministic provider: scripted coder payload, approving reviewer."""

    name = "scripted"

    def __init__(self, coder_payload: str = CSV_PAYLOAD) -> None:
        self.coder_payload = coder_payload
        self.prompts: list[str] = []

    def generate(self, prompt, *, context="", task="", instructions="",
                 max_output_tokens=None, temperature=None):
        self.prompts.append(prompt)
        if "Review the following change set" in prompt:
            return ModelResult(
                json.dumps({"findings": [], "verdict": "APPROVE"}),
                self.name)
        return ModelResult(self.coder_payload, self.name)


class BlockingProvider(ScriptedProvider):
    """Scripted provider that blocks until released (pause/cancel tests)."""

    name = "blocking"

    def __init__(self, coder_payload: str = CSV_PAYLOAD) -> None:
        super().__init__(coder_payload)
        self.entered = threading.Event()
        self.release = threading.Event()

    def generate(self, prompt, **kwargs):
        if "Review the following change set" not in prompt:
            self.entered.set()
            assert self.release.wait(timeout=60), "provider wait timed out"
        return super().generate(prompt, **kwargs)


def make_fabric(provider) -> ModelFabric:
    return ModelFabric(
        registry=ModelRegistry([
            Model(name="m/a34", provider="p",
                  capabilities=("coding", "debugging", "review"),
                  free=True, local=True),
        ]),
        providers=ProviderRegistry({"p": provider}),
    )


def make_plane(tmp_path: Path, provider=None, *,
               project_id: str = "demo",
               extra_projects: dict[str, str] | None = None,
               approval_timeout: float = 60.0,
               start: bool = True) -> ControlPlane:
    root = tmp_path / project_id
    root.mkdir(parents=True, exist_ok=True)
    projects = {project_id: str(root)}
    for pid, proot in (extra_projects or {}).items():
        Path(proot).mkdir(parents=True, exist_ok=True)
        projects[pid] = proot
    config = ControlConfig(
        db_path=str(tmp_path / "cockpit.db"),
        projects=projects,
        fabric=make_fabric(provider or ScriptedProvider()),
        approval_timeout=approval_timeout,
        approval_token_ttl=60.0,
    )
    plane = ControlPlane(config)
    if start:
        plane.start()
    return plane


def make_client(plane: ControlPlane) -> TestClient:
    return TestClient(create_app(plane), raise_server_exceptions=False)


@contextmanager
def run_server(plane: ControlPlane):
    """Serve the app over a real localhost socket (for SSE tests)."""
    import uvicorn

    app = create_app(plane)
    config = uvicorn.Config(app, host="127.0.0.1", port=0,
                            log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15.0
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "uvicorn failed to start"
    sockets = server.servers[0].sockets if server.servers else []
    assert sockets, "uvicorn has no sockets"
    port = sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10.0)


def login(client: TestClient, actor: str = "alice",
          project_id: str = "demo", profile: str = "assisted"):
    response = client.post("/api/v1/sessions", json={
        "actor": actor, "project_id": project_id, "profile": profile})
    assert response.status_code == 200, response.text
    payload = response.json()
    headers = {"Authorization": f"Bearer {payload['token']}"}
    return payload["session"], payload["token"], headers


def wait_for(predicate, timeout: float = 60.0, interval: float = 0.2):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"timed out waiting: {predicate} (last={last!r})")


def task_state(client: TestClient, headers: dict,
               task_id: str) -> dict:
    response = client.get(f"/api/v1/tasks/{task_id}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["task"]


def wait_for_status(client: TestClient, headers: dict, task_id: str,
                    statuses, timeout: float = 120.0) -> dict:
    wanted = {statuses} if isinstance(statuses, str) else set(statuses)
    return wait_for(
        lambda: (task_state(client, headers, task_id)
                 if task_state(client, headers, task_id)["status"] in wanted
                 else None),
        timeout=timeout)


def approve_all(client: TestClient, headers: dict) -> int:
    response = client.get("/api/v1/approvals", headers=headers)
    assert response.status_code == 200, response.text
    count = 0
    for approval in response.json()["approvals"]:
        decide = client.post(
            f"/api/v1/approvals/{approval['id']}/approve", headers=headers)
        assert decide.status_code == 200, decide.text
        count += 1
    return count


def drive_to_terminal(client: TestClient, headers: dict, task_id: str,
                      timeout: float = 180.0) -> dict:
    """Approve pending requests until the task reaches a terminal state."""
    terminal = {"SUCCEEDED", "FAILED", "CANCELLED", "ROLLED_BACK"}
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        approve_all(client, headers)
        last = task_state(client, headers, task_id)
        if last["status"] in terminal:
            return last
        time.sleep(0.3)
    raise AssertionError(f"task {task_id} never finished (last={last!r})")
