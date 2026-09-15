"""Session 11.5 (§35) — the acceptance test, and the honest counter-case.

The claim under test is the one that matters:

    "Submit a task, disconnect the client, and Forge continues autonomous
    engineering using the canonical verified inference fabric."

So the submitting connection is closed *before* the loop does any of its work,
the run is driven by the real queue → scheduler → worker → SupervisorExecutor →
Supervisor → ModelFabric → Session-11 InferenceFabric chain, and full status is
recovered afterwards over a brand-new connection.

Two runs are asserted, because one number cannot be honest on its own:

* ``MOCK_BACKEND_TEST`` — a labelled test double stands in for a production
  model. It *is* verified by the catalog, so the whole pipeline (write, test,
  review, security, acceptance, commit) really runs to completion. No
  production foundation model exists in this repository's test environment, and
  nothing here claims otherwise.
* ``REAL_INFERENCE_TEST`` / ``DETERMINISTIC_REFERENCE_MODEL`` — the first-party
  reference engine, a real forward pass. It advertises no coding capability, so
  capability routing refuses it, the deterministic rung answers, the coder
  honestly fails the task, the run rolls back, and the durable record says
  ``neural=false``. That is the invariant holding: a tiny character model is
  never dressed up as an engineering model, and a failure is never laundered
  into a success.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helpers_a34 import CSV_PAYLOAD, ScriptedProvider, make_fabric  # noqa: E402
from helpers_s11 import scripted_fabric, write_reference_artifact  # noqa: E402
from helpers_server import (  # noqa: E402
    TEST_TOKEN, approve_until_terminal, auth_headers, create_task, git_log,
    make_client, make_server)

from forge.models.inference_path import PATH_SESSION11  # noqa: E402
from forge.server.inference import (  # noqa: E402
    InferenceServiceConfig, ServerInferenceService)

ADMIN = auth_headers(TEST_TOKEN)

#: What the legacy provider would have written. Its absence is the proof that
#: the old path never answered (§3: no silent fallback).
LEGACY_PAYLOAD = json.dumps({
    "summary": "legacy path marker",
    "changes": [{"path": "legacy_marker.py", "action": "create",
                 "content": "LEGACY = True\n"}],
    "tests_to_run": [],
    "reasoning_summary": "legacy",
    "risk_level": "low",
})

#: §14 — the durable minimum a completed task must record about its inference.
DURABLE_KEYS = (
    "path", "inference_mode", "canonical_inference", "request_id", "task_id",
    "attempt_id", "generation_id", "trace_id", "model", "model_id",
    "backend_id", "provider_id", "verification_state", "availability_state",
    "neural", "deterministic", "finish_reason", "error_code", "latency_ms",
    "input_tokens", "output_tokens", "truncated", "fabric_id", "fence_state",
    "boot_id", "lease_owner",
)


def _server(tmp_path: Any, *, inference_fabric: Any = None,
            reference_dir: Any = None) -> Any:
    """A real autonomous server whose canonical inference path is Session 11.

    Production classes only: durable TaskQueue, Scheduler, WorkerPool,
    SupervisorExecutor, Supervisor, ModelFabric seam, InferenceFabric. The
    legacy fabric is present exactly as it is in production — and is asserted
    never to answer.
    """
    from forge.models.inference_path import InferencePathConfig

    legacy = make_fabric(ScriptedProvider(LEGACY_PAYLOAD))
    path_config = InferencePathConfig(mode="session11")
    if reference_dir is not None:
        inference = InferenceServiceConfig(
            enabled=True, reference_dirs=(str(reference_dir),),
            auto_verify=True, require_verified=True,
            max_model_slots=2, max_concurrent_requests=2)
    else:
        inference = InferenceServiceConfig(
            enabled=True, require_verified=True, max_concurrent_requests=2)
    server = make_server(tmp_path, executor=None, fabric=legacy,
                         profile="autonomous", inference=inference,
                         inference_path=path_config, max_retries=0)
    if inference_fabric is not None:
        #: The documented production seam: same service class, explicit fabric.
        server.inference = ServerInferenceService(
            config=inference, fabric=inference_fabric,
            governor=server.governor, audit=server.audit,
            emit=server.emit, fences=server.fences)
        server.fabric.attach_inference_path(server.inference, path_config,
                                            fences=server.fences)
    return server


def _wait_for(server: Any, task_id: str, predicate: Callable[[Any], bool],
              timeout: float = 240.0) -> Any:
    """Poll the *server* — deliberately not a client — until predicate holds."""
    deadline = time.time() + timeout
    record = server.tasks.get_or_raise(task_id)
    while time.time() < deadline:
        record = server.tasks.get_or_raise(task_id)
        if predicate(record):
            return record
        time.sleep(0.05)
    return record


def _events(server: Any, task_id: str) -> List[Dict[str, Any]]:
    events, _seq = server.events.list(task_id)
    return [{"type": event.type, "seq": event.seq, "data": event.data}
            for event in events]


def _types(events: List[Dict[str, Any]]) -> List[str]:
    return [event["type"] for event in events]


def _committed_files(root: Path) -> str:
    """The files in HEAD's commit (empty string when there is no commit)."""
    try:
        return subprocess.run(
            ["git", "show", "--name-only", "--format="],
            cwd=str(root), text=True, capture_output=True).stdout
    except Exception:                                    # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# §35 — submit, disconnect, and the loop finishes on the canonical path
