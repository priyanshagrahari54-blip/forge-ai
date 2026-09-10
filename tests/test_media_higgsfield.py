"""Higgsfield client: lifecycle, errors, retries over a real local HTTP stub."""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from forge.cli import main  # noqa: E402
from forge.media.higgsfield import (  # noqa: E402
    HiggsfieldAuthError,
    HiggsfieldClient,
    HiggsfieldConfigurationError,
    HiggsfieldCredentials,
    HiggsfieldCreditsError,
    HiggsfieldError,
    HiggsfieldNotFoundError,
    HiggsfieldTimeoutError,
    HiggsfieldValidationError,
    known_models,
)
from forge.tools.higgsfield import HiggsfieldTool  # noqa: E402


def run_cli(argv):
    with patch.object(sys, "argv", argv):
        main()


CREDS = HiggsfieldCredentials(key_id="test-key-id-1234", secret="s3cret")


class StubState:
    def __init__(self):
        self.mode = "happy"  # happy | flaky-status | needs-auth
        self.status_calls = 0
        self.polls = 0
        self.posts = 0
        self.auth_headers: list[str] = []
        self.bodies: list[dict] = []


class Handler(BaseHTTPRequestHandler):
    state: StubState = StubState()  # replaced per fixture

    def log_message(self, *args):  # silence stdlib logging
        pass

    def _send(self, code, payload, correlation="corr-stub-1"):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Correlation-ID", correlation)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _base(self):
        host, port = self.server.server_address[:2]
        return f"http://127.0.0.1:{port}"

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode() or "{}")
        except ValueError:
            body = {}
        self.state.auth_headers.append(
            self.headers.get("Authorization", ""))
        self.state.bodies.append(body if isinstance(body, dict) else {})
        if self.path.endswith("/cancel"):
            if "already-started" in self.path:
                self._send(400, {"detail": "already started"})
            else:
                self.send_response(202)
                self.send_header("X-Correlation-ID", "corr-cancel-1")
                self.send_header("Content-Length", "0")
                self.end_headers()
            return
        self.state.posts += 1
        if self.state.mode == "needs-auth":
            self._send(401, {"detail": "Invalid credentials"})
            return
        if not body.get("prompt"):
            self._send(422, {"detail": [{"msg": "prompt required"}]})
            return
        request_id = "req-happy-1"
        self._send(200, {
            "status": "queued", "request_id": request_id,
            "status_url": f"{self._base()}/requests/{request_id}/status",
            "cancel_url": f"{self._base()}/requests/{request_id}/cancel"})

    def do_GET(self):
        self.state.auth_headers.append(
            self.headers.get("Authorization", ""))
        if self.path == "/file/video.mp4":
            body = b"FAKEVIDEO" * 1000
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if "/requests/" not in self.path:
            self._send(404, {"detail": "no such request"})
            return
        request_id = self.path.split("/requests/")[1].split("/")[0]
        if request_id == "missing":
            self._send(404, {"detail": "Request not found"})
            return
        if request_id == "unpaid":
            self._send(403, {"detail": "Insufficient credits"})
            return
        if request_id == "bad":
            self._send(400, {"detail": "concurrency reached"})
            return
        if self.state.mode == "flaky-status":
            self.state.status_calls += 1
            if self.state.status_calls == 1:
                self._send(500, {"detail": "boom"})
                return
        if request_id == "slow":
            self.state.polls += 1
            self._send(200, {"status": "in_progress",
                             "request_id": request_id})
            return
        if request_id == "failed-job":
            self._send(200, {"status": "failed", "request_id": request_id,
                             "error": "Generation failed"})
            return
        if request_id == "vid":
            self._send(200, {
                "status": "completed", "request_id": request_id,
                "video": {"url": f"{self._base()}/file/video.mp4"}})
            return
        self._send(200, {
            "status": "completed", "request_id": request_id,
            "images": [{"url": f"{self._base()}/file/video.mp4"}]})


@pytest.fixture()
def stub():
    state = StubState()
    Handler.state = state
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield f"http://127.0.0.1:{port}", state
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _client(base_url, state_mode=None, stub_state=None):
    if state_mode is not None and stub_state is not None:
        stub_state.mode = state_mode
    return HiggsfieldClient(CREDS, base_url=base_url, timeout=10.0)


# -- credentials ------------------------------------------------------------------------


def test_credentials_from_env_primary_and_alias():
    env = {"HF_API_KEY_ID": "id1", "HF_API_KEY_SECRET": "sec1"}
    creds = HiggsfieldCredentials.from_env(env)
    assert creds is not None and creds.key_id == "id1"
    assert creds.auth_header == "Key id1:sec1"
    alias = {"HIGGSFIELD_API_KEY_ID": "id2",
             "HIGGSFIELD_API_KEY_SECRET": "sec2"}
    assert HiggsfieldCredentials.from_env(alias).key_id == "id2"
    assert HiggsfieldCredentials.from_env({}) is None
    assert HiggsfieldCredentials.from_env({"HF_API_KEY_ID": "x"}) is None


