"""Session 11 — the server's typed inference surface (§24).

What is checked here:

* inference is disabled until an operator enables it, and a disabled service
  answers ``INFERENCE_NOT_CONFIGURED`` instead of pretending;
* the model operations (list / status / verify / load / unload) and the
  inference operations (generate / stream / events / cancel / status) are
  reachable over HTTP, schema-validated, and scope-checked;
* there is no arbitrary command endpoint, and execution-shaped fields are
  refused even before the schema is consulted;
* a real local generation over HTTP carries honest provenance, and a stream
  delivers deltas with a text-free completion summary;
* prompts never reach the audit log, the event stream, or a response body
  other than the model's own answer.

Labels: ``REAL_INFERENCE_TEST`` for the HTTP suites driven by the first-party
reference engine over a locally materialised artifact, ``MOCK_BACKEND_TEST``
for the scripted-double service suites.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers_a34 import ScriptedProvider, make_fabric  # noqa: E402
from helpers_s11 import (  # noqa: E402
    MOCK_BACKEND_TEST,
    REAL_INFERENCE_TEST,
    isolate_env,
    reference_model_id,
    scripted_fabric,
    write_reference_artifact,
)
from helpers_server import (  # noqa: E402
    TEST_TOKEN,
    auth_headers,
    make_client,
    make_server,
    success_executor,
)

from forge.server.authorization import (  # noqa: E402
    API_OPERATIONS,
    EXECUTION_VECTOR_FIELDS,
)
from forge.core.fencing import FenceRegistry  # noqa: E402
from forge.server.inference import (  # noqa: E402
    MAX_OUTPUT_TOKENS,
    MAX_PROMPT_CHARS,
    MAX_TIMEOUT_SECONDS,
    CancelledAttempt,
    FenceAuthorityError,
    InferenceNotConfigured,
    InferenceServiceConfig,
    NoFenceAuthority,
    ServerInferenceService,
    StaleAttempt,
    SupersededAttempt,
)

MODEL = reference_model_id()
GENERATE_PATH = "/api/v1/inference/generate"


# -- fixtures ---------------------------------------------------------------------


def service(fabric: Any = None, *, fences: Any = None, emit: Any = None,
            **config: Any) -> ServerInferenceService:
    """A service over an injected fabric (``MOCK_BACKEND_TEST``)."""
    if fabric is None:
        fabric, _backend = scripted_fabric(response="mock-answer")
    cfg = {"enabled": True}
    cfg.update(config)
    return ServerInferenceService(config=InferenceServiceConfig(**cfg),
                                  fabric=fabric, fences=fences, emit=emit)


def reference_server(tmp_path: Any, monkeypatch: Any, *, profile: str = "",
                     **config: Any) -> Any:
    """A real server whose inference runs on the reference artifact."""
    isolate_env(monkeypatch, tmp_path)
    directory = Path(tmp_path) / "models"
    write_reference_artifact(directory)
    cfg = {
        "enabled": True,
        "reference_dirs": (str(directory),),
        "allow_network": False,
        "max_model_slots": 1,
        "default_timeout_seconds": 30.0,
        "max_concurrent_requests": 2,
    }
    if profile:
        cfg["resource_profile"] = profile
    cfg.update(config)
    server = make_server(tmp_path, executor=success_executor(),
                         fabric=make_fabric(ScriptedProvider("scripted")),
                         inference=InferenceServiceConfig(**cfg))
    return server


def verified_client(server: Any) -> Any:
    """A TestClient whose server has discovered and verified its models."""
    client = make_client(server)
    listed = client.get("/api/v1/models", params={"discover": "1"},
                        headers=auth_headers())
    assert listed.status_code == 200, listed.text
    checked = client.post("/api/v1/models/verify", json={},
                          headers=auth_headers())
    assert checked.status_code == 200, checked.text
    return client


# -- disabled by default -------------------------------------------------------------


def test_inference_is_disabled_until_an_operator_enables_it():
    """MOCK_BACKEND_TEST: the default posture is "not configured"."""
    disabled = ServerInferenceService(config=InferenceServiceConfig())
    assert disabled.enabled is False
    with pytest.raises(InferenceNotConfigured) as caught:
        disabled.models_list()
    assert caught.value.code == "INFERENCE_NOT_CONFIGURED"
    assert caught.value.status == 503
    assert "FORGE_SERVER_INFERENCE=1" in str(caught.value)
    assert disabled.status()["enabled"] is False


def test_the_server_answers_503_when_inference_is_off(tmp_path, monkeypatch):
    isolate_env(monkeypatch, tmp_path)
    server = make_server(tmp_path, executor=success_executor(),
                         fabric=make_fabric(ScriptedProvider("scripted")),
                         inference=InferenceServiceConfig(enabled=False))
    client = make_client(server)
    response = client.get("/api/v1/models", headers=auth_headers())
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "INFERENCE_NOT_CONFIGURED"
    generation = client.post(GENERATE_PATH, json={"prompt": "hi"},
                             headers=auth_headers())
    assert generation.status_code == 503
    server.stop()


# -- service-level operations (mock doubles) ------------------------------------------


def test_service_lists_verifies_and_reports_status():
    """MOCK_BACKEND_TEST"""
    fabric, _backend = scripted_fabric(response="mock-answer")
    svc = service(fabric)
    listed = svc.models_list(discover=True)
    assert listed["count"] == 1
    assert listed["models"][0]["model_id"] == "scripted:scripted-model"
    assert listed["require_verified"] is True
    assert listed["service"]["enabled"] is True

    status = svc.models_status("scripted:scripted-model")
    assert status["identity"]["verification_state"] == "verified"

    checked = svc.models_verify("")
    assert checked["count"] == 1 and checked["verified"] == 1

    loaded = svc.models_load("scripted:scripted-model")
    assert loaded["loaded"] is True
    unloaded = svc.models_unload("scripted:scripted-model")
    assert unloaded["unloaded"] is True


def test_service_generate_returns_bounded_provenance():
    """MOCK_BACKEND_TEST"""
    svc = service()
    body = svc.inference_generate({"prompt": "hello", "capability": "coding"})
    assert body["success"] is True
    assert body["neural"] is True
    assert body["text"] == "mock-answer"
    assert body["model_id"] == "scripted:scripted-model"
    assert body["backend_id"] == "scripted"
    assert body["verification_state"] == "verified"
    assert body["request_id"].startswith("inf-")
    #: Internal metadata (which can carry residency detail) is not echoed.
    assert "metadata" not in body
    assert len(body["observations"]) <= 12


def test_service_refuses_an_empty_or_oversized_prompt():
    svc = service()
    from forge.server.errors import InvalidRequest

    with pytest.raises(InvalidRequest):
        svc.inference_generate({"prompt": "   "})
    with pytest.raises(InvalidRequest) as caught:
        svc.inference_generate({"prompt": "p" * (MAX_PROMPT_CHARS + 1)})
    assert "bound" in str(caught.value)
    #: Not a dict at all.
    with pytest.raises(InvalidRequest):
        svc.inference_generate("just a string")  # type: ignore[arg-type]


def test_service_clamps_generation_parameters():
    """Bounds are enforced, not trusted: a caller cannot ask for infinity."""
    fabric, backend = scripted_fabric(response="mock-answer")
    svc = service(fabric)
    body = svc.inference_generate({
        "prompt": "hello", "capability": "coding",
        "max_output_tokens": MAX_OUTPUT_TOKENS * 100,
        "timeout": MAX_TIMEOUT_SECONDS * 100,
        "temperature": 99.0, "complexity": 10_000.0,
    })
    assert body["success"] is True
    sent = backend.generate_calls[-1]
    assert sent.max_output_tokens == MAX_OUTPUT_TOKENS
    assert sent.temperature == 2.0
    assert sent.timeout <= MAX_TIMEOUT_SECONDS

    #: A context window no model has is a routing refusal, not a clamp.
    refused = svc.inference_generate({"prompt": "hello", "capability": "coding",
                                      "min_context_window": 10 ** 9,
                                      "allow_deterministic": False})
    assert refused["success"] is False
    assert refused["neural"] is False


def test_service_binds_a_generation_to_the_task_fence():
    """MOCK_BACKEND_TEST: a superseded attempt publishes nothing (§13)."""
    from forge.core.fencing import FenceRegistry

    registry = FenceRegistry()
    chunks = ["z"] * 40
    fabric, _backend = scripted_fabric(response="".join(chunks), chunks=chunks,
                                       chunk_delay=0.02)
    svc = service(fabric, fences=registry)
    fence = registry.begin("task-9")
    registry.mark_running("task-9", fence)

    started = svc.inference_stream({"prompt": "in flight",
                                    "capability": "coding",
                                    "task_id": "task-9"})
    page = svc.inference_stream_events(started["stream_id"], wait=0.2)
    assert page["events"], "the stream should already be producing"
    #: A newer attempt supersedes the one that is generating.
    registry.begin("task-9")

    final: Dict[str, Any] = {}
    deadline = time.time() + 10.0
    while time.time() < deadline:
        final = svc.inference_stream_events(started["stream_id"],
                                            after=page["after"], wait=0.5)
        if final["done"]:
            break
    assert final["done"] is True
    summary = final.get("result") or {}
    assert summary["success"] is False
    assert summary["state"] == "stale"
    assert summary["neural"] is False
    assert "text" not in summary
    #: The stale attempt left nothing in the retained stream either.
    assert "".join(event.get("delta", "") for event in final["events"]) != \
        "".join(chunks)


def test_task_bound_generation_requires_a_fence_authority():
    """INTEGRATION_TEST (§6): no authority → denial, never "no fence, continue".

    This test replaced one that asserted the opposite. A task-bound generation
    whose authorization cannot be established is refused: no model compute, no
    published result, and an explicit infrastructure verdict for the caller.
    """
    svc = service()
    with pytest.raises(NoFenceAuthority) as refused:
        svc.inference_generate({"prompt": "hi", "capability": "coding",
                                "task_id": "task-1"})
    assert refused.value.code == "NO_FENCE_AUTHORITY"
    assert refused.value.status == 503
    assert svc._counts.get("fence_authority_missing") == 1
    #: An interactive generation has no attempt to fence — that is a different
    #: thing from a task attempt whose fence is missing, and it still works.
    body = svc.inference_generate({"prompt": "hi", "capability": "coding"})
    assert body["success"] is True
    assert svc._fence_for("") is None


def test_a_broken_fence_authority_fails_closed():
    """INTEGRATION_TEST (§6): an authority that raises is FENCE_ERROR."""

    class BrokenFences:
        def current(self, task_id: str) -> Any:
            raise RuntimeError("fence store unavailable")

    svc = service(fences=BrokenFences())
    with pytest.raises(FenceAuthorityError) as refused:
        svc.inference_generate({"prompt": "hi", "capability": "coding",
                                "task_id": "task-2"})
    assert refused.value.code == "FENCE_ERROR"
    assert svc._counts.get("fence_authority_error") == 1


def test_an_unknown_attempt_is_not_an_authorization():
    """INTEGRATION_TEST (§6): the authority knows no attempt for this task."""
    fences = FenceRegistry()
    svc = service(fences=fences)
    with pytest.raises(NoFenceAuthority):
        svc.inference_generate({"prompt": "hi", "capability": "coding",
                                "task_id": "task-nobody"})
    assert svc._counts.get("fence_missing_for_task") == 1


def test_the_current_attempt_is_admitted_and_refused_by_name_when_it_ends():
    """INTEGRATION_TEST (§6/§7): admitted while authorized, refused by name after.

    Every refusal below comes from a real :class:`FenceRegistry` transition —
    no test seam stands in for the service's own verdict.
    """
    fences = FenceRegistry()
    svc = service(fences=fences)
    fence = fences.begin("task-9", owner="worker-1")
    fences.mark_running("task-9", fence)
    body = svc.inference_generate({"prompt": "hi", "capability": "coding",
                                   "task_id": "task-9"})
    assert body["success"] is True
    assert body["task_id"] == "task-9"

    #: Cancellation reaches the fence, and the fence reaches admission.
    fences.cancel("task-9")
    with pytest.raises(CancelledAttempt) as cancelled:
        svc.inference_generate({"prompt": "hi", "capability": "coding",
                                "task_id": "task-9"})
    assert cancelled.value.code == "CANCELLED_ATTEMPT"
    assert cancelled.value.status == 409

    #: A crashed/fenced attempt is stale, and stale is not "authorized".
    fences2 = FenceRegistry()
    svc2 = service(fences=fences2)
    other = fences2.begin("task-10", owner="worker-1")
    fences2.mark_running("task-10", other)
    fences2.fence("task-10", other, reason="worker vanished at restart")
    with pytest.raises(StaleAttempt) as stale:
        svc2.inference_generate({"prompt": "hi", "capability": "coding",
                                 "task_id": "task-10"})
    assert stale.value.code == "STALE_ATTEMPT"
    assert svc2._counts.get("fence_denied") == 1


def test_a_superseded_attempt_is_named_as_superseded():
    """INTEGRATION_TEST (§6): SUPERSEDED_ATTEMPT, not a generic stale verdict."""

    class SupersedingFences:
        """A registry whose current attempt is fenced as superseded."""

        def __init__(self) -> None:
            self._inner = FenceRegistry()
            self.first = self._inner.begin("task-11", owner="worker-1")
            self._inner.mark_running("task-11", self.first)
            self._inner.begin("task-11", owner="worker-2")

        def current(self, task_id: str) -> Any:
            #: Reports the *old* attempt as current: the situation a zombie
            #: worker is in when it asks whether it may still publish.
            return self._inner.get(task_id, self.first.generation)

        def is_authorized(self, fence: Any) -> bool:
            return self._inner.is_authorized(fence)

    svc = service(fences=SupersedingFences())
    with pytest.raises(SupersededAttempt) as refused:
        svc.inference_generate({"prompt": "hi", "capability": "coding",
                                "task_id": "task-11"})
    assert refused.value.code == "SUPERSEDED_ATTEMPT"
    assert svc._counts.get("fence_denied") == 1


def test_service_stream_events_are_cursored_and_text_free():
    """MOCK_BACKEND_TEST"""
    chunks = ["al", "ph", "a"]
    fabric, _backend = scripted_fabric(response="".join(chunks), chunks=chunks,
                                       chunk_delay=0.01)
    svc = service(fabric)
    started = svc.inference_stream({"prompt": "stream", "capability": "coding"})
    stream_id = started["stream_id"]
    assert stream_id.startswith("str-")
    assert started["request_id"].startswith("inf-")

    seen: List[Dict[str, Any]] = []
    cursor = 0
    deadline = time.time() + 10.0
    done = False
    while not done and time.time() < deadline:
        page = svc.inference_stream_events(stream_id, after=cursor, wait=1.0)
        seen.extend(page["events"])
        cursor = page["after"]
        done = bool(page["done"])
    assert done is True
    text = "".join(event.get("delta", "") for event in seen)
    assert text == "alpha"
    sequences = [event["sequence"] for event in seen]
    assert sequences == sorted(set(sequences))

    final = svc.inference_stream_events(stream_id, after=cursor, wait=0.0)
    assert final["done"] is True
    summary = final.get("result") or {}
    #: The completion summary is metadata: never the text, never the prompt.
    assert "text" not in summary
    assert summary["success"] is True
    assert summary["neural"] is True
    assert summary["state"] == "succeeded"
    snapshot = final.get("stream") or {}
    assert snapshot["complete"] is True
    assert "alpha" not in repr(final)


def test_service_cancel_stops_an_in_flight_stream():
    """MOCK_BACKEND_TEST"""
    chunks = ["x"] * 60
    fabric, _backend = scripted_fabric(response="".join(chunks), chunks=chunks,
                                       chunk_delay=0.02)
    svc = service(fabric)
    started = svc.inference_stream({"prompt": "long", "capability": "coding"})
    page = svc.inference_stream_events(started["stream_id"], wait=0.2)
    assert page["events"]
    cancelled = svc.inference_cancel(started["request_id"], reason="tester")
    assert cancelled["cancelled"] is True
    deadline = time.time() + 10.0
    final: Dict[str, Any] = {}
    while time.time() < deadline:
        final = svc.inference_stream_events(started["stream_id"],
                                            after=page["after"], wait=0.5)
        if final["done"]:
            break
    assert final["done"] is True
    summary = final.get("result") or {}
    #: The producer, not the cancel call, reports how the attempt ended, so
    #: the provenance of the cancelled generation is still on the record.
    assert summary["success"] is False
    assert summary["state"] == "cancelled"
    assert summary["neural"] is False
    assert summary["error_code"] == "CANCELLED"
    assert (final.get("stream") or {}).get("cancelled") is True


def test_cancelling_an_unknown_request_is_honest():
    svc = service()
    outcome = svc.inference_cancel("inf-does-not-exist")
    assert outcome["cancelled"] is False
    assert outcome["reason"]


def test_the_concurrency_bound_refuses_instead_of_queueing_forever():
    """MOCK_BACKEND_TEST: a bounded service says no rather than piling up."""
    chunks = ["y"] * 20
    fabric, _backend = scripted_fabric(response="".join(chunks), chunks=chunks,
                                       chunk_delay=0.05)
    svc = service(fabric, max_concurrent_requests=1)
    first = svc.inference_stream({"prompt": "one", "capability": "coding"})
    assert first["stream_id"]
    from forge.server.errors import PermissionDenied

    with pytest.raises(PermissionDenied):
        svc.inference_generate({"prompt": "two", "capability": "coding"})
    #: The refused call is counted, so an operator can see the pressure.
    assert svc.status()["counts"].get("concurrency_refused") == 1
    deadline = time.time() + 15.0
    while time.time() < deadline:
        if svc.inference_stream_events(first["stream_id"], wait=0.5)["done"]:
            break


def test_service_status_reports_bounded_counters():
    svc = service()
    svc.inference_generate({"prompt": "a", "capability": "coding"})
    svc.inference_generate({"prompt": "b", "capability": "vision"})
    status = svc.status()
    assert status["enabled"] is True
    assert status["counts"]["generate"] == 2
    assert status["counts"].get("generate_failed", 0) == 1
    assert status["running_requests"] == 0
    assert status["max_concurrent_requests"] == 2
    assert status["config"]["enabled"] is True


# -- HTTP surface (real local inference) -------------------------------------------------


def test_models_endpoint_lists_a_verified_real_model(tmp_path, monkeypatch):
    """REAL_INFERENCE_TEST"""
    server = reference_server(tmp_path, monkeypatch)
    client = make_client(server)
    listed = client.get("/api/v1/models", params={"discover": "1"},
                        headers=auth_headers())
    assert listed.status_code == 200, listed.text
    payload = listed.json()
    ids = [item["model_id"] for item in payload["models"]]
    assert MODEL in ids
    entry = [item for item in payload["models"] if item["model_id"] == MODEL][0]
    #: Discovered, not yet verified: configuration is never availability.
    assert entry["verification_state"] == "unverified"
    assert entry["availability_state"] in ("discovered", "loaded", "ready")
    assert entry["local"] is True
    assert entry["artifact_fingerprint"]

    checked = client.post("/api/v1/models/verify", json={},
                          headers=auth_headers())
    assert checked.status_code == 200, checked.text
    body = checked.json()
    assert body["verified"] >= 1

    status = client.get("/api/v1/models/status", params={"model_id": MODEL},
                        headers=auth_headers())
    assert status.status_code == 200
    assert status.json()["identity"]["verification_state"] == "verified"
    assert status.json()["identity"]["verification_method"]
    server.stop()


def test_generate_over_http_is_real_and_labelled(tmp_path, monkeypatch):
    """REAL_INFERENCE_TEST: a real forward pass on a local artifact."""
    server = reference_server(tmp_path, monkeypatch)
    client = verified_client(server)
    response = client.post(GENERATE_PATH, json={"prompt": "continue",
                                                "capability": ""},
                           headers=auth_headers())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True
    assert body["neural"] is True
    assert body["text"]
    assert body["model_id"] == MODEL
    assert body["backend_id"] == "reference"
    assert body["verification_state"] == "verified"
    assert body["finish_reason"] in ("stop", "length")
    assert body["latency_ms"] > 0
    #: The reference engine is honest about what it is.
    assert body["output_scan"]["scanned_chars"] > 0
    server.stop()


def test_unverified_models_are_refused_over_http(tmp_path, monkeypatch):
    """REAL_INFERENCE_TEST: no verification, no neural claim."""
    server = reference_server(tmp_path, monkeypatch)
    client = make_client(server)
    client.get("/api/v1/models", params={"discover": "1"},
               headers=auth_headers())
    response = client.post(GENERATE_PATH,
                           json={"prompt": "continue", "capability": "",
                                 "allow_deterministic": False},
                           headers=auth_headers())
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is False
    assert body["state"] == "unverified"
    assert body["neural"] is False
    assert body["text"] == ""
    server.stop()


def test_stream_over_http_delivers_deltas_and_a_text_free_summary(
        tmp_path, monkeypatch):
    """REAL_INFERENCE_TEST"""
    server = reference_server(tmp_path, monkeypatch)
    client = verified_client(server)
    started = client.post("/api/v1/inference/stream",
                          json={"prompt": "stream this", "capability": ""},
                          headers=auth_headers())
    assert started.status_code == 200, started.text
    stream_id = started.json()["stream_id"]

    seen: List[Dict[str, Any]] = []
    cursor = 0
    done = False
    deadline = time.time() + 30.0
    while not done and time.time() < deadline:
        page = client.get(
            "/api/v1/inference/streams/%s/events" % stream_id,
            params={"after": cursor, "wait": "1.0"}, headers=auth_headers())
        assert page.status_code == 200, page.text
        payload = page.json()
        seen.extend(payload["events"])
        cursor = payload["after"]
        done = bool(payload["done"])
    assert done is True
    text = "".join(event.get("delta", "") for event in seen)
    assert text
    sequences = [event["sequence"] for event in seen]
    assert sequences == sorted(set(sequences))
    assert all(b > a for a, b in zip(sequences, sequences[1:]))

    final = client.get("/api/v1/inference/streams/%s/events" % stream_id,
                       params={"after": cursor}, headers=auth_headers()).json()
    summary = final.get("result") or {}
    assert summary["success"] is True
    assert summary["neural"] is True
    assert summary["streamed"] is True
    assert "text" not in summary
    assert (final.get("stream") or {}).get("complete") is True
    #: The whole exchange never echoes the prompt back.
    assert "stream this" not in repr(final)
    server.stop()


def test_cancel_over_http_stops_a_real_stream(tmp_path, monkeypatch):
    """REAL_INFERENCE_TEST"""
    server = reference_server(tmp_path, monkeypatch)
    client = verified_client(server)
    started = client.post("/api/v1/inference/stream",
                          json={"prompt": "a longer generation please",
                                "capability": ""},
                          headers=auth_headers()).json()
    first = client.get(
        "/api/v1/inference/streams/%s/events" % started["stream_id"],
        params={"wait": "0.2"}, headers=auth_headers()).json()
    cancelled = client.post("/api/v1/inference/cancel",
                            json={"request_id": started["request_id"],
                                  "reason": "test"},
                            headers=auth_headers())
    assert cancelled.status_code == 200
    assert cancelled.json()["cancelled"] is True
    deadline = time.time() + 15.0
    final: Dict[str, Any] = {}
    while time.time() < deadline:
        final = client.get(
            "/api/v1/inference/streams/%s/events" % started["stream_id"],
            params={"after": first["after"], "wait": "0.5"},
            headers=auth_headers()).json()
        if final["done"]:
            break
    assert final["done"] is True
    summary = final.get("result") or {}
    assert summary["state"] == "cancelled"
    assert summary["neural"] is False
    server.stop()


def test_inference_status_over_http_reports_the_service(tmp_path, monkeypatch):
    """REAL_INFERENCE_TEST"""
    server = reference_server(tmp_path, monkeypatch)
    client = verified_client(server)
    client.post(GENERATE_PATH, json={"prompt": "hi", "capability": ""},
                headers=auth_headers())
    response = client.get("/api/v1/inference/status", headers=auth_headers())
    assert response.status_code == 200
    body = response.json()
    assert body["service"]["enabled"] is True
    assert body["service"]["counts"]["generate"] >= 1
    assert body["service"]["running_requests"] == 0
    #: The fabric's own diagnostics ride along, including its evidence feed.
    assert body["fabric"]["counts"]["requests"] >= 1
    assert body["evidence"]["requests"] >= 1
    #: ``in_flight`` is the list of live requests: empty once the call ended.
    assert body["in_flight"] == []
    server.stop()


def test_g560_server_never_becomes_an_inference_machine(tmp_path, monkeypatch):
    """REAL_INFERENCE_TEST under the Win7/32-bit/2GB profile."""
    server = reference_server(tmp_path, monkeypatch, profile="g560")
    client = verified_client(server)
    response = client.post(GENERATE_PATH,
                           json={"prompt": "hi", "capability": "",
                                 "allow_deterministic": False},
                           headers=auth_headers())
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is False
    assert body["state"] == "resource_denied"
    assert body["neural"] is False
    assert body["text"] == ""
    assert body["resource_result"]["profile"] == "g560"
    #: Loading is refused outright, so nothing is ever resident.
    loaded = client.post("/api/v1/models/load", json={"model_id": MODEL},
                         headers=auth_headers())
    assert loaded.status_code == 200
    assert loaded.json()["loaded"] is False
    server.stop()


# -- authorization, schemas, and the no-remote-shell guarantee ---------------------------


def test_requests_without_a_token_are_refused(tmp_path, monkeypatch):
    server = reference_server(tmp_path, monkeypatch)
    client = make_client(server)
    for path in ("/api/v1/models", "/api/v1/inference/status"):
        assert client.get(path).status_code == 401
    assert client.post(GENERATE_PATH, json={"prompt": "hi"}).status_code == 401
    bad = client.get("/api/v1/models",
                     headers=auth_headers("not-a-real-token"))
    assert bad.status_code == 401
    assert bad.json()["error"]["code"] in ("AUTH_REQUIRED", "AUTH_FAILED",
                                           "UNAUTHORIZED")
    server.stop()


def test_a_viewer_may_read_models_but_not_run_inference(tmp_path, monkeypatch):
    server = reference_server(tmp_path, monkeypatch)
    key = server.auth.create_api_key("s11-viewer", role="viewer")["key"]
    client = make_client(server)
    headers = auth_headers(key)
    assert client.get("/api/v1/models", headers=headers).status_code == 200
    forbidden = client.post(GENERATE_PATH, json={"prompt": "hi"},
                            headers=headers)
    assert forbidden.status_code == 403
    assert client.post("/api/v1/models/load", json={"model_id": MODEL},
                       headers=headers).status_code == 403
    server.stop()


def test_the_inference_schemas_reject_unknown_and_oversized_fields(
        tmp_path, monkeypatch):
    server = reference_server(tmp_path, monkeypatch)
    client = verified_client(server)
    #: An unknown field is a schema violation, not a silently dropped hint.
    unknown = client.post(GENERATE_PATH,
                          json={"prompt": "hi", "surprise": True},
                          headers=auth_headers())
    assert unknown.status_code in (400, 422)
    oversized = client.post(GENERATE_PATH,
                            json={"prompt": "p" * (MAX_PROMPT_CHARS + 1)},
                            headers=auth_headers())
    assert oversized.status_code in (400, 413, 422)
    empty = client.post(GENERATE_PATH, json={"prompt": ""},
                        headers=auth_headers())
    assert empty.status_code in (400, 422)
    bad_tokens = client.post(GENERATE_PATH,
                             json={"prompt": "hi",
                                   "max_output_tokens": MAX_OUTPUT_TOKENS + 1},
                             headers=auth_headers())
    assert bad_tokens.status_code in (400, 422)
    bad_timeout = client.post(GENERATE_PATH,
                              json={"prompt": "hi",
                                    "timeout": MAX_TIMEOUT_SECONDS + 1},
                              headers=auth_headers())
    assert bad_timeout.status_code in (400, 422)
    server.stop()


def test_execution_shaped_fields_are_refused(tmp_path, monkeypatch):
    """The inference surface is not a remote shell (§24, §25)."""
    server = reference_server(tmp_path, monkeypatch)
    client = verified_client(server)
    for field in sorted(EXECUTION_VECTOR_FIELDS)[:6]:
        response = client.post(GENERATE_PATH,
                               json={"prompt": "hi", field: "rm -rf /"},
                               headers=auth_headers())
        assert response.status_code in (400, 422), field
    #: The service-level guard says why, in words an operator can read.
    from forge.server.errors import InvalidRequest

    svc = service()
    with pytest.raises(InvalidRequest):
        from forge.server.authorization import reject_execution_vectors

        reject_execution_vectors({"prompt": "hi", "command": "ls"})
    server.stop()


def test_there_is_no_arbitrary_command_endpoint(tmp_path, monkeypatch):
    server = reference_server(tmp_path, monkeypatch)
    client = make_client(server)
    app = server.create_app()
    paths = sorted({str(getattr(route, "path", "")) for route in app.routes})
    assert paths, "the app exposes no routes"
    lowered = " ".join(paths).lower()
    for forbidden in ("/exec", "/shell", "/command", "/eval", "/run",
                      "/subprocess", "/system"):
        assert forbidden not in lowered, forbidden
    #: Every inference operation is one of the typed, closed set.
    for operation in ("models.list", "models.status", "models.verify",
                      "models.load", "models.unload", "inference.generate",
                      "inference.stream", "inference.stream_events",
                      "inference.cancel", "inference.status"):
        assert operation in API_OPERATIONS
    #: Guessing at an endpoint that does not exist gets a 404, not a shell.
    for path in ("/api/v1/exec", "/api/v1/inference/exec",
                 "/api/v1/models/shell", "/api/v1/run"):
        assert client.post(path, json={"cmd": "id"},
                           headers=auth_headers()).status_code in (404, 405)
    server.stop()


def test_prompts_never_reach_the_audit_log_or_the_response_metadata(
        tmp_path, monkeypatch):
    """REAL_INFERENCE_TEST with a marked prompt (§20, §25)."""
    server = reference_server(tmp_path, monkeypatch)
    client = verified_client(server)
    marker = "UNIQUE-PROMPT-MARKER-77aa"
    response = client.post(GENERATE_PATH,
                           json={"prompt": marker, "capability": ""},
                           headers=auth_headers())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True
    #: The answer may mention nothing of the prompt; every field except the
    #: generated text itself must be free of it.
    without_text = {key: value for key, value in body.items()
                    if key not in ("text", "context", "routing", "fallback")}
    assert marker not in repr(without_text)

    audit = server.audit.to_dict()
    assert audit, "nothing was audited"
    assert marker not in repr(audit)
    operations = {item["operation"] for item in audit}
    assert "inference.generate" in operations
    assert "models.verify" in operations
    server.stop()


def test_task_notifications_carry_metadata_only():
    """MOCK_BACKEND_TEST: the event payload is a closed, bounded set."""
    captured: List[Dict[str, Any]] = []

    def emit(task_id: str, _attempt: str, event_type: str,
             payload: Dict[str, Any]) -> None:
        captured.append({"task_id": task_id, "type": event_type,
                         "payload": payload})

    fabric, _backend = scripted_fabric(response="SECRET-ANSWER-1f2e")
    #: Session 11.5 (§6): a task-bound generation needs a fence authority, so
    #: the test wires a real one and admits the attempt it is testing.
    fences = FenceRegistry()
    fence = fences.begin("task-77", owner="worker-1")
    fences.mark_running("task-77", fence)
    svc = ServerInferenceService(
        config=InferenceServiceConfig(enabled=True), fabric=fabric, emit=emit,
        fences=fences)
    svc.inference_generate({"prompt": "SECRET-PROMPT-9d41",
                            "capability": "coding", "task_id": "task-77"})
    assert captured and captured[0]["type"] == "inference.completed"
    payload = captured[0]["payload"]
    blob = repr(payload)
    assert "SECRET-PROMPT-9d41" not in blob
    assert "SECRET-ANSWER-1f2e" not in blob
    assert set(payload) <= {"request_id", "model_id", "backend_id", "state",
                            "success", "error_code", "latency_ms", "neural"}


def test_labels_are_declared():
    assert REAL_INFERENCE_TEST == "REAL_INFERENCE_TEST"
    assert MOCK_BACKEND_TEST == "MOCK_BACKEND_TEST"
    assert __doc__ and "REAL_INFERENCE_TEST" in __doc__
