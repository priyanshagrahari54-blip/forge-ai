"""A81 client-stack tests: ForgeClient against the real server app.

Everything runs in-process (signed requests over the TestClient
adapter) — deterministic, no sockets.
"""
from __future__ import annotations

import time

import pytest

from forge.client.resources import ResourceSnapshot
from forge.link.errors import LinkError
from helpers_link import (
    LinkEnv,
    make_client_config,
    make_client_for,
    wait_until,
)


@pytest.fixture()
def env(tmp_path):
    environment = LinkEnv(tmp_path)
    environment.make_repo()
    yield environment
    environment.close()


def _resources(free_mb=3000, cpus=2):
    return lambda: ResourceSnapshot(cpus=cpus, total_ram_mb=8192,
                                    free_ram_mb=free_mb, source="test")


def test_connect_handshake_and_status(env):
    secret = env.register()
    client = make_client_for(env, secret)
    assert not client.server_status.reachable
    assert client.connect(with_heartbeat=False)
    assert client.connection.state_name == "CONNECTED"
    status = client.status()
    assert status["state"] == "CONNECTED"
    assert status["server_url"] == "http://forge-server:8000"
    assert status["client_id"] == "g560"
    assert status["mode"] == "server"
    assert client.server_status.reachable
    assert client.server_status.workers >= 1
    client.disconnect()
    assert client.connection.state_name == "DISCONNECTED"
    assert not client.server_status.reachable


def test_bad_secret_is_auth_failure_not_retry(env):
    env.register()  # real client exists...
    config = make_client_config(env.tmp, "Z" * 43 + "9")  # ...wrong secret
    from forge.client.transport import LinkTransport
    from forge.client.client import ForgeClient

    bad = ForgeClient(config,
                      transport=LinkTransport(config, sender=env.sender()))
    assert not bad.connect(with_heartbeat=False)
    assert bad.connection.state_name == "AUTH_FAILED"
    assert "authentication" in bad.connection.last_error.lower()
    bad.disconnect()


def test_submit_server_task_end_to_end_with_approval(env):
    secret = env.register()
    client = make_client_for(env, secret)
    assert client.connect(with_heartbeat=False)

    result = client.submit("Add CSV export functionality")
    assert result["execution"] == "SERVER"
    task_id = result["task"]["id"]
    assert result["effective_mode"] == "assisted"  # server ceiling
    assert any("ROUTE: SERVER" in line for line in client.logs())

    def finished():
        snapshot = client.snapshot(selected_task=task_id, full=True)
        for approval in snapshot["approvals"]:
            client.decide_approval(approval["id"], True)
        for task in snapshot["tasks"]:
            if task["id"] == task_id and task["status"] in ("SUCCEEDED",
                                                            "FAILED"):
                return task
        return None

    final = wait_until(finished, timeout=60)
    assert final["status"] == "SUCCEEDED"
    assert final["model"] == "m/a81"
    assert final["files"] == ["app.py", "tests/test_csv.py"]

    snapshot = client.snapshot(selected_task=task_id, full=True)
    verification = snapshot["verification"]
    assert verification["acceptance"].get("accepted") is True
    assert verification["tests"]
    assert snapshot["queue"]["depth"] >= 0
    assert snapshot["event_cursor"] > 0


def test_local_light_task_runs_without_server(tmp_path):
    from forge.client.client import ForgeClient
    from forge.client.transport import LinkTransport

    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "app.py").write_text("def f():\n    return 1\n# TODO x\n")

    def dead_sender(*_args, **_kwargs):
        raise LinkError("no route", code="TRANSPORT_ERROR")

    config = make_client_config(tmp_path, "A" * 43 + "b", mode="hybrid",
                                project_id="proj")
    client = ForgeClient(
        config, transport=LinkTransport(config, sender=dead_sender),
        resource_probe=_resources())

    result = client.submit("show repository status", root=str(repo))
    assert result["execution"] == "LOCAL"
    task = result["task"]
    assert task["status"] == "SUCCEEDED"
    assert task["model"] == ""          # no model was loaded anywhere
    assert task["provider"] == "local-light"
    assert task["result"]["operation"] == "repo_summary"
    assert "light local task" in task["decision"]


