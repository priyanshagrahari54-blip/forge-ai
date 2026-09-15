"""Shared fixtures for the Session-11 inference fabric suites.

Test policy labels are part of the contract, not decoration: every test in
these suites declares which kind of inference it exercised.

``REAL_INFERENCE_TEST``
    An actual model ran: a real forward pass over real (tiny, first-party,
    locally materialised) weights, or a live provider when the operator opted
    in with ``FORGE_REAL_INFERENCE=1``.
``MOCK_BACKEND_TEST``
    A scripted/loopback double answered. Nothing here may ever be described
    as real inference, and the doubles are labelled as such in their output.
``DETERMINISTIC_TEST``
    The non-neural fallback rung (or pure logic) was exercised.

Live provider tests are opt-in: without ``FORGE_REAL_INFERENCE=1`` they skip,
so offline CI stays fully functional and never touches the network.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).parent))

from helpers_a81 import ScriptedBackend, make_runtime, wait_until  # noqa: E402,F401

#: Test policy labels (asserted in test names/docstrings and reports).
REAL_INFERENCE_TEST = "REAL_INFERENCE_TEST"
MOCK_BACKEND_TEST = "MOCK_BACKEND_TEST"
DETERMINISTIC_TEST = "DETERMINISTIC_TEST"

#: Opt-in switch for live provider tests (Ollama / remote HTTP).
LIVE_ENV = "FORGE_REAL_INFERENCE"


def live_inference_enabled() -> bool:
    """True only when an operator explicitly opted into live tests."""
    return str(os.environ.get(LIVE_ENV, "")).strip().lower() in (
        "1", "true", "yes", "on")


def live_reason() -> str:
    return ("live provider tests are opt-in: set %s=1 (and the endpoint "
            "environment variables) to run them" % LIVE_ENV)


def bounded_runtime_config(**overrides: Any) -> Any:
    """A runtime config with test-sized bounds (never the ambient env)."""
    from forge.runtime.model_runtime import RuntimeConfig

    values: Dict[str, Any] = {
        "default_backend": "native",
        "backends": ("native",),
        "allow_network": False,
        "timeout_seconds": 5.0,
        "max_timeout_seconds": 30.0,
        "chunk_timeout_seconds": 2.0,
        "health_timeout_seconds": 2.0,
        "history_size": 50,
        "retries": 0,
    }
    values.update(overrides)
    return RuntimeConfig(**values).validate()


def write_reference_artifact(directory: Any, name: str = "reference-clm",
                             **config: Any) -> str:
    """Materialise the tiny first-party reference artifact on disk.

    The repository ships **no** weights and nothing is downloaded: the writer
    fills a small shape with deterministic pseudo-random values. The artifact
    is honest about what it is (a path-verification model with no agentic
    capability) and its identity is fingerprinted.
    """
    from forge.models.reference_engine import (ReferenceArtifactWriter,
                                               ReferenceModelConfig)

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ("%s.forgeref" % name)
    ReferenceArtifactWriter.write(str(path),
                                  ReferenceModelConfig(name=name, **config),
                                  overwrite=True)
    return str(path)


def reference_fabric(directory: Any, *, governor: Any = None,
                     discover: bool = True, verify: bool = False,
                     name: str = "reference-clm", **fabric_kwargs: Any) -> Any:
    """A fabric with one REAL local model (``REAL_INFERENCE_TEST``)."""
    from forge.models.engine import build_inference_fabric

    path = write_reference_artifact(directory, name=name)
    fabric = build_inference_fabric(
        governor=governor, runtime_config=bounded_runtime_config(),
        reference_dirs=(str(Path(path).parent),), allow_network=False,
        **fabric_kwargs)
    if discover:
        fabric.catalog.discover()
    if verify:
        fabric.catalog.verify_all()
    return fabric


def reference_model_id(name: str = "reference-clm") -> str:
    return "reference:%s" % name


class ScriptedModelBackend(ScriptedBackend):
    """A scripted double that also reports one model (``MOCK_BACKEND_TEST``).

    Everything it claims is scripted by the test: the model id, the context
    window, the capabilities, and the text it "generates". Its metadata says
    ``test_double=True`` so no report can mistake it for a real model, and its
    description says so in plain words.
    """

    def __init__(self, name: str = "scripted", response: str = "scripted-ok",
                 chunks: Optional[List[str]] = None,
                 model_name: str = "scripted-model",
                 context_window: int = 4096,
                 capabilities: Tuple[str, ...] = ("coding",),
                 size_bytes: int = 2048, local: bool = True,
                 requires_network: bool = False,
                 parameter_count: Optional[int] = 1234,
                 delay: float = 0.0, chunk_delay: float = 0.0,
                 kind: str = "custom") -> None:
        super().__init__(name=name, response=response, chunks=chunks,
                         delay=delay, chunk_delay=chunk_delay, kind=kind)
        self.description = ("Test double (MOCK_BACKEND_TEST): replays "
                            "caller-supplied text; not a real model.")
        self.local = bool(local)
        self.requires_network = bool(requires_network)
        self.model_name = model_name
        self.context_window = int(context_window)
        self.model_capabilities = tuple(capabilities or ())
        self.size_bytes = int(size_bytes)
        self.parameter_count = parameter_count
        self.loaded_models: List[str] = []
        self.unloaded_models: List[str] = []
        self.list_calls = 0

    def list_models(self) -> List[Any]:
        from forge.runtime.model_runtime import RuntimeModel

        self.list_calls += 1
        metadata: Dict[str, Any] = {"test_double": True,
                                    "label": MOCK_BACKEND_TEST}
        if self.parameter_count is not None:
            metadata["parameter_count"] = int(self.parameter_count)
        return [RuntimeModel(
            model_id=RuntimeModel.make_id(self.name, self.model_name),
            name=self.model_name, backend=self.name, kind="text",
            capabilities=self.model_capabilities,
            context_window=self.context_window, max_output_tokens=512,
            size_bytes=self.size_bytes, format="unknown", local=self.local,
            metadata=metadata)]

    def load_model(self, model: Any, token: Any = None) -> Any:
        if token is not None:
            token.raise_if_cancelled()
        self.loaded_models.append(model.model_id)
        model.loaded = True
        return model

    def unload_model(self, model_id: str) -> bool:
        self.unloaded_models.append(model_id)
        return True


def scripted_fabric(response: str = "scripted-ok", *,
                    chunks: Optional[List[str]] = None,
                    backend_name: str = "scripted", governor: Any = None,
                    discover: bool = True, verify: bool = True,
                    model_name: str = "scripted-model",
                    **backend_kwargs: Any) -> Tuple[Any, ScriptedModelBackend]:
    """A fabric over a scripted runtime backend (``MOCK_BACKEND_TEST``).

    The double answers with exactly the text the test asked for. It is never
    presented as real inference: the backend description says "test double",
    and suites that use it assert on the mock's own labels.
    """
    from forge.models.engine import InferenceFabric

    backend = ScriptedModelBackend(name=backend_name, response=response,
                                   chunks=chunks, model_name=model_name,
                                   **backend_kwargs)
    #: The scripted backend reports one model so discovery has something to
    #: register; the model id makes its provenance obvious in any output.
    runtime = make_runtime(backend, default_backend=backend_name,
                           timeout_seconds=5.0, max_timeout_seconds=30.0,
                           chunk_timeout_seconds=2.0)
    fabric = InferenceFabric.from_runtime(runtime, governor=governor)
    if discover:
        fabric.catalog.discover(backend_id=backend_name)
    if verify:
        fabric.catalog.verify_all(backend_id=backend_name)
    return fabric, backend


def default_governor() -> Any:
    from forge.core.resource_governor import ResourceGovernor, select_profile

    return ResourceGovernor(select_profile("default"))


def g560_governor() -> Any:
    """The Win7/32-bit/2GB profile: local model loading is denied outright."""
    from forge.core.resource_governor import ResourceGovernor, select_profile

    return ResourceGovernor(select_profile("g560"))


def loopback_ollama_server(chunks: Optional[List[str]] = None,
                           response: str = "ollama-scripted",
                           model: str = "tiny-llama") -> Tuple[Any, str]:
    """Start a loopback Ollama-compatible stub (``MOCK_BACKEND_TEST``).

    Bound to 127.0.0.1 on an ephemeral port. It is a *double*: it replays
    scripted text and says so in its responses. Never labelled real.
    """
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    payload_chunks = list(chunks) if chunks is not None else [response]
    calls: List[Dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:  # silence stderr
            return None

        def _send(self, code: int, body: bytes,
                  content_type: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - http.server contract
            if self.path.startswith("/api/tags"):
                self._send(200, json.dumps({"models": [
                    {"name": model, "model": model, "size": 1024,
                     "details": {"family": "llama",
                                 "parameter_size": "1B"}}]}).encode("utf-8"))
            elif self.path.startswith("/api/version"):
                self._send(200, json.dumps({"version": "0.0.0-test"}
                                           ).encode("utf-8"))
            else:
                self._send(404, b'{"error":"not found"}')

        def do_POST(self) -> None:  # noqa: N802 - http.server contract
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                request = json.loads(raw.decode("utf-8") or "{}")
            except ValueError:
                request = {}
            calls.append({"path": self.path, "request": request})
            text = "".join(payload_chunks)
            if self.path.startswith("/api/generate"):
                if request.get("stream"):
                    #: Ollama streams NDJSON: one object per line, then a
                    #: terminal ``done`` object.
                    body = b""
                    for index, chunk in enumerate(payload_chunks):
                        body += json.dumps({
                            "model": model, "created_at": time.time(),
                            "response": chunk, "done": False,
                            "eval_count": index + 1}).encode("utf-8") + b"\n"
                    body += json.dumps({
                        "model": model, "created_at": time.time(),
                        "response": "", "done": True,
                        "eval_count": len(payload_chunks)}).encode("utf-8")
                    self._send(200, body + b"\n",
                               content_type="application/x-ndjson")
                else:
                    #: A non-streaming call gets exactly one JSON object.
                    self._send(200, json.dumps({
                        "model": model, "created_at": time.time(),
                        "response": text, "done": True,
                        "eval_count": len(text)}).encode("utf-8"))
            elif self.path.startswith("/api/chat"):
                self._send(200, json.dumps({
                    "model": model, "done": True,
                    "message": {"role": "assistant", "content": text}}
                ).encode("utf-8"))
            else:
                self._send(404, b'{"error":"not found"}')

    server = HTTPServer(("127.0.0.1", 0), Handler)
    #: Tests can assert what the double was actually asked to do.
    server.forge_calls = calls  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever,
                              name="s11-loopback-ollama", daemon=True)
    thread.start()
    return server, "http://127.0.0.1:%d" % server.server_address[1]


def stop_server(server: Any) -> None:
    try:
        server.shutdown()
    except Exception:
        pass
    try:
        server.server_close()
    except Exception:
        pass


def free_port() -> int:
    """An ephemeral loopback port (used by the live-socket tests)."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def run_cli(argv: List[str]) -> int:
    """Run ``forge`` with ``argv`` and return its exit code."""
    from unittest.mock import patch

    from forge.cli import main

    with patch.object(sys, "argv", list(argv)):
        try:
            main()
        except SystemExit as exc:
            code = exc.code
            if code is None:
                return 0
            return int(code) if isinstance(code, int) else 1
    return 0