def test_credentials_never_leak_secret():
    assert "s3cret" not in repr(CREDS)
    assert CREDS.fingerprint() == "key …1234"
    with pytest.raises(HiggsfieldConfigurationError):
        HiggsfieldCredentials(key_id="", secret="")


def test_known_models_lists_documented_paths():
    assert known_models() == {
        "soul-standard-image": "higgsfield-ai/soul/v2/standard"}


# -- client validation ----------------------------------------------------------------------


def test_client_rejects_bad_config():
    with pytest.raises(HiggsfieldConfigurationError):
        HiggsfieldClient(CREDS, base_url="not a url")
    with pytest.raises(HiggsfieldConfigurationError):
        HiggsfieldClient(CREDS, timeout=0)
    with pytest.raises(HiggsfieldConfigurationError):
        HiggsfieldClient(CREDS, base_url="ftp://x/y")


def test_client_requires_credentials(monkeypatch):
    monkeypatch.delenv("HF_API_KEY_ID", raising=False)
    monkeypatch.delenv("HF_API_KEY_SECRET", raising=False)
    monkeypatch.delenv("HIGGSFIELD_API_KEY_ID", raising=False)
    monkeypatch.delenv("HIGGSFIELD_API_KEY_SECRET", raising=False)
    client = HiggsfieldClient()
    assert client.configured is False
    with pytest.raises(HiggsfieldConfigurationError):
        client.submit("soul-standard-image", {"prompt": "x"})
    with pytest.raises(HiggsfieldConfigurationError):
        client.get_status("req-1")


def test_submit_validates_arguments(stub):
    base_url, _ = stub
    client = _client(base_url)
    with pytest.raises(HiggsfieldConfigurationError):
        client.submit("", {"prompt": "x"})
    with pytest.raises(HiggsfieldConfigurationError):
        client.submit("m", {})
    with pytest.raises(HiggsfieldConfigurationError):
        client.generate_image("  ")
    with pytest.raises(HiggsfieldConfigurationError):
        client.get_status("")
    with pytest.raises(HiggsfieldConfigurationError):
        client.get_status("has space")
    with pytest.raises(HiggsfieldConfigurationError):
        client.wait("req-x", timeout=0)


# -- lifecycle over the stub -----------------------------------------------------------------


def test_submit_and_status_round_trip(stub):
    base_url, state = _client(stub[0]), stub[1]
    submission = base_url.submit("soul-standard-image",
                                 {"prompt": "A quiet alpine lake"})
    assert submission.request_id == "req-happy-1"
    assert submission.status == "queued"
    assert submission.correlation_id == "corr-stub-1"
    assert state.auth_headers and state.auth_headers[0] == (
        "Key test-key-id-1234:s3cret")
    assert state.bodies[0]["prompt"] == "A quiet alpine lake"
    snapshot = base_url.get_status(submission)
    assert snapshot.succeeded is True
    assert snapshot.terminal is True
    assert snapshot.output_urls() == [
        f"{stub[0]}/file/video.mp4"]
    assert snapshot.raise_for_terminal() is snapshot


def test_generate_image_uses_documented_path(stub):
    base_url, _ = stub
    submission = _client(base_url).generate_image("portrait")
    assert submission.request_id == "req-happy-1"


def test_wait_polls_to_terminal(stub):
    base_url, state = stub
    client = _client(base_url)
    submission = client.submit("soul-standard-image", {"prompt": "x"})
    snapshot = client.wait(submission, timeout=10.0, interval=0.05)
    assert snapshot.succeeded is True
    assert state.posts == 1  # POST never retried


def test_wait_timeout_raises(stub):
    base_url, _ = stub
    client = _client(base_url)
    with pytest.raises(HiggsfieldTimeoutError) as exc:
        client.wait("slow", timeout=0.3, interval=0.05)
    assert "in_progress" in str(exc.value)


def test_failed_terminal_raises_with_detail(stub):
    base_url, _ = stub
    snapshot = _client(base_url).get_status("failed-job")
    assert snapshot.terminal is True
    with pytest.raises(HiggsfieldError) as exc:
        snapshot.raise_for_terminal()
    assert "Generation failed" in str(exc.value)


def test_status_retries_transient_500(stub):
    base_url, state = stub
    client = _client(base_url, "flaky-status", state)
    snapshot = client.get_status("req-happy-1", attempts=3, deadline=10.0)
    assert snapshot.succeeded is True
    assert state.status_calls == 2