# ---------------------------------------------------------------------------


def test_submit_disconnect_and_the_loop_finishes_on_the_canonical_path(
        tmp_path):
    """INTEGRATION_TEST · MOCK_BACKEND_TEST (§35, and §6/§10/§14/§16/§27/§28).

    USER SUBMITS → TASK PERSISTED → CLIENT DISCONNECTS → WORKER CONTINUES →
    SESSION11 INFERENCE → VERIFIED MODEL → CODE CHANGE → CHECKPOINT → WRITE →
    TEST → REVIEW → SECURITY → ACCEPTANCE → COMMIT → RESULT PERSISTED →
    CLIENT RECONNECTS → FULL STATUS RECOVERED.

    The double is a labelled mock: it is verified by the catalog and answers
    with a real change set, so every gate downstream runs for real. It is not a
    production model and is never described as one.
    """
    fabric, backend = scripted_fabric(response=CSV_PAYLOAD,
                                      backend_name="s11double",
                                      model_name="s11-model")
    server = _server(tmp_path, inference_fabric=fabric)
    root = Path(server.projects.get("demo").root)
    #: unrelated work in progress, which no outcome may touch (§27)
    (root / "USER_WIP.txt").write_text("unrelated in-progress work\n",
                                       encoding="utf-8")
    try:
        # -- 1. USER SUBMITS, TASK PERSISTED --------------------------------
        submitter = make_client(server)
        with submitter:
            task = create_task(submitter, ADMIN, "Add CSV export functionality",
                               mode="autonomous")
            task_id = task["task_id"]
            persisted = submitter.get("/api/v1/tasks/%s" % task_id,
                                      headers=ADMIN).json()["task"]
            assert persisted["status"] in ("created", "queued", "started",
                                           "running", "waiting_for_approval")
            assert persisted["requirement"].startswith("Add CSV export")

        # -- 2. CLIENT DISCONNECTS ------------------------------------------
        #: The submitting connection is gone from here on. Nothing below uses
        #: it, and the worker never learns it existed.
        disconnected_at = time.time()

        # -- 3. WORKER CONTINUES … all the way to the one sensitive gate -----
        waiting = _wait_for(
            server, task_id,
            lambda record: record.status.value == "waiting_for_approval"
            or record.terminal)
        assert waiting.status.value == "waiting_for_approval", (
            "the loop should reach the git-commit gate unattended, got %s (%s)"
            % (waiting.status.value, waiting.error or ""))

        events = _events(server, task_id)
        #: the canonical boundary was crossed, with no client attached
        assert "inference.path" in _types(events)
        path_event = [event for event in events
                      if event["type"] == "inference.path"][0]["data"]
        assert path_event["mode"] == "session11"
        assert path_event["attached"] is True
        #: inference → verified model → change proposed → written → tested →
        #: reviewed → security → acceptance, in that order, unattended
        order = _types(events)
        for expected in ("task.running", "inference.path", "model.selected",
                         "change.proposed", "changes.applied",
                         "tests.executed", "review.completed",
                         "security.completed", "acceptance.completed",
                         "approval.required"):
            assert expected in order, (expected, order)
        assert order.index("inference.path") < order.index("model.selected")
        assert order.index("model.selected") < order.index("change.proposed")
        assert order.index("changes.applied") < order.index("tests.executed")
        assert order.index("tests.executed") < order.index("review.completed")
        assert order.index("review.completed") < order.index(
            "security.completed")
        assert order.index("security.completed") < order.index(
            "acceptance.completed")
        #: a checkpoint exists to roll back to, taken before the writes
        assert waiting.checkpoint_id
        assert order.index("checkpoint.created") < order.index(
            "changes.applied")
        #: the change set is on disk already; the legacy path never answered
        assert "export_csv" in (root / "app.py").read_text(encoding="utf-8")
        assert not (root / "legacy_marker.py").exists()
        assert [call for call in backend.generate_calls
                if "verification probe" not in (call.prompt or "")]
        #: unrelated user work is untouched mid-run
        assert (root / "USER_WIP.txt").read_text(
            encoding="utf-8") == "unrelated in-progress work\n"

        # -- 4. the one sensitive gate, answered over a *different* connection
        #: Approvals are durable server state (§21): they belong to the
        #: operator, not to the connection that submitted the task.
        operator = make_client(server)
        with operator:
            pending = operator.get("/api/v1/approvals",
                                   headers=ADMIN).json()["approvals"]
            assert [record["payload"]["operation"]
                    for record in pending] == ["git:commit"]
            done = approve_until_terminal(operator, ADMIN, task_id, timeout=240)
        assert done["status"] == "completed", done.get("error", "")

        # -- 5. RESULT PERSISTED, with the §14 durable minimum --------------
        record = server.tasks.get_or_raise(task_id)
        result = json.loads(record.result_json or "{}")
        inference = result.get("inference") or {}
        missing = [key for key in DURABLE_KEYS if key not in inference]
        assert not missing, "durable inference metadata missing %s" % missing
        assert inference["path"] == PATH_SESSION11
        assert inference["canonical_inference"] is True
        assert inference["inference_mode"] == "session11"
        assert inference["verification_state"] == "verified"
        assert inference["availability_state"] == "ready"
        assert inference["neural"] is True
        assert inference["deterministic"] is False
        assert inference["model"] == "s11double:s11-model"
        assert inference["model_id"] == "s11double:s11-model"
        assert inference["backend_id"] == "s11double"
        assert inference["provider_id"] == "s11double"
        assert inference["fabric_id"] == fabric.fabric_id
        assert inference["attempt_id"] == "%s#g1" % task_id
        assert inference["generation_id"] == "%s#g1" % task_id
        assert inference["task_id"] == task_id
        assert inference["trace_id"]
        assert inference["request_id"].startswith("inf-")
        assert inference["finish_reason"] == "stop"
        assert inference["error_code"] == ""
        assert inference["truncated"] is False
        assert inference["fallback_used"] is False
        assert float(inference["latency_ms"]) >= 0.0
        assert int(inference["output_tokens"]) > 0
        #: the fence state recorded is the one at publication, not mid-flight
        assert inference["fence_state"] == "SUCCEEDED"
        assert inference["published_authorized"] is False
        assert inference["boot_id"] == server.boot_id
        assert inference["lease_owner"]

        #: §14/§33 — durable metadata is identifiers and verdicts, never content
        blob = json.dumps(result)
        assert CSV_PAYLOAD not in blob
        assert "export_csv" not in json.dumps(inference)
        assert "def export_csv" not in blob

        # -- 6. §27/§28 — the commit is scoped, and unrelated work survives --
        assert "forge: Add CSV export functionality" in git_log(root)
        committed = _committed_files(root)
        assert "app.py" in committed
        assert "USER_WIP.txt" not in committed
        assert (root / "USER_WIP.txt").read_text(
            encoding="utf-8") == "unrelated in-progress work\n"

        # -- 7. §8/§16 — a finished attempt may not publish again ------------
        authority = server.authority_for(
            task_id, lease_owner=server.queue.lease_owner(task_id))
        assert authority.publish_authorized() is False
        assert authority.denial_reason() in ("STALE_ATTEMPT", "LEASE_LOST")
        assert authority.fence_state == "SUCCEEDED"
        assert record.finished_at >= disconnected_at

        # -- 8. CLIENT RECONNECTS → FULL STATUS RECOVERED --------------------
        reconnected = make_client(server)
        with reconnected:
            body = reconnected.get("/api/v1/tasks/%s" % task_id,
                                   headers=ADMIN).json()["task"]
            assert body["status"] == "completed"
            assert body["progress"] == 1.0
            assert body["stage"] == "completed"
            assert body["inference"]["path"] == PATH_SESSION11
            assert body["inference"]["authorized_to_publish"] is False
            assert body["inference"]["fence_state"] == "SUCCEEDED"
            assert body["result"]["files"]

            dedicated = reconnected.get(
                "/api/v1/tasks/%s/inference" % task_id,
                headers=ADMIN).json()
            assert dedicated["inference"]["model_id"] == "s11double:s11-model"
            assert dedicated["inference"]["canonical_inference"] is True

            listing = reconnected.get("/api/v1/tasks/%s/events" % task_id,
                                      headers=ADMIN).json()
            recovered = [event["type"] for event in listing["events"]]
            for expected in ("task.created", "task.queued", "task.started",
                             "checkpoint.created", "task.running",
                             "inference.path", "changes.applied",
                             "review.completed", "security.completed",
                             "acceptance.completed", "approval.approved",
                             "git.commit", "task.completed"):
                assert expected in recovered, (expected, recovered)
            assert listing["latest_seq"] >= len(recovered) - 1
            #: a cursor replay from the middle duplicates nothing (§15)
            mid = listing["events"][len(recovered) // 2]["seq"]
            tail = reconnected.get(
                "/api/v1/tasks/%s/events?after=%d" % (task_id, mid),
                headers=ADMIN).json()["events"]
            assert tail and all(event["seq"] > mid for event in tail)

            logs = reconnected.get("/api/v1/tasks/%s/logs" % task_id,
                                   headers=ADMIN)
            assert logs.status_code == 200
            status = reconnected.get("/api/v1/status",
                                     headers=ADMIN).json()
            assert status["inference_path"]["mode"] == "session11"
            assert status["inference_path"]["attached"] is True
            assert status["tasks"]["completed"] >= 1
    finally:
        server.close()


# ---------------------------------------------------------------------------
# §29/§36 — the honest counter-case: a real forward pass that is not a coder
# ---------------------------------------------------------------------------


def test_a_real_reference_run_fails_honestly_and_rolls_back(tmp_path):
    """REAL_INFERENCE_TEST · DETERMINISTIC_REFERENCE_MODEL (§29/§27/§36).

    The reference engine is a real forward pass over a tiny first-party model.
    It advertises no coding capability, so routing refuses it for engineering
    work and the deterministic non-neural rung answers instead — and the coder,
    faced with text that is not a change set, fails the task rather than
    inventing one. The run rolls back, nothing is committed, unrelated user work
    survives, and the durable record says ``neural=false``.
    """
    models = tmp_path / "models"
    write_reference_artifact(models, "reference-clm")
    server = _server(tmp_path, reference_dir=models)
    root = Path(server.projects.get("demo").root)
    (root / "USER_WIP.txt").write_text("unrelated in-progress work\n",
                                       encoding="utf-8")
    before = git_log(root)
    try:
        submitter = make_client(server)
        with submitter:
            task = create_task(submitter, ADMIN, "Add CSV export functionality",
                               mode="autonomous")
            task_id = task["task_id"]
        #: client gone; the loop runs, and terminates honestly on its own
        record = _wait_for(server, task_id,
                           lambda item: item.terminal, timeout=240)
        assert record.terminal
        assert record.status.value == "failed", record.status.value
        assert "no working code model" in (record.error or "").lower()

        events = _events(server, task_id)
        order = _types(events)
        assert "inference.path" in order
        #: the rollback really happened, and it was not a git reset --hard
        assert "rollback.completed" in order
        assert "task.failed" in order
        assert "git.commit" not in order

        result = json.loads(record.result_json or "{}")
        inference = result.get("inference") or {}
        assert inference, "a failed run still records what answered"
        assert inference["path"] == PATH_SESSION11
        assert inference["canonical_inference"] is True
        assert inference["inference_mode"] == "session11"
        #: §29 — never label a deterministic answer as AI inference
        assert inference["neural"] is False
        assert inference["deterministic"] is True
        assert inference["fallback_used"] is True
        assert inference["backend_id"] == "deterministic"
        assert "capability" in str(inference.get("routing_reason", "")).lower()
        assert inference["attempt_id"] == "%s#g1" % task_id
        assert inference["fabric_id"]

        #: nothing was written, nothing was committed, user work is intact
        assert "export_csv" not in (root / "app.py").read_text(
            encoding="utf-8")
        assert not (root / "legacy_marker.py").exists()
        assert git_log(root) == before
        assert (root / "USER_WIP.txt").read_text(
            encoding="utf-8") == "unrelated in-progress work\n"

        #: and the failed attempt may not publish afterwards either
        authority = server.authority_for(
            task_id, lease_owner=server.queue.lease_owner(task_id))
        assert authority.publish_authorized() is False
    finally:
        server.close()