#: Environment that must never leak into (or out of) an inference test.
INFERENCE_ENV = (
    "FORGE_SERVER_INFERENCE", "FORGE_SERVER_INFERENCE_REFERENCE_DIRS",
    "FORGE_SERVER_INFERENCE_MODEL_DIRS", "FORGE_SERVER_INFERENCE_ALLOW_NETWORK",
    "FORGE_SERVER_INFERENCE_REMOTE_PROVIDERS",
    "FORGE_SERVER_INFERENCE_OLLAMA_URL",
    "FORGE_SERVER_INFERENCE_ALLOW_REMOTE_OLLAMA",
    "FORGE_SERVER_INFERENCE_AUTO_VERIFY",
    "FORGE_SERVER_INFERENCE_ALLOW_UNVERIFIED",
    "FORGE_SERVER_INFERENCE_TIMEOUT", "FORGE_SERVER_INFERENCE_MAX_CONCURRENT",
    "FORGE_SERVER_INFERENCE_MAX_MODEL_SLOTS",
    "FORGE_SERVER_INFERENCE_MAX_RESIDENT_BYTES",
    "FORGE_SERVER_INFERENCE_IDLE_UNLOAD_SECONDS",
    "FORGE_REFERENCE_CREATE", "FORGE_RESOURCE_PROFILE", "FORGE_SERVER_TOKEN",
    "FORGE_RUNTIME_BACKEND", "FORGE_RUNTIME_BACKENDS",
    "FORGE_RUNTIME_ALLOW_NETWORK", "FORGE_RUNTIME_OLLAMA_URL",
    "FORGE_RUNTIME_MODEL_DIRS", "OLLAMA_BASE_URL", "OLLAMA_URL",
)