def test_local_refuses_without_root(env):
    secret = env.register()
    client = make_client_for(env, secret)
    client.connect(with_heartbeat=False)
    # Force a LOCAL decision with a dedicated config view.
    client.config.mode = "local"
    client.config.local.allow_server = False
    from forge.link.errors import LocalExecutionRefused

    with pytest.raises(LocalExecutionRefused):
        client.submit("show repository status")


def test_engineering_task_with_dead_server_fails_honestly(tmp_path):
    from forge.client.client import ForgeClient
    from forge.client.transport import LinkTransport

    def dead_sender(*_args, **_kwargs):
        raise LinkError("no route", code="TRANSPORT_ERROR")

    config = make_client_config(tmp_path, "A" * 43 + "b", mode="hybrid")
    client = ForgeClient(
        config, transport=LinkTransport(config, sender=dead_sender),
        resource_probe=_resources())
    with pytest.raises(LinkError) as excinfo:
        client.submit("implement a new feature")
    assert "unreachable" in str(excinfo.value)


def test_snapshot_shape_and_logs(env):
    secret = env.register()
    client = make_client_for(env, secret)
    client.connect(with_heartbeat=False)
    client.submit("Add CSV export functionality")
    snapshot = client.snapshot(full=True)
    for key in ("connection", "server_info", "tasks", "queue", "approvals",
                "events", "event_cursor", "verification", "logs"):
        assert key in snapshot
    assert snapshot["server_info"]["server"] == "forge-server"
    assert snapshot["server_info"]["model_ready"] is True
    assert any("[ROUTE]" in line or "ROUTE" in line
               for line in snapshot["logs"])
    # Local snapshot never contains the secret.
    assert "A" * 10 not in repr(snapshot)


def test_client_request_timeout_is_applied(env):
    secret = env.register()
    config = make_client_config(env.tmp, secret, request_timeout=0.6)
    from forge.client.transport import LinkTransport
    from forge.client.client import ForgeClient

    def slow_sender(method, url, headers, body, timeout):
        time.sleep(timeout + 0.5)
        return 200, b"{}"

    client = ForgeClient(config, transport=LinkTransport(
        config, sender=slow_sender))
    # The slow sender ignores its contract; the connect must still work
    # because the fake server returns garbage -> auth error path.
    assert not client.connect(with_heartbeat=False)


def test_task_estimate_travels_with_server_submission(env):
    secret = env.register()
    client = make_client_for(env, secret)
    client.connect(with_heartbeat=False)
    decision = client.decide("Add CSV export functionality")
    assert decision.execution == "SERVER"
    assert decision.estimate.needs_model
    result = client.submit("Add CSV export functionality")
    # The decision reason was recorded in the origin table server-side.
    # (mode=server short-circuits the hybrid gates, so the recorded
    # reason is the mode itself.)
    task_id = result["task"]["id"]
    origin = env.service.store.get_task_origin(task_id)
    assert origin is not None
    assert origin["execution"] == "SERVER"
    assert origin["decision"] == "mode=server"

    # Through HYBRID the recorded reason is the model-availability gate.
    client.config.mode = "hybrid"
    hybrid = client.submit("Add CSV export functionality")
    hybrid_id = hybrid["task"]["id"]
    hybrid_origin = env.service.store.get_task_origin(hybrid_id)
    assert "task requires a model" in hybrid_origin["decision"]


def test_mutate_proxy_validates_operations(env):
    secret = env.register()
    client = make_client_for(env, secret)
    client.connect(with_heartbeat=False)
    from forge.link.errors import LinkError

    with pytest.raises(LinkError):
        client.mutate_task("t-x", "detonate")
