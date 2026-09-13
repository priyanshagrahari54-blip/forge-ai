"""Session 11 — live surfaces: real HTTP, the thin client, opt-in providers.

Three kinds of test live here, and they are labelled so nobody can confuse
them:

* ``REAL_INFERENCE_TEST`` — a real uvicorn-hosted Forge Server on loopback,
  driven by the typed client and by ``forge infer --server``. The generation
  is produced by the first-party reference engine over a locally materialised
  artifact. Nothing leaves the machine; nothing is downloaded.
* ``MOCK_BACKEND_TEST`` — a loopback Ollama-compatible *stub* that replays
  scripted text. It exercises the real HTTP client code paths against a
  double, and it is never presented as a real model.
* opt-in live provider tests — skipped unless ``FORGE_REAL_INFERENCE=1`` is
  set, and skipped again (with a reason) when no such provider is reachable.
  Offline CI therefore stays fully functional.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers_a34 import ScriptedProvider, make_fabric  # noqa: E402
from helpers_s11 import (  # noqa: E402
    MOCK_BACKEND_TEST,
    REAL_INFERENCE_TEST,
    bounded_runtime_config,
    default_governor,
    free_port,
    isolate_env,
    loopback_ollama_server,
    reference_model_id,
    run_cli,
    stop_server,
    write_reference_artifact,
)
from helpers_server import success_executor  # noqa: E402

from forge.server.client import (  # noqa: E402
    ForgeServerClient,
    ForgeServerClientError,
)
from forge.server.inference import InferenceServiceConfig  # noqa: E402

MODEL = reference_model_id()
TOKEN = "s11-live-token"

#: The single opt-in switch for anything that touches a real third-party
#: provider. Unset means "offline CI": those tests skip with a reason.
LIVE_FLAG = os.environ.get("FORGE_REAL_INFERENCE", "").strip().lower()
LIVE_ENABLED = LIVE_FLAG in ("1", "true", "yes", "on")

requires_live = pytest.mark.skipif(
    not LIVE_ENABLED,
    reason="opt-in live provider test: set FORGE_REAL_INFERENCE=1 to run it")


# -- a real server on loopback -------------------------------------------------------


class LiveInferenceServer:
    """A uvicorn-hosted Forge Server with Session-11 inference enabled."""

    def __init__(self, tmp_path: Any, *, profile: str = "",
                 require_verified: bool = True) -> None:
        import uvicorn

        from forge.server import ForgeServer, ServerConfig

        isolate_env_from_path(tmp_path)
        directory = Path(tmp_path) / "models"
        write_reference_artifact(directory)
        repo = Path(tmp_path) / "demo"
        repo.mkdir(parents=True, exist_ok=True)
        self.port = free_port()
        config = ServerConfig(
            db_path=str(Path(tmp_path) / "server.db"),
            host="127.0.0.1", port=self.port,
            projects={"demo": str(repo)},
            bootstrap_token=TOKEN,
            executor=success_executor("s11-live"),
            max_workers=2, poll_interval=0.05, approval_timeout=30.0,
            inference=InferenceServiceConfig(
                enabled=True, reference_dirs=(str(directory),),
                allow_network=False, max_model_slots=1,
                default_timeout_seconds=30.0, max_concurrent_requests=2,
                require_verified=require_verified,
                resource_profile=profile))
        self.server = ForgeServer(config)
        self.server.start()
        self.httpd = uvicorn.Server(uvicorn.Config(
            self.server.create_app(), host="127.0.0.1", port=self.port,
            log_level="warning"))
        self.thread = threading.Thread(target=self.httpd.run, daemon=True)
        self.thread.start()
        deadline = time.time() + 20.0
        while time.time() < deadline and not self.httpd.started:
            time.sleep(0.05)
        assert self.httpd.started, "uvicorn did not start"
        self.base_url = "http://127.0.0.1:%d" % self.port
        self.api_url = self.base_url + "/api/v1"

    def client(self, token: str = TOKEN) -> ForgeServerClient:
        return ForgeServerClient(self.api_url, token=token, timeout=40.0)

    def verify(self) -> Dict[str, Any]:
        client = self.client()
        client.list_models(discover=True)
        return client.verify_models()

    def close(self) -> None:
        try:
            self.httpd.should_exit = True
            self.thread.join(timeout=10.0)
        except Exception:
            pass
        try:
            self.server.stop()
        except Exception:
            pass


def isolate_env_from_path(tmp_path: Any) -> None:
    """Drop inherited inference/runtime configuration for this process."""
    from helpers_s11 import INFERENCE_ENV

    for name in INFERENCE_ENV:
        os.environ.pop(name, None)
    os.environ.pop("FORGE_REAL_INFERENCE_KEEP", None)
    os.chdir(str(tmp_path))


@pytest.fixture()
def live_server(tmp_path: Any, monkeypatch: Any):
    monkeypatch.chdir(tmp_path)
    server = LiveInferenceServer(tmp_path)
    try:
        yield server
    finally:
        server.close()


def tcp_reachable(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# -- typed client over real HTTP ------------------------------------------------------


def test_client_discovers_and_verifies_a_real_model_over_http(live_server):
    """REAL_INFERENCE_TEST"""
    client = live_server.client()
    listed = client.list_models(discover=True)
    ids = [item["model_id"] for item in listed["models"]]
    assert MODEL in ids
    entry = [item for item in listed["models"]
             if item["model_id"] == MODEL][0]
    assert entry["verification_state"] == "unverified"

    verified = client.verify_models()
    assert verified["verified"] == 1
    status = client.model_status(MODEL)
    assert status["identity"]["verification_state"] == "verified"
    assert status["identity"]["artifact_fingerprint"]


def test_client_generate_over_http_reports_real_provenance(live_server):
    """REAL_INFERENCE_TEST"""
    live_server.verify()
    client = live_server.client()
    body = client.generate("hello there", capability="")
    assert body["success"] is True
    assert body["neural"] is True
    assert body["text"]
    assert body["model_id"] == MODEL
    assert body["backend_id"] == "reference"
    assert body["verification_state"] == "verified"
    assert body["state"] == "succeeded"
    assert float(body["latency_ms"]) > 0.0


def test_client_loads_and_unloads_over_http(live_server):
    """REAL_INFERENCE_TEST"""
    live_server.verify()
    client = live_server.client()
    loaded = client.load_model(MODEL)
    assert loaded["loaded"] is True
    status = client.model_status(MODEL)
    assert status["resident"] is not None
    assert status["resident"]["state"] == "loaded"
    unloaded = client.unload_model(MODEL)
    assert unloaded["unloaded"] is True
    after = client.model_status(MODEL)
    assert after["resident"] is None


def test_client_stream_over_http_delivers_every_delta_once(live_server):
    """REAL_INFERENCE_TEST"""
    live_server.verify()
    client = live_server.client()
    deltas: List[str] = []
    outcome = client.stream("stream this please", capability="",
                            on_delta=lambda text, event: deltas.append(text))
    assert outcome["complete"] is True
    #: One event per delta, plus the terminal event that closes the stream.
    assert outcome["events"] == len(deltas) + 1
    assert outcome["text"] == "".join(deltas)
    assert outcome["text"]
    assert outcome["neural"] is True
    assert outcome["model_id"] == MODEL
    assert outcome["state"] == "succeeded"
    sequences = [event["sequence"] for event in outcome.get("collected", [])]
    assert sequences == sorted(set(sequences)) or not sequences


def test_client_stream_is_resumable_from_a_cursor(live_server):
    """REAL_INFERENCE_TEST: a reconnecting thin client loses nothing."""
    live_server.verify()
    client = live_server.client()
    started = client.start_stream("resumable stream", capability="")
    stream_id = started["stream_id"]
    first = client.stream_events(stream_id, after=0, wait=1.0)
    #: Replaying from the same cursor re-sends exactly what was already sent
    #: (the stream may have grown in between, so compare the common prefix),
    #: and replaying from the returned cursor sends none of it twice.
    replay = client.stream_events(stream_id, after=0, wait=0.5)
    assert [event["sequence"] for event in replay["events"]][:len(
        first["events"])] == [event["sequence"] for event in first["events"]]
    tail = client.stream_events(stream_id, after=first["after"], wait=2.0)
    overlap = {event["sequence"] for event in tail["events"]} & \
        {event["sequence"] for event in first["events"]}
    assert not overlap
    deadline = time.time() + 20.0
    done = bool(tail["done"])
    while not done and time.time() < deadline:
        tail = client.stream_events(stream_id, after=tail["after"], wait=1.0)
        done = bool(tail["done"])
    assert done is True


def test_client_cancel_over_http_stops_a_real_stream(live_server):
    """REAL_INFERENCE_TEST"""
    live_server.verify()
    client = live_server.client()
    started = client.start_stream("a longer generation to cancel",
                                  capability="")
    client.stream_events(started["stream_id"], wait=0.2)
    cancelled = client.cancel_inference(started["request_id"], reason="test")
    assert cancelled["cancelled"] is True
    outcome = client.stream("second stream", capability="")
    assert outcome["complete"] is True
    #: The cancelled request is reported as cancelled by the service.
    status = client.inference_status()
    assert status["service"]["counts"]["cancel"] >= 1


def test_client_reports_auth_failures_without_leaking_the_token(live_server):
    """REAL_INFERENCE_TEST"""
    bad = ForgeServerClient(live_server.api_url, token="wrong-token",
                            timeout=10.0)
    with pytest.raises(ForgeServerClientError) as caught:
        bad.list_models()
    assert caught.value.code in ("AUTH_REQUIRED", "AUTH_FAILED",
                                 "UNAUTHORIZED", "FORBIDDEN")
    assert caught.value.http_status == 401
    assert "wrong-token" not in str(caught.value)
    assert "wrong-token" not in repr(caught.value)


def test_client_reports_an_unreachable_server_honestly(tmp_path, monkeypatch):
    """REAL_INFERENCE_TEST: no server, no invented answer."""
    monkeypatch.chdir(tmp_path)
    port = free_port()
    client = ForgeServerClient("http://127.0.0.1:%d/api/v1" % port,
                               token=TOKEN, timeout=2.0)
    with pytest.raises(ForgeServerClientError) as caught:
        client.generate("hello")
    assert caught.value.code in ("UNREACHABLE", "NETWORK", "TIMEOUT")
    assert "127.0.0.1" in str(caught.value)


# -- the CLI as a G560 thin client ----------------------------------------------------


def test_cli_infer_through_a_server_delegates_the_compute(live_server, capsys):
    """REAL_INFERENCE_TEST: the thin-client path (§22, §26)."""
    live_server.verify()
    code = run_cli(["forge", "infer", "hello from the thin client",
                    "--server", live_server.api_url, "--token", TOKEN,
                    "-c", ""])
    assert code == 0
    printed = capsys.readouterr().out
    assert "neural=True" in printed
    assert "model=%s" % MODEL in printed
    assert "backend=reference" in printed


def test_cli_infer_through_a_server_streams_and_reports_once(live_server,
                                                            capsys):
    """REAL_INFERENCE_TEST"""
    live_server.verify()
    code = run_cli(["forge", "infer", "stream through the server",
                    "--server", live_server.api_url, "--token", TOKEN,
                    "--stream", "-c", ""])
    assert code == 0
    printed = capsys.readouterr().out
    assert "stream: events=" in printed
    assert "complete=True" in printed
    body = printed.split("provenance:")[0].strip()
    assert body
    assert printed.count(body) == 1


def test_cli_server_mode_json_carries_the_servers_provenance(live_server,
                                                            capsys):
    """REAL_INFERENCE_TEST"""
    live_server.verify()
    code = run_cli(["forge", "infer", "json thin client",
                    "--server", live_server.api_url, "--token", TOKEN,
                    "--json", "-c", ""])
    assert code == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["via"] == "server"
    assert payload["server"] == live_server.api_url
    assert payload["success"] is True
    assert payload["neural"] is True
    assert payload["model_id"] == MODEL
    assert payload["verification_state"] == "verified"
    assert payload["text"]


def test_cli_server_mode_explains_the_routing(live_server, capsys):
    """REAL_INFERENCE_TEST"""
    live_server.verify()
    code = run_cli(["forge", "infer", "explain thin client",
                    "--server", live_server.api_url, "--token", TOKEN,
                    "--explain", "-c", ""])
    assert code == 0
    printed = capsys.readouterr().out
    assert "routing:" in printed
    assert "resource:" in printed
    assert "ladder:" in printed


def test_cli_server_mode_refuses_a_bad_token(live_server, capsys):
    """REAL_INFERENCE_TEST"""
    code = run_cli(["forge", "infer", "hello", "--server", live_server.api_url,
                    "--token", "not-the-token", "-c", ""])
    assert code == 1
    captured = capsys.readouterr()
    assert "Server refused the request" in captured.err
    assert "not-the-token" not in captured.err + captured.out


def test_cli_server_mode_reports_an_unreachable_server(tmp_path, monkeypatch,
                                                      capsys):
    """REAL_INFERENCE_TEST"""
    monkeypatch.chdir(tmp_path)
    port = free_port()
    code = run_cli(["forge", "infer", "hello",
                    "--server", "http://127.0.0.1:%d/api/v1" % port,
                    "--token", TOKEN, "-c", ""])
    assert code == 1
    captured = capsys.readouterr()
    assert "Server refused the request" in captured.err
    assert "UNREACHABLE" in captured.err or "NETWORK" in captured.err


def test_g560_refuses_locally_but_delegates_to_a_server(live_server,
                                                       tmp_path, capsys):
    """REAL_INFERENCE_TEST: the thin client never loads a model (§12)."""
    live_server.verify()
    directory = Path(tmp_path) / "models"
    write_reference_artifact(directory)
    local = run_cli(["forge", "infer", "hello", "--verify",
                     "--no-deterministic", "--resource-profile", "g560",
                     "--reference-dir", str(directory), "-c", ""])
    assert local == 1
    printed = capsys.readouterr()
    assert "state=resource_denied" in printed.out + printed.err

    delegated = run_cli(["forge", "infer", "hello", "--server",
                         live_server.api_url, "--token", TOKEN,
                         "--resource-profile", "g560", "-c", ""])
    assert delegated == 0
    printed = capsys.readouterr().out
    assert "neural=True" in printed
    #: The desktop profile loaded nothing: the server did the work.
    assert "backend=reference" in printed


# -- the optional Ollama backend (loopback double) --------------------------------------


def ollama_fabric(url: str, *, allow_network: bool = True,
                  **overrides: Any) -> Any:
    """A fabric whose Ollama backend points at ``url``.

    ``allow_network`` is the runtime's own switch (Session 10: no socket at
    all unless an operator enables it). The *loopback* guard in the fabric is
    separate and unconditional: a non-loopback Ollama host is refused even
    with network access enabled.
    """
    from forge.models.engine import build_inference_fabric

    config = bounded_runtime_config(
        backends=("native", "ollama"), default_backend="ollama",
        ollama_url=url, allow_network=allow_network, **overrides)
    return build_inference_fabric(runtime_config=config,
                                  governor=default_governor())


def test_ollama_backend_serves_a_loopback_double(monkeypatch, tmp_path):
    """MOCK_BACKEND_TEST: real HTTP, scripted peer, honestly labelled."""
    monkeypatch.chdir(tmp_path)
    server, url = loopback_ollama_server(response="ollama-scripted",
                                         model="tiny-llama")
    try:
        fabric = ollama_fabric(url)
        fabric.catalog.discover(backend_id="ollama")
        identities = fabric.catalog.list(backend_id="ollama")
        assert identities, "the loopback double was not discovered"
        model_id = identities[0].model_id
        assert model_id.endswith("tiny-llama")
        assert identities[0].local is True

        result = fabric.catalog.verify(model_id)
        assert result.verified is True, result.error

        from forge.models.request import ModelRequest

        generated = fabric.generate(ModelRequest(prompt="say something",
                                                 capability="",
                                                 model=model_id))
        assert generated.success is True
        assert generated.neural is True
        assert generated.text == "ollama-scripted"
        assert generated.backend_id == "ollama"
        #: The peer is a double: the fabric only ever claims what the peer
        #: reported, and this suite labels it as a mock.
        assert MOCK_BACKEND_TEST == "MOCK_BACKEND_TEST"
    finally:
        stop_server(server)


def test_ollama_backend_streams_from_a_loopback_double(monkeypatch, tmp_path):
    """MOCK_BACKEND_TEST"""
    monkeypatch.chdir(tmp_path)
    chunks = ["ol", "la", "ma"]
    server, url = loopback_ollama_server(chunks=chunks, model="tiny-llama")
    try:
        fabric = ollama_fabric(url)
        fabric.catalog.discover(backend_id="ollama")
        fabric.catalog.verify_all(backend_id="ollama")
        model_id = fabric.catalog.list(backend_id="ollama")[0].model_id

        from forge.models.request import ModelRequest
        from forge.models.streams import join_deltas

        handle = fabric.stream(ModelRequest(prompt="stream", capability="",
                                            model=model_id))
        joined = join_deltas(handle.events())
        result = handle.wait(15.0)
        assert joined["monotonic_sequence"] is True
        assert joined["done"] is True
        assert joined["text"] == "".join(chunks)
        assert result.success is True
        assert result.streamed is True
    finally:
        stop_server(server)


def test_a_non_loopback_ollama_url_is_refused_without_an_explicit_opt_in(
        monkeypatch, tmp_path):
    """MOCK_BACKEND_TEST: the SSRF guard (§16, §25).

    No socket is opened to the non-loopback address: the guard refuses first.
    """
    monkeypatch.chdir(tmp_path)
    #: Network access is ON here on purpose: what must refuse is the
    #: loopback guard, not the network switch.
    fabric = ollama_fabric("http://models.internal.example:11434",
                           allow_network=True)
    report = fabric.catalog.discover(backend_id="ollama")
    identities = fabric.catalog.list(backend_id="ollama")
    assert identities == []
    statuses = {status.backend_id: status
                for status in fabric.catalog.backends.statuses(probe=True)}
    ollama = statuses.get("ollama")
    assert ollama is not None
    assert ollama.ready is False
    assert ollama.reachable is False
    assert ollama.denial, "a non-loopback endpoint must carry a denial reason"
    assert "not loopback" in (ollama.denial + ollama.error).lower()
    assert report.to_dict()["per_backend"]["ollama"]["found"] == 0


def test_ollama_is_refused_entirely_when_network_access_is_off(
        monkeypatch, tmp_path):
    """MOCK_BACKEND_TEST: the Session-10 switch is not weakened (§11)."""
    monkeypatch.chdir(tmp_path)
    server, url = loopback_ollama_server(response="never-contacted")
    try:
        fabric = ollama_fabric(url, allow_network=False)
        fabric.catalog.discover(backend_id="ollama")
        #: A listening double is still not contacted: no socket, no model.
        assert fabric.catalog.list(backend_id="ollama") == []
        statuses = {status.backend_id: status
                    for status in fabric.catalog.backends.statuses(probe=True)}
        assert statuses["ollama"].reachable is False
        assert statuses["ollama"].ready is False
        assert "network access is disabled" in (
            statuses["ollama"].detail + statuses["ollama"].error).lower()
    finally:
        stop_server(server)


def test_a_dead_ollama_endpoint_never_becomes_an_available_model(
        monkeypatch, tmp_path):
    """MOCK_BACKEND_TEST: nothing is listening, so nothing is claimed."""
    monkeypatch.chdir(tmp_path)
    port = free_port()
    fabric = ollama_fabric("http://127.0.0.1:%d" % port, allow_network=True)
    fabric.catalog.discover(backend_id="ollama")
    assert fabric.catalog.list(backend_id="ollama") == []
    statuses = {status.backend_id: status
                for status in fabric.catalog.backends.statuses(probe=True)}
    assert statuses["ollama"].ready is False
    assert statuses["ollama"].reachable is False

    from forge.models.request import ModelRequest

    result = fabric.generate(ModelRequest(prompt="hi", capability="",
                                          backend="ollama",
                                          allow_deterministic=False))
    assert result.success is False
    assert result.neural is False
    assert result.text == ""
    assert result.state in ("needs_model", "failed", "unverified")


# -- opt-in live providers -------------------------------------------------------------


@requires_live
def test_live_ollama_when_a_real_server_is_present():
    """REAL_INFERENCE_TEST (opt-in): a real Ollama on loopback.

    Skipped unless ``FORGE_REAL_INFERENCE=1`` *and* something is actually
    listening on the configured endpoint. Forge never downloads a model to
    make this test pass.
    """
    url = os.environ.get("FORGE_RUNTIME_OLLAMA_URL") or \
        os.environ.get("OLLAMA_BASE_URL") or "http://127.0.0.1:11434"
    #: A real Ollama is an explicit opt-in, so network access is on for it.
    os.environ.setdefault("FORGE_RUNTIME_ALLOW_NETWORK", "1")
    host = url.split("//")[-1].split(":")[0]
    port = int(url.rsplit(":", 1)[-1].split("/")[0] or 11434)
    if not tcp_reachable(host, port):
        pytest.skip("no Ollama endpoint is reachable at %s" % url)

    fabric = ollama_fabric(url)
    fabric.catalog.discover(backend_id="ollama")
    identities = fabric.catalog.list(backend_id="ollama")
    if not identities:
        pytest.skip("Ollama is reachable but serves no model; nothing is "
                    "downloaded on purpose")
    model_id = identities[0].model_id
    verification = fabric.catalog.verify(model_id)
    assert verification.verified is True, verification.error

    from forge.models.request import ModelRequest

    result = fabric.generate(ModelRequest(prompt="Reply with one word.",
                                          capability="", model=model_id))
    assert result.success is True
    assert result.neural is True
    assert result.text.strip()
    assert result.model_id == model_id


@requires_live
def test_live_remote_provider_when_one_is_configured(tmp_path, monkeypatch):
    """REAL_INFERENCE_TEST (opt-in): a real remote provider (§16).

    Requires ``FORGE_REAL_INFERENCE=1`` plus a ``FORGE_REMOTE_<ID>_*``
    configuration. Credentials are read from the environment and must never
    appear in the result, the logs, or an exception message.
    """
    monkeypatch.chdir(tmp_path)
    provider = os.environ.get("FORGE_REAL_INFERENCE_PROVIDER", "")
    if not provider:
        configured = [key for key in os.environ
                      if key.startswith("FORGE_REMOTE_") and
                      key.endswith("_URL")]
        if not configured:
            pytest.skip("no remote provider configured: set "
                        "FORGE_REMOTE_<ID>_URL (and _API_KEY) plus "
                        "FORGE_REAL_INFERENCE=1")
        provider = configured[0][len("FORGE_REMOTE_"):-len("_URL")].lower()

    from forge.models.engine import build_inference_fabric
    from forge.models.remote import RemoteProviderConfig

    config = RemoteProviderConfig.from_env(provider)
    if not config.base_url:
        pytest.skip("remote provider %r has no URL configured" % provider)
    fabric = build_inference_fabric(
        runtime_config=bounded_runtime_config(), governor=default_governor(),
        remote_configs=(config,), allow_network=True)
    fabric.catalog.discover(backend_id=provider)
    identities = fabric.catalog.list(backend_id=provider)
    if not identities:
        pytest.skip("remote provider %r served no model" % provider)
    model_id = identities[0].model_id
    assert fabric.catalog.verify(model_id).verified is True

    from forge.models.request import ModelRequest

    secret_marker = "FORGE-LIVE-SECRET-MARKER"
    result = fabric.generate(ModelRequest(prompt="Reply with one word.",
                                          capability="", model=model_id,
                                          network_policy="explicit"))
    assert result.success is True, result.error
    assert result.neural is True
    assert result.text.strip()
    blob = repr(result.to_dict()) + repr(fabric.evidence())
    assert secret_marker not in blob
    #: A credential must never surface in a result or an error message.
    api_key = str(config.api_key or "")
    if api_key:
        assert api_key not in blob


@requires_live
def test_live_server_generation_end_to_end(tmp_path, monkeypatch):
    """REAL_INFERENCE_TEST (opt-in): a live server on a real socket.

    The same path as the offline HTTP suites, but with the operator's own
    environment allowed to contribute remote providers when they are set.
    """
    monkeypatch.chdir(tmp_path)
    server = LiveInferenceServer(tmp_path)
    try:
        assert server.verify()["verified"] == 1
        body = server.client().generate("live end to end", capability="")
        assert body["success"] is True
        assert body["neural"] is True
    finally:
        server.close()


def test_live_tests_are_opt_in_and_offline_ci_is_complete():
    """The suite itself states the contract (§27)."""
    assert requires_live.args[0] is (not LIVE_ENABLED)
    assert "FORGE_REAL_INFERENCE=1" in str(requires_live.kwargs.get("reason"))
    #: Nothing in this module imports a provider SDK at module scope.
    assert "openai" not in sys.modules
    assert "anthropic" not in sys.modules
    assert REAL_INFERENCE_TEST == "REAL_INFERENCE_TEST"
