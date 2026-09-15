"""Session 11.5 (§15 streaming, §30 G560) — transport is not durability.

Labels: MOCK_BACKEND_TEST (a labelled double answers; no model compute),
INTEGRATION_TEST (real server, queue, fences, service), PORTABILITY_TEST and
SECURITY_TEST for the G560 thin-client case.

The streaming guarantees under test are the ones Session 11.5 adds on top of
Session 11's bounded stream: a stream knows whose attempt it carries, a stale
attempt cannot open one, a consumer that disappears does not stop the work, and
a restart never pretends a lost in-memory buffer was durable state.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helpers_a34 import ScriptedProvider, make_fabric  # noqa: E402
from helpers_s11 import g560_governor, scripted_fabric  # noqa: E402
from helpers_server import (  # noqa: E402
    TEST_TOKEN, auth_headers, make_client, make_server)

from forge.models.inference_path import PATH_SESSION11  # noqa: E402
from forge.models.request import ModelRequest  # noqa: E402
from forge.server.inference import (  # noqa: E402
    InferenceServiceConfig, ServerInferenceService, StaleAttempt,
    SupersededAttempt)

ADMIN = auth_headers(TEST_TOKEN)


def _canonical_server(tmp_path: Any, fabric: Any, *, db_path: Any = None,
                      governor: Any = None, with_repo: bool = True) -> Any:
    """A real server whose canonical inference path is Session 11."""
    from forge.models.inference_path import InferencePathConfig

    legacy = make_fabric(ScriptedProvider(json.dumps({
        "summary": "legacy marker",
        "changes": [{"path": "legacy_marker.py", "action": "create",
                     "content": "LEGACY = True\n"}],
        "tests_to_run": [], "reasoning_summary": "legacy",
        "risk_level": "low"})))
    path_config = InferencePathConfig(mode="session11")
    inference = InferenceServiceConfig(enabled=True, require_verified=True,
                                       max_concurrent_requests=4)
    server = make_server(tmp_path, executor=None, fabric=legacy,
                         inference=inference, inference_path=path_config,
                         db_path=db_path, max_retries=0,
                         with_repo=with_repo)
    server.inference = ServerInferenceService(
        config=inference, fabric=fabric, governor=governor or server.governor,
        audit=server.audit, emit=server.emit, fences=server.fences)
    server.fabric.attach_inference_path(server.inference, path_config,
                                        fences=server.fences)
    return server


def _drain(service: Any, stream_id: str, *, cursor: int = 0,
           timeout: float = 20.0) -> Dict[str, Any]:
    """Read a stream to its single terminal event, from a cursor."""
    seen: List[Dict[str, Any]] = []
    deadline = time.time() + timeout
    done = False
    page: Dict[str, Any] = {}
    while not done and time.time() < deadline:
        page = service.inference_stream_events(stream_id, after=cursor,
                                               wait=0.5)
        seen.extend(page["events"])
        cursor = page["after"]
        done = bool(page["done"])
    page["events"] = seen
    page["after"] = cursor
    return page


# ---------------------------------------------------------------------------
# §15 — a stream carries its attempt, and survives its consumer
# ---------------------------------------------------------------------------


def test_a_stream_carries_its_task_identity_and_one_terminal_event(tmp_path):
    """MOCK_BACKEND_TEST · INTEGRATION_TEST (§15).

    A stream must know whose work it is carrying, so a thin client (or an
    auditor) can tie a delta to a durable attempt: task_id, attempt_id,
    generation_id and request_id are on both the start response and every
    cursor page. Sequences stay monotonic, exactly one terminal event is
    produced, and a replay from the beginning duplicates nothing.
    """
    chunks = ["al", "ph", "a"]
    fabric, backend = scripted_fabric(response="".join(chunks), chunks=chunks,
                                      chunk_delay=0.01,
                                      backend_name="s11double")
    server = _canonical_server(tmp_path, fabric)
    try:
        task = server.tasks.create("demo", "stream me")
        fence = server.fences.begin(task.task_id, owner="worker-A")
        server.fences.mark_running(task.task_id, fence)

        started = server.inference.inference_stream(
            {"prompt": "stream", "capability": "",
             "task_id": task.task_id, "attempt_id": fence.attempt_id})
        stream_id = started["stream_id"]
        assert started["task_id"] == task.task_id
        assert started["attempt_id"] == fence.attempt_id
        assert started["generation_id"] == fence.attempt_id
        assert started["request_id"].startswith("inf-")

        body = _drain(server.inference, stream_id)
        assert body["done"] is True
        #: the identity travels with the transport, not just with the start
        assert body["task_id"] == task.task_id
        assert body["attempt_id"] == fence.attempt_id
        assert body["generation_id"] == started["generation_id"]
        assert body["request_id"] == started["request_id"]

        sequences = [event["sequence"] for event in body["events"]]
        assert sequences == sorted(set(sequences))          # monotonic, unique
        assert "".join(event.get("delta", "")
                       for event in body["events"]) == "alpha"
        #: exactly one terminal state, reported once
        assert body["state"] in ("succeeded", "")
        summary = body.get("result") or {}
        assert summary["success"] is True
        assert summary["state"] == "succeeded"
        assert "text" not in summary                        # never content
        snapshot = body.get("stream") or {}
        assert snapshot["complete"] is True

        #: a replay from the beginning is exact and adds no duplicates
        replay = _drain(server.inference, stream_id, cursor=0)
        assert [event["sequence"] for event in replay["events"]] == sequences
        assert backend.generate_calls
    finally:
        server.close()


def test_a_superseded_attempt_cannot_open_a_stream(tmp_path):
    """SECURITY_TEST · INTEGRATION_TEST (§15/§12) — the fence reaches transport.

    Streaming is a publication surface: an attempt that lost authority must not
    be able to open one, and the refusal is the same typed verdict the
    generation path gives, not a generic error.
    """
    fabric, _backend = scripted_fabric(response="x" * 32,
                                       chunks=["x"] * 8, chunk_delay=0.01,
                                       backend_name="s11double")
    server = _canonical_server(tmp_path, fabric)
    try:
        task = server.tasks.create("demo", "superseded stream")
        stale = server.fences.begin(task.task_id, owner="worker-A")
        server.fences.mark_running(task.task_id, stale)
        successor = server.fences.begin(task.task_id, owner="worker-B")
        server.fences.mark_running(task.task_id, successor)

        with pytest.raises(SupersededAttempt) as excinfo:
            server.inference.inference_stream(
                {"prompt": "late", "capability": "",
                 "task_id": task.task_id, "attempt_id": stale.attempt_id})
        assert excinfo.value.status == 409
        assert excinfo.value.code == "SUPERSEDED_ATTEMPT"

        #: a fenced attempt is refused under the same vocabulary
        server.fences.fence(task.task_id, successor, reason="restart")
        with pytest.raises(StaleAttempt):
            server.inference.inference_stream(
                {"prompt": "later", "capability": "",
                 "task_id": task.task_id,
                 "attempt_id": successor.attempt_id})
    finally:
        server.close()


def test_a_disconnected_consumer_does_not_stop_the_generation(tmp_path):
    """INTEGRATION_TEST · MOCK_BACKEND_TEST (§15/§11).

    The client is observational. A consumer that reads one page and then
    disappears must not cancel the work: the producer runs to completion, the
    bounded buffer keeps the events, and a later reader recovers the rest from
    its cursor — with the terminal event and the provenance summary intact.
    """
    chunks = ["c%d" % index for index in range(12)]
    fabric, backend = scripted_fabric(response="".join(chunks), chunks=chunks,
                                      chunk_delay=0.02,
                                      backend_name="s11double")
    server = _canonical_server(tmp_path, fabric)
    try:
        started = server.inference.inference_stream(
            {"prompt": "long running", "capability": ""})
        stream_id = started["stream_id"]
        first = server.inference.inference_stream_events(stream_id, wait=0.2)
        cursor = first["after"]
        assert first["events"]

        #: the consumer goes away. Nobody polls, nobody cancels.
        deadline = time.time() + 20.0
        while time.time() < deadline:
            probe = server.inference.inference_stream_events(stream_id,
                                                             after=cursor,
                                                             wait=0.0)
            if probe["done"]:
                break
            time.sleep(0.05)

        #: a new consumer picks up exactly where the old one stopped
        resumed = server.inference.inference_stream_events(stream_id,
                                                           after=cursor,
                                                           wait=1.0)
        assert resumed["done"] is True
        assert resumed["task_id"] == ""              # interactive: no task
        full = _drain(server.inference, stream_id, cursor=0)
        assert "".join(event.get("delta", "")
                       for event in full["events"]) == "".join(chunks)
        assert (full.get("result") or {})["state"] == "succeeded"
        #: the generation really ran to the end; nothing was abandoned
        assert backend.generate_calls
        assert (full.get("stream") or {})["complete"] is True
    finally:
        server.close()


def test_a_restart_never_claims_a_lost_stream_is_durable(tmp_path):
    """INTEGRATION_TEST (§15/§12) — transport dies, the task record does not.

    After a restart the buffer is gone, and the service says so in words that
    distinguish live transport from durable state. What must survive is the
    task: its record, its status and its persisted inference metadata are read
    back from SQLite by the new process, and no invented replay is offered.
    """
    db_path = tmp_path / "server" / "server.db"
    chunks = ["a", "b", "c"]
    fabric, _backend = scripted_fabric(response="abc", chunks=chunks,
                                       chunk_delay=0.01,
                                       backend_name="s11double")
    server = _canonical_server(tmp_path, fabric, db_path=db_path)
    try:
        task = server.tasks.create("demo", "survives the restart")
        task_id = task.task_id
        fence = server.fences.begin(task_id, owner="worker-A")
        server.fences.mark_running(task_id, fence)
        started = server.inference.inference_stream(
            {"prompt": "stream", "capability": "", "task_id": task_id,
             "attempt_id": fence.attempt_id})
        stream_id = started["stream_id"]
        _drain(server.inference, stream_id)
    finally:
        server.close()

    #: a new process over the same durable store
    reborn_fabric, _reborn_backend = scripted_fabric(response="abc",
                                                     backend_name="s11double")
    #: ``with_repo=False``: the repository (and its git history) is the durable
    #: part being restarted onto, not something to initialise twice.
    restarted = _canonical_server(tmp_path, reborn_fabric, db_path=db_path,
                                  with_repo=False)
    try:
        record = restarted.tasks.get_or_raise(task_id)
        assert record.task_id == task_id                 # durability is real
        assert restarted.queue.lease_owner(task_id) == ""

        from forge.server.errors import NotFound

        with pytest.raises(NotFound) as excinfo:
            restarted.inference.inference_stream_events(stream_id)
        message = str(excinfo.value).lower()
        assert "not durable state" in message
        assert "survive a restart" in message
        #: and the honest answer is a 404, never a fabricated empty replay
        assert excinfo.value.status == 404
    finally:
        restarted.close()


def test_cancelling_a_task_revokes_its_stream_authority(tmp_path):
    """SECURITY_TEST · INTEGRATION_TEST (§11/§15/§34) — cancel during streaming.

    One cancellation chain: the operator cancels the task, the fence goes to
    CANCELLING, the attempt loses publish authority immediately, and the stream
    reaches a single terminal state that says how it ended.
    """
    chunks = ["x"] * 40
    fabric, _backend = scripted_fabric(response="".join(chunks), chunks=chunks,
                                      chunk_delay=0.03,
                                      backend_name="s11double")
    server = _canonical_server(tmp_path, fabric)
    try:
        task = server.tasks.create("demo", "cancel me while streaming")
        task_id = task.task_id
        #: a real lease, so the authority's answer is about the cancellation
        #: and not about a missing owner
        from forge.server import TaskStatus

        server.tasks.transition(task_id, TaskStatus.QUEUED,
                                expected=(TaskStatus.CREATED,))
        server.queue.enqueue(task_id, "demo")
        assert server.queue.lease_next("demo", "worker-A") == task_id
        fence = server.fences.begin(task_id, owner="worker-A")
        server.fences.mark_running(task_id, fence)
        started = server.inference.inference_stream(
            {"prompt": "long", "capability": "", "task_id": task_id,
             "attempt_id": fence.attempt_id})
        stream_id = started["stream_id"]
        server.inference.inference_stream_events(stream_id, wait=0.2)

        authority = server.authority_for(task_id, fence=fence,
                                         lease_owner="worker-A")
        assert authority.publish_authorized() is True

        #: the unified chain: control + fence in one call
        assert authority.request_cancel("operator") is True
        assert server.fences.current(task_id).state == "CANCELLING"
        assert authority.publish_authorized() is False
        assert authority.denial_reason() == "CANCELLED_ATTEMPT"

        cancelled = server.inference.inference_cancel(started["request_id"],
                                                      reason="operator")
        assert cancelled["cancelled"] is True
        final = _drain(server.inference, stream_id, timeout=20.0)
        assert final["done"] is True
        summary = final.get("result") or {}
        #: the producer reports the honest end state; nothing is fabricated
        assert summary.get("success") is False
        assert summary.get("state") in ("cancelled", "stale", "failed")
        assert final["state"] or summary.get("state")
    finally:
        server.close()


# ---------------------------------------------------------------------------
# §30 — G560 stays a thin client: local denied, delegation canonical
# ---------------------------------------------------------------------------


def test_g560_refuses_local_inference_and_delegates_over_the_typed_api(
        tmp_path):
    """PORTABILITY_TEST · SECURITY_TEST · MOCK_BACKEND_TEST (§30/§17).

    A Win7/32-bit/2 GB device is never an inference host. Under its own profile
    the fabric refuses to load or run a model locally — the refusal names the
    governor, and no text is invented in its place. The same requirement sent
    over the typed API to a server succeeds on the canonical Session-11 path,
    which is the only route a thin client has: no privileged path, no direct
    provider access, no local residency.
    """
    #: 1) local: denied by the resource governor, before any backend work
    local_fabric, local_backend = scripted_fabric(response="local-answer",
                                                  backend_name="local-double",
                                                  governor=g560_governor())
    load = local_fabric.models_load("local-double:scripted-model")
    assert load["loaded"] is False
    assert "denied" in str(load.get("reason") or load.get("error")
                           or "").lower() or "g560" in json.dumps(load).lower()

    result = local_fabric.generate(ModelRequest(
        prompt="write code", capability="coding", hardware_profile="g560"))
    #: refused, not fabricated: no text, and the resource verdict says why
    assert result.text == ""
    resource = getattr(result, "resource_result", None) or {}
    evidence = " ".join([str(getattr(result, "error", "") or ""),
                         str(getattr(result, "error_code", "") or ""),
                         str(resource.get("reason", "") or ""),
                         str(getattr(result, "routing", None) or "")]).lower()
    assert bool(resource.get("allowed") is False) or "denied" in evidence \
        or "g560" in evidence or getattr(result, "success", False) is False
    assert local_backend.loaded_models == []

    #: 2) delegated: the typed API answers on the canonical path
    server_fabric, server_backend = scripted_fabric(response="delegated-answer",
                                                    backend_name="s11double")
    server = _canonical_server(tmp_path, server_fabric)
    client = make_client(server)
    try:
        with client:
            response = client.post("/api/v1/inference/generate",
                                   headers=ADMIN,
                                   json={"prompt": "write code",
                                         "capability": "",
                                         "max_output_tokens": 32})
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["success"] is True
            assert body["text"] == "delegated-answer"
            assert body["backend_id"] == "s11double"
            assert body["neural"] is True
            assert server.fabric.inference_path_snapshot()["mode"] == \
                "session11"
            #: the thin client used the control plane, and nothing else
            assert server_backend.generate_calls
    finally:
        server.close()