def test_auth_failure_maps_and_carries_correlation(stub):
    base_url, state = stub
    client = _client(base_url, "needs-auth", state)
    with pytest.raises(HiggsfieldAuthError) as exc:
        client.submit("soul-standard-image", {"prompt": "x"})
    assert exc.value.correlation_id == "corr-stub-1"


def test_error_envelope_mapping(stub):
    base_url, _ = stub
    client = _client(base_url)
    with pytest.raises(HiggsfieldNotFoundError):
        client.get_status("missing")
    with pytest.raises(HiggsfieldCreditsError):
        client.get_status("unpaid")
    with pytest.raises(HiggsfieldValidationError):
        client.get_status("bad")
    with pytest.raises(HiggsfieldValidationError):
        client.submit("m", {"prompt": ""})


def test_cancel_accepted_and_already_started(stub):
    base_url, _ = stub
    client = _client(base_url)
    assert client.cancel("req-happy-1") is True
    assert client.cancel("already-started") is False


def test_download_writes_file_and_confines(stub, tmp_path):
    base_url, _ = stub
    client = _client(base_url)
    target = client.download(f"{base_url}/file/video.mp4",
                             tmp_path / "dl")
    assert target.read_bytes() == b"FAKEVIDEO" * 1000
    named = client.download(f"{base_url}/file/video.mp4", tmp_path,
                            filename="clip.mp4")
    assert named.name == "clip.mp4"
    with pytest.raises(HiggsfieldConfigurationError):
        client.download("ftp://evil/x", tmp_path)
    with pytest.raises(HiggsfieldConfigurationError):
        client.download(f"{base_url}/file/video.mp4", tmp_path,
                        filename="../escape.mp4")


# -- tool wrapper -----------------------------------------------------------------------------


def _tool(base_url, monkeypatch):
    monkeypatch.setenv("HF_API_KEY_ID", "test-key-id-1234")
    monkeypatch.setenv("HF_API_KEY_SECRET", "s3cret")
    return HiggsfieldTool(base_url=base_url, timeout=10.0)


def test_tool_status_unconfigured(monkeypatch):
    monkeypatch.delenv("HF_API_KEY_ID", raising=False)
    monkeypatch.delenv("HF_API_KEY_SECRET", raising=False)
    monkeypatch.delenv("HIGGSFIELD_API_KEY_ID", raising=False)
    monkeypatch.delenv("HIGGSFIELD_API_KEY_SECRET", raising=False)
    report = HiggsfieldTool().status()
    assert report["configured"] is False
    assert "cloud.higgsfield.ai" in report["hint"]


def test_tool_full_cycle(stub, tmp_path, monkeypatch):
    base_url, _ = stub
    tool = _tool(base_url, monkeypatch)
    assert tool.status()["configured"] is True
    submitted = tool.submit("soul-standard-image", {"prompt": "lake"})
    assert submitted["submitted"] is True
    assert submitted["request_id"] == "req-happy-1"
    assert "s3cret" not in json.dumps(submitted)
    status = tool.get_status("vid")
    assert status["outputs"] == [f"{base_url}/file/video.mp4"]
    assert tool.cancel("req-happy-1")["cancelled"] is True
    assert tool.cancel("already-started")["cancelled"] is False
    downloaded = tool.download(f"{base_url}/file/video.mp4",
                               str(tmp_path))
    assert downloaded["ok"] is True


def test_tool_submit_reports_auth_failure(stub, monkeypatch):
    base_url, state = stub
    state.mode = "needs-auth"
    tool = _tool(base_url, monkeypatch)
    report = tool.submit("soul-standard-image", {"prompt": "x"})
    assert report["submitted"] is False
    assert report["retryable"] is False
    assert report["correlation_id"] == "corr-stub-1"


# -- CLI ----------------------------------------------------------------------------------------


def test_cli_higgsfield_status_json(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HF_API_KEY_ID", raising=False)
    monkeypatch.delenv("HF_API_KEY_SECRET", raising=False)
    monkeypatch.delenv("HIGGSFIELD_API_KEY_ID", raising=False)
    monkeypatch.delenv("HIGGSFIELD_API_KEY_SECRET", raising=False)
    with pytest.raises(SystemExit) as exc:
        run_cli(["forge", "higgsfield", "status", "--json"])
    assert exc.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["configured"] is False


def test_cli_higgsfield_submit_without_creds(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HF_API_KEY_ID", raising=False)
    monkeypatch.delenv("HF_API_KEY_SECRET", raising=False)
    monkeypatch.delenv("HIGGSFIELD_API_KEY_ID", raising=False)
    monkeypatch.delenv("HIGGSFIELD_API_KEY_SECRET", raising=False)
    with pytest.raises(SystemExit) as exc:
        run_cli(["forge", "higgsfield", "submit", "--prompt", "lake"])
    assert exc.value.code == 1
    assert "configured" in capsys.readouterr().out
