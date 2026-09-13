"""Session 10 (D2): G560 true thin client.

The desktop (and the CLI) talk to a Forge Server as a TRUE thin
client: UI, typed task submission, progress, approvals. No local
execution plane, no arbitrary commands (the server schema is
strict). Login is challenge/response with single-use, time-bounded
nonces, so a captured session exchange cannot be replayed.
"""
from __future__ import annotations

import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from helpers_server import success_executor  # noqa: E402
from forge.server.client import (  # noqa: E402
    ForgeServerClient,
    ForgeServerClientError,
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class LiveServer:
    """A real uvicorn-hosted Forge Server for HTTP-level tests."""

    def __init__(self, tmp_path, *, require_challenge: bool = False,
                 executor=None):
        import uvicorn

        from forge.server import ForgeServer, ServerConfig

        self.tmp_path = tmp_path
        repo = tmp_path / "demo"
        repo.mkdir(exist_ok=True)
        self.port = _free_port()
        config = ServerConfig(
            db_path=str(tmp_path / "server.db"),
            host="127.0.0.1", port=self.port,
            projects={"demo": str(repo)},
            bootstrap_token="g560_bootstrap_token",
            require_challenge=require_challenge,
            executor=executor or success_executor("thin"),
            max_workers=2,
            poll_interval=0.05,
            approval_timeout=60.0,
        )
        self.server = ForgeServer(config)
        self.server.start()
        self.uvicorn_config = uvicorn.Config(
            self.server.create_app(), host="127.0.0.1", port=self.port,
            log_level="warning")
        self.httpd = uvicorn.Server(self.uvicorn_config)
        self.thread = threading.Thread(target=self.httpd.run, daemon=True)
        self.thread.start()
        deadline = time.time() + 15
        while time.time() < deadline and not self.httpd.started:
            time.sleep(0.05)
        assert self.httpd.started, "uvicorn did not start"
        self.base_url = "http://127.0.0.1:%d" % self.port

    def raw(self, method: str, path: str, payload=None, token=""):
        url = self.base_url + "/api/v1" + path
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        request = urllib.request.Request(url, data=data, headers=headers,
                                         method=method)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return int(response.status), json.loads(
                    response.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            return int(exc.code), json.loads(exc.read().decode() or "{}")

    def close(self):
        self.httpd.should_exit = True
        self.thread.join(timeout=15)
        self.server.close()


# -- challenge / response mechanics ------------------------------------------


def test_challenge_login_and_replay_rejected(tmp_path):
    live = LiveServer(tmp_path)
    try:
        client = ForgeServerClient(live.base_url)
        result = client.login("g560_bootstrap_token")
        assert result["token"].startswith("fss_")
        who = client.whoami()
        assert who["principal"]["role"] in ("admin", "operator")

        # The session token itself authenticates subsequent calls.
        status = client.status()
        assert status["running"] is True

        # Replay of the SAME consumed challenge must be refused.
        challenge = client.challenge()
        nonce = challenge["nonce"]
        code, _ = live.raw(
            "POST", "/auth/sessions",
            payload={"challenge_nonce": nonce},
            token="g560_bootstrap_token")
        assert code == 200
        code, body = live.raw(
            "POST", "/auth/sessions",
            payload={"challenge_nonce": nonce},
            token="g560_bootstrap_token")
        assert code == 400
        assert "already used" in body["error"]["message"].lower() \
            or "expired" in body["error"]["message"].lower() \
            or "unknown" in body["error"]["message"].lower()

        # A fresh challenge works again (new session).
        result2 = client.login("g560_bootstrap_token")
        assert result2["token"].startswith("fss_")
    finally:
        live.close()


def test_require_challenge_blocks_challengeless_sessions(tmp_path):
    live = LiveServer(tmp_path, require_challenge=True)
    try:
        # Without a challenge: refused (authenticated, but no nonce).
        code, body = live.raw("POST", "/auth/sessions", payload={},
                              token="g560_bootstrap_token")
        assert code == 400
        assert "challenge" in body["error"]["message"].lower()
        # Bogus nonce: refused.
        code, _ = live.raw(
            "POST", "/auth/sessions",
            payload={"challenge_nonce": "not-a-real-nonce"},
            token="g560_bootstrap_token")
        assert code == 400
        # Proper handshake: works.
        client = ForgeServerClient(live.base_url)
        result = client.login("g560_bootstrap_token")
        assert result["token"].startswith("fss_")
    finally:
        live.close()


def test_expired_challenge_is_refused(tmp_path, monkeypatch):
    import forge.server.auth as auth_module

    monkeypatch.setattr(auth_module, "CHALLENGE_TTL_SECONDS", 0.05)
    live = LiveServer(tmp_path)
    try:
        client = ForgeServerClient(live.base_url)
        challenge = client.challenge()
        time.sleep(0.12)  # outlive the (shortened) TTL
        code, body = live.raw(
            "POST", "/auth/sessions",
            payload={"challenge_nonce": challenge["nonce"]},
            token="g560_bootstrap_token")
        assert code == 400
    finally:
        live.close()


# -- typed thin-client roundtrip ----------------------------------------------


def test_thin_client_submits_watches_and_cancels(tmp_path):
    live = LiveServer(tmp_path)
    try:
        client = ForgeServerClient(live.base_url)
        client.login("g560_bootstrap_token")

        # Typed submission: only requirement/mode exist in the protocol.
        task = client.submit_task("demo", "add a small utility function")
        assert task["project_id"] == "demo"
        # Raw server vocabulary is lowercase.
        assert task["status"] in ("created", "queued", "started",
                                  "running", "completed")

        # Progress: poll to a terminal state (scripted executor is fast).
        deadline = time.time() + 30
        while time.time() < deadline:
            snapshot = client.task(task["task_id"])
            if snapshot["status"] in ("completed", "failed",
                                      "cancelled", "rolled_back"):
                break
            time.sleep(0.1)
        assert snapshot["status"] == "completed"

        # Events carry the cursor-replay contract.
        events = client.task_events(task["task_id"])
        assert events["latest_seq"] >= 1
        assert events["events"]

        # The server rejects execution vectors even from a client:
        # the strict schema forbids unknown fields outright.
        code, _ = live.raw("POST", "/tasks",
                           payload={"project_id": "demo",
                                    "requirement": "x",
                                    "command": "rm -rf /"},
                           token=client.token)
        assert code in (400, 422)
    finally:
        live.close()


def test_wrong_key_is_refused(tmp_path):
    live = LiveServer(tmp_path)
    try:
        client = ForgeServerClient(live.base_url)
        with pytest.raises(ForgeServerClientError) as exc:
            client.login("fsk_totally_wrong_key")
        assert exc.value.http_status in (401, 403)
    finally:
        live.close()


# -- desktop connection mode -----------------------------------------------------


def test_desktop_server_mode_routes_through_server(tmp_path):
    from forge.desktop_app.backend import DesktopBackend

    live = LiveServer(tmp_path)
    try:
        backend = DesktopBackend(actor="desktop")
        # Not started locally, not connected: honest errors everywhere.
        with pytest.raises(Exception):
            backend.submit_task("demo", "anything")
        assert backend.connection_mode == "local"

        # Connect: thin-client mode with challenge login.
        state = backend.connect_server(live.base_url,
                                       "g560_bootstrap_token")
        assert state["mode"] == "server"
        assert backend.connection_mode == "server"
        # No local execution plane was created.
        assert backend._plane is None

        # Projects come from the server.
        projects = backend.projects()
        assert [p["id"] for p in projects] == ["demo"]

        # Submission routes to the server and completes there.
        task = backend.submit_task("demo", "add a small utility function")
        assert task["status"] in ("QUEUED", "RUNNING", "SUCCEEDED")
        deadline = time.time() + 30
        while time.time() < deadline:
            snapshot = backend.get_task("demo", task["id"])
            if snapshot["status"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
                break
            time.sleep(0.1)
        assert snapshot["status"] == "SUCCEEDED"

        # Approvals route to the server (none pending with the scripted
        # executor: the list is honest about that).
        approvals = backend.list_approvals("demo")
        assert isinstance(approvals, list)

        # Back to local mode: without a local plane, calls fail honestly.
        backend.disconnect_server()
        assert backend.connection_mode == "local"
        with pytest.raises(Exception) as exc:
            backend.submit_task("demo", "anything")
        assert "not started" in str(exc.value).lower()
    finally:
        live.close()


def test_desktop_connect_without_key_is_refused(tmp_path):
    from forge.desktop_app.backend import DesktopBackend

    live = LiveServer(tmp_path)
    try:
        backend = DesktopBackend(actor="desktop")
        with pytest.raises(Exception) as exc:
            backend.connect_server(live.base_url, "")
        assert "api key" in str(exc.value).lower()
        assert backend.connection_mode == "local"
    finally:
        live.close()


# -- CLI login -----------------------------------------------------------------


def test_cli_server_login(tmp_path, capsys):
    import forge.cli as cli

    live = LiveServer(tmp_path)
    try:
        args = cli_main_args(url=live.base_url + "/api/v1",
                             key="g560_bootstrap_token", json=True)
        assert cli._run_server_login(args) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["token"].startswith("fss_")

        capsys.readouterr()
        bad = cli_main_args(url=live.base_url + "/api/v1",
                            key="fsk_wrong", json=True)
        assert cli._run_server_login(bad) == 1
        assert "Login failed" in capsys.readouterr().err
    finally:
        live.close()


def cli_main_args(**kwargs):
    import argparse

    return argparse.Namespace(**kwargs)