def isolate_env(monkeypatch, tmp_path: Any) -> Any:
    """A clean cwd with no inherited inference/runtime configuration."""
    for name in INFERENCE_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def loopback_provider_server(model: str = "acme-1",
                             response: str = "remote-scripted"
                             ) -> Tuple[Any, str, Dict[str, Any]]:
    """Start a loopback OpenAI-compatible provider double (``MOCK_BACKEND_TEST``).

    Bound to 127.0.0.1 on an ephemeral port, serving ``/v1/models`` and
    ``/v1/chat/completions``. It is a *double*: it replays scripted text and
    says so. Behaviour is controlled through the returned ``state`` dict so one
    server can serve a success, a 401, a redirect, a malformed body or a slow
    answer without restarting.

    ``server.forge_calls`` records every request the double actually received
    (``method``, ``path``, ``headers``, ``body``), so a test can prove that a
    refused request never reached the wire and that a redirect was not
    followed.
    """
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    state: Dict[str, Any] = {
        "model": model,
        "response": response,
        "status": 200,
        "content_type": "application/json",
        "body": None,             # raw bytes, when set, wins over "response"
        "reported_model": None,   # None => echo the requested model
        "sleep": 0.0,
        "redirect_to": "",
        "oversize": 0,
        "usage": {"prompt_tokens": 3, "completion_tokens": 4},
    }
    calls: List[Dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:  # silence stderr
            return None

        def _record(self, body: bytes = b"") -> None:
            calls.append({
                "method": self.command,
                "path": self.path,
                "headers": {key.lower(): value
                            for key, value in self.headers.items()},
                "body": body,
            })

        def _send(self, code: int, body: bytes,
                  content_type: str = "application/json",
                  extra: Optional[Dict[str, str]] = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _completion(self, requested_model: str) -> bytes:
            if state.get("body") is not None:
                return bytes(state["body"])
            if state.get("oversize"):
                return b"{" + b" " * int(state["oversize"]) + b"}"
            text = str(state.get("response") or "")
            return json.dumps({
                "id": "cmpl-double-1", "object": "chat.completion",
                "created": int(time.time()),
                "model": state.get("reported_model") or requested_model,
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant",
                                         "content": text}}],
                "usage": dict(state.get("usage") or {}),
            }).encode("utf-8")

        def do_GET(self) -> None:  # noqa: N802 - http.server contract
            self._record()
            if state.get("sleep"):
                time.sleep(float(state["sleep"]))
            if state.get("redirect_to") and self.path.startswith("/v1/models"):
                self._send(302, b"", extra={"Location": state["redirect_to"]})
                return
            if self.path.startswith("/v1/models"):
                if state.get("oversize"):
                    self._send(200, b"{" + b" " * int(state["oversize"]) + b"}")
                    return
                self._send(200, json.dumps({
                    "object": "list",
                    "data": [{"id": str(state.get("model") or model),
                              "object": "model", "owned_by": "forge-double",
                              "context_length": 4096}]}).encode("utf-8"))
                return
            if self.path.startswith("/secret-target"):
                #: Reached only if a redirect was (wrongly) followed.
                self._send(200, b'{"leaked": true}')
                return
            self._send(404, b'{"error":"not found"}')

        def do_POST(self) -> None:  # noqa: N802 - http.server contract
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            self._record(raw)
            if state.get("sleep"):
                time.sleep(float(state["sleep"]))
            if not self.path.startswith("/v1/chat/completions"):
                self._send(404, b'{"error":"not found"}')
                return
            if state.get("redirect_to"):
                self._send(302, b"", extra={"Location": state["redirect_to"]})
                return
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except ValueError:
                payload = {}
            requested = str(payload.get("model") or state.get("model") or "")
            status = int(state.get("status") or 200)
            body = self._completion(requested)
            if status >= 400 and state.get("body") is None \
                    and not state.get("oversize"):
                body = json.dumps({"error": {"message": "double failure",
                                             "type": "invalid_request_error",
                                             "code": str(status)}}
                                  ).encode("utf-8")
            self._send(status, body,
                       content_type=str(state.get("content_type") or
                                        "application/json"))

    server = HTTPServer(("127.0.0.1", 0), Handler)
    server.forge_calls = calls  # type: ignore[attr-defined]
    server.forge_state = state  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever,
                              name="s11-loopback-provider", daemon=True)
    thread.start()
    base = "http://127.0.0.1:%d/v1" % server.server_address[1]
    return server, base, state
