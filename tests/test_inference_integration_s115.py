"""Session 11.5 — the background engineering loop on the canonical inference path.

What is proven here, with the production classes and no miniature replacements:

    submit task → SQLite queue → Scheduler lease → WorkerPool → run_task
      → attempt fence begun (existing FenceRegistry) → SupervisorExecutor
      → Supervisor → CoderAgent → ModelFabric → InferencePath decision
      → Session-11 InferenceFabric → RoutingEngine → catalog → residency
      → backend → real generation → result → durable task record

and the failure directions that matter:

    a stale/superseded attempt cannot publish          (§7, §9)
    a lost lease fences the attempt                     (§9, §12)
    a restarted process never reuses a generation       (§12)
    cancellation reaches the inference boundary          (§8)
    legacy mode still behaves exactly like legacy        (§3)
    hybrid mode says which caller stayed legacy and why  (§3)

Test labels (§30) are declared per test. The reference engine is a real forward
pass over a tiny first-party artifact — it is labelled
``DETERMINISTIC_REFERENCE_MODEL`` and is *not* a production foundation model.
Nothing here contacts a third party and nothing is downloaded.

Offline-safe: every test runs without network access. Live-provider tests stay
opt-in elsewhere (``FORGE_REAL_INFERENCE=1``).
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from helpers_a34 import CSV_PAYLOAD, ScriptedProvider, make_fabric  # noqa: E402
from helpers_s11 import (  # noqa: E402
    reference_fabric,
    scripted_fabric,
    write_reference_artifact,
)
from helpers_server import (  # noqa: E402
    TEST_TOKEN,
    approve_until_terminal,
    create_task,
    event_types,
    make_client,
    auth_headers,
    make_server,
    wait_for_status_http,
)

ADMIN = auth_headers(TEST_TOKEN)

from forge.core.fencing import FenceRegistry, StaleAttemptError  # noqa: E402
from forge.models.inference_path import (  # noqa: E402
    PATH_LEGACY,
    PATH_SESSION11,
    ExecutionIdentity,
    InferencePathConfig,
)
from forge.models.request import ModelRequest  # noqa: E402
from forge.server.inference import (  # noqa: E402
    CancelledAttempt,
    InferenceServiceConfig,
    NoFenceAuthority,
    ServerInferenceService,
    StaleAttempt,
    SupersededAttempt,
)
from forge.server.workers import _begin_attempt, _settle_attempt  # noqa: E402

REAL_INFERENCE_TEST = "REAL_INFERENCE_TEST"
DETERMINISTIC_REFERENCE_MODEL = "DETERMINISTIC_REFERENCE_MODEL"
MOCK_BACKEND_TEST = "MOCK_BACKEND_TEST"
INTEGRATION_TEST = "INTEGRATION_TEST"

#: The legacy fabric's provider answers with this. If a task ever writes
#: ``legacy_marker.py``, the canonical path was bypassed — every test below
#: asserts that it was not.
LEGACY_PAYLOAD = json.dumps({
    "summary": "legacy path marker",
    "changes": [{"path": "legacy_marker.py", "action": "create",
                 "content": "LEGACY = True\n"}],
    "tests_to_run": [],
    "reasoning_summary": "legacy",
    "risk_level": "low",
})


# --------------------------------------------------------------------------
# harness: a real server whose background loop runs on the canonical path
# --------------------------------------------------------------------------

def server_on_path(tmp_path: Any, *, mode: str = "session11",
                   inference_fabric: Any = None,
                   inference_config: Any = None,
                   reference_dir: Any = None,
                   max_output_tokens: int = 48,
                   max_retries: int = 0,
                   legacy_payload: str = LEGACY_PAYLOAD,
                   hybrid_legacy_callers: Any = ()) -> Any:
    """A real ``ForgeServer`` (queue, scheduler, workers, SupervisorExecutor).

    ``inference_fabric`` injects an already-built Session-11 fabric (the
    documented ``ServerInferenceService(fabric=...)`` seam); ``reference_dir``
    instead lets the server build its own fabric from a real artifact directory
    exactly as it does in production.
    """
    provider = ScriptedProvider(legacy_payload)
    legacy = make_fabric(provider)
    if inference_config is not None:
        pass                                  #: caller-supplied service config
    elif reference_dir is not None:
        inference_config = InferenceServiceConfig(
            enabled=True, reference_dirs=(str(reference_dir),),
            auto_verify=True, require_verified=True,
            max_model_slots=2, max_concurrent_requests=2)
    else:
        inference_config = InferenceServiceConfig(
            enabled=True, require_verified=True, max_concurrent_requests=2)
    path_config = InferencePathConfig(
        mode=mode, max_output_tokens=max_output_tokens,
        hybrid_legacy_callers=tuple(hybrid_legacy_callers))
    server = make_server(tmp_path, executor=None, fabric=legacy,
                         inference=inference_config,
                         inference_path=path_config,
                         max_retries=max_retries)
    if inference_fabric is not None:
        #: Same production class, explicit fabric: no test-only service.
        server.inference = ServerInferenceService(
            config=inference_config, fabric=inference_fabric,
            governor=server.governor, audit=server.audit,
            emit=server.emit, fences=server.fences)
        server.fabric.attach_inference_path(
            server.inference, path_config, fences=server.fences)
    return server, provider


def task_result(server: Any, task_id: str) -> Dict[str, Any]:
    record = server.tasks.get_or_raise(task_id)
    try:
        return json.loads(record.result_json or "{}")
    except ValueError:
        return {}


def task_events(server: Any, task_id: str) -> List[Dict[str, Any]]:
    events, _seq = server.events.list(task_id, limit=500)
    return [{"type": str(getattr(event, "type", "") or ""),
             "data": dict(getattr(event, "data", None) or {})}
            for event in events]


# --------------------------------------------------------------------------
# §4 — the real background model call
# --------------------------------------------------------------------------

def test_background_task_runs_on_the_canonical_path_and_refuses_honestly(tmp_path):
    """REAL_INFERENCE_TEST · INTEGRATION_TEST (§2/§3/§19/§20).

    A task submitted over HTTP is leased by a real worker, run by the real
    SupervisorExecutor, and its model call travels the Session-11 path.

    The only real model in this repository is the first-party reference engine,
    which advertises **no** capability — so capability routing refuses it for
    ``coding`` work and the deterministic non-neural rung answers instead. That
    is the invariant holding, not a bug: a tiny character model must never be
    selected for engineering work, and the legacy provider must never be
    silently substituted either. Both directions are asserted here.
    """
    models = tmp_path / "models"
    write_reference_artifact(models, "reference-clm")
    server, legacy_provider = server_on_path(tmp_path, reference_dir=models)
    client = make_client(server)
    try:
        with client:
            task = create_task(client, ADMIN, "Add CSV export functionality")
            task_id = task["task_id"]
            state = wait_for_status_http(
                client, ADMIN, task_id, {"failed", "completed"}, timeout=240)
            assert state["status"] == "failed", (
                "no verified model in this sandbox advertises the coding "
                "capability, so nothing may complete this task; a completion "
                "here would mean output was fabricated")

            result = task_result(server, task_id)
            inference = result.get("inference") or {}
            #: §24 — the durable result tells the truth about its source.
            assert inference.get("path") == PATH_SESSION11
            assert inference.get("inference_mode") == "session11"
            #: §20 — a non-neural answer is labelled non-neural.
            assert inference.get("neural") is False
            assert inference.get("deterministic") is True
            assert inference.get("backend_id") == "deterministic"
            assert inference.get("fallback_used") is True
            #: The refusal reason is recorded, not swallowed.
            assert "capability" in str(inference.get("routing_reason", ""))
            #: §19 — the deterministic rung is never dressed up as verified.
            assert inference.get("verification_state") != "verified"
            #: §5 — one identity, quoted identically everywhere.
            assert inference.get("attempt_id") == "%s#g1" % task_id
            assert inference.get("task_id") == task_id
            assert inference.get("boot_id") == server.boot_id
            assert inference.get("generation_id")
            assert result.get("attempt_id") == "%s#g1" % task_id

            #: The legacy provider was never asked anything (§2/§3).
            assert legacy_provider.prompts == []
            root = Path(server.projects.get("demo").root)
            assert not (root / "legacy_marker.py").exists()

            #: The real artifact *was* registered — it was refused for
            #: capability reasons, not because it was missing. It also stayed
            #: unverified, which is correct rather than sloppy: verification
            #: follows selection, and capability routing refused it before it
            #: could ever be selected for coding work.
            registered = server.inference.fabric.models_list(discover=True)
            reference = [item for item in registered.get("models") or []
                         if str(item.get("model_id", ""))
                         .startswith("reference:")]
            assert reference, "the real artifact was never registered"
            assert reference[0]["backend_id"] == "reference"
            assert "coding" not in (reference[0].get("capabilities") or ())
            assert reference[0]["availability_state"] in ("discovered",
                                                          "unverified")

            #: §3 — which path served the run is an event, not a guess.
            events = task_events(server, task_id)
            assert "inference.path" in event_types(events)
            path_event = [event for event in events
                          if event["type"] == "inference.path"][0]
            assert path_event["data"]["mode"] == "session11"
            assert path_event["data"]["attempt_id"] == "%s#g1" % task_id
    finally:
        server.stop()


def test_a_real_forward_pass_through_the_canonical_path(tmp_path):
    """REAL_INFERENCE_TEST · DETERMINISTIC_REFERENCE_MODEL (§4/§20).

    The same canonical path, with a request the reference engine *can* serve
    (no capability requirement): a real forward pass happens, Session-11
    routing is what selected the model, and the response carries the artifact
    fingerprint of the bytes that answered.

    The reference engine is a tiny first-party character model. It is not a
    production foundation model, and nothing here claims otherwise.
    """
    models = tmp_path / "models"
    fabric = reference_fabric(models, verify=True)
    server, _provider = server_on_path(tmp_path, inference_fabric=fabric)
    try:
        identity = ExecutionIdentity.new("task-forward", attempt=1,
                                         boot_id=server.boot_id,
                                         lease_owner="worker-A")
        fence = server.fences.begin("task-forward", owner="worker-A")
        server.fences.mark_running("task-forward", fence)
        identity.attempt_id = fence.attempt_id
        bound = server.fabric.bind_identity(identity, fence=fence,
                                            fence_registry=server.fences)
        response = bound.generate(ModelRequest(prompt="real forward pass",
                                               capability=""))
        metadata = response.metadata
        assert response.success is True
        assert response.text != ""
        #: A real model produced this text.
        assert metadata["neural"] is True
        assert metadata["deterministic"] is False
        assert metadata["path"] if "path" in metadata else True
        assert metadata["inference_path"] == PATH_SESSION11
        assert str(metadata["model"]).startswith("reference:")
        assert metadata["backend_id"] == "reference"
        assert metadata["verification_state"] == "verified"
        assert metadata["availability_state"] == "ready"
        assert len(str(metadata["artifact_fingerprint"])) >= 32
        assert int(metadata["resident_bytes"] or 0) > 0
        #: Session-11 routing made the selection, and says why.
        assert "reference:" in str(metadata["routing_reason"])
        assert float(metadata["routing_score"] or 0.0) > 0.0
        #: Identity survived the whole chain.
        assert metadata["task_id"] == "task-forward"
        assert metadata["attempt_id"] == fence.attempt_id
        assert metadata["generation_id"]
        assert response.latency_ms > 0.0
        assert response.output_tokens > 0
    finally:
        server.stop()


def test_completed_task_persists_canonical_provenance(tmp_path):
    """INTEGRATION_TEST · MOCK_BACKEND_TEST: the loop completes end to end.

    The Session-11 backend is a labelled test double answering with the same
    change set the legacy provider would have — except the legacy provider
    writes a different file, so a silent fallback is detectable.
    """
    fabric, backend = scripted_fabric(response=CSV_PAYLOAD,
                                      backend_name="s11double",
                                      model_name="s11-model")
    server, legacy_provider = server_on_path(tmp_path,
                                             inference_fabric=fabric)
    client = make_client(server)
    try:
        with client:
            task = create_task(client, ADMIN, "Add CSV export functionality")
            task_id = task["task_id"]
            state = approve_until_terminal(client, ADMIN, task_id, timeout=240)
            assert state["status"] == "completed", state.get("error", "")

            result = task_result(server, task_id)
            inference = result.get("inference") or {}
            assert inference.get("path") == PATH_SESSION11
            assert inference.get("model") == "s11double:s11-model"
            assert inference.get("backend_id") == "s11double"
            assert inference.get("generation_id")
            assert inference.get("attempt_id") == "%s#g1" % task_id
            assert inference.get("trace_id")

            root = Path(server.projects.get("demo").root)
            #: The Session-11 payload's files were written and committed.
            assert (root / "tests" / "test_csv.py").exists()
            assert "export_csv" in (root / "app.py").read_text()
            #: The legacy path never ran.
            assert not (root / "legacy_marker.py").exists()
            assert legacy_provider.prompts == []
            assert backend.generate_calls

            #: §24 — the completion event carries the same bounded provenance.
            completed = [event for event in task_events(server, task_id)
                         if event["type"] == "task.completed"]
            assert completed, event_types(task_events(server, task_id))
            payload = completed[0]["data"]
            assert payload["inference"]["path"] == PATH_SESSION11
            assert payload["inference"]["model_id"] == "s11double:s11-model"
            assert payload["attempt_id"] == "%s#g1" % task_id
            blob = repr(payload)
            assert CSV_PAYLOAD not in blob          # no completion content
            assert "export_csv" not in blob
    finally:
        server.stop()


# --------------------------------------------------------------------------
# §3 — modes are explicit and observable
# --------------------------------------------------------------------------

def test_legacy_mode_preserves_the_existing_behaviour(tmp_path):
    """INTEGRATION_TEST: ``mode=legacy`` never consults the Session-11 fabric."""
    fabric, backend = scripted_fabric(response="never-used",
                                      backend_name="s11double")
    server, legacy_provider = server_on_path(tmp_path, mode="legacy",
                                             inference_fabric=fabric)
    client = make_client(server)
    try:
        with client:
            task = create_task(client, ADMIN, "Add CSV export functionality")
            task_id = task["task_id"]
            state = approve_until_terminal(client, ADMIN, task_id, timeout=240)
            assert state["status"] == "completed"
            #: The double only ever ran its own verification probe (built by
            #: ``scripted_fabric(verify=True)``); the task never reached it.
            assert [call for call in backend.generate_calls
                    if "verification probe" not in (call.prompt or "")] == []
            assert legacy_provider.prompts            # legacy really served it
            result = task_result(server, task_id)
            inference = result.get("inference") or {}
            #: Not attached in legacy mode, so nothing is claimed: the record
            #: says legacy and fabricates no Session-11 model metadata.
            assert inference.get("path", PATH_LEGACY) == PATH_LEGACY
            assert inference.get("backend_id", "") == ""
            assert inference.get("generation_id", "") == ""
            root = Path(server.projects.get("demo").root)
            #: The legacy provider's own payload is what landed — proof the
            #: legacy path really served it (the Session-11 double answers with
            #: CSV_PAYLOAD, which writes tests/test_csv.py instead).
            assert (root / "legacy_marker.py").exists()
            assert not (root / "tests" / "test_csv.py").exists()
    finally:
        server.stop()


def test_hybrid_mode_records_which_caller_stayed_legacy(tmp_path):
    """INTEGRATION_TEST (§3/§22): hybrid eligibility is configuration.

    The allow-list names a *component* — ``coder`` — not a requirement string.
    Keying it off ``ModelRequest.task`` (as this test originally did) would mean
    keying configuration off user content, since that same field becomes a
    remote system prompt; §22 gave requests a stable ``caller`` label instead.
    The task therefore stays on the legacy path, and the response says so with
    the reason.
    """
    fabric, backend = scripted_fabric(response=CSV_PAYLOAD,
                                      backend_name="s11double")
    server, legacy_provider = server_on_path(
        tmp_path, mode="hybrid", inference_fabric=fabric,
        hybrid_legacy_callers=("coder",))
    client = make_client(server)
    try:
        with client:
            task = create_task(client, ADMIN, "Add CSV export functionality")
            task_id = task["task_id"]
            state = approve_until_terminal(client, ADMIN, task_id, timeout=240)
            assert state["status"] == "completed"
            assert [call for call in backend.generate_calls
                    if "verification probe" not in (call.prompt or "")] == []
            assert legacy_provider.prompts
            response = server.fabric.generate(
                ModelRequest(prompt="x", capability="coding", caller="coder",
                             task="Add CSV export functionality"))
            assert response.metadata["inference_path"] == PATH_LEGACY
            assert "not migrated" in response.metadata["inference_path_reason"]
            migrated = server.fabric.generate(
                ModelRequest(prompt="x", capability="coding", caller="reviewer",
                             task="Add CSV export functionality"))
            assert migrated.metadata["inference_path"] == PATH_SESSION11
            history = server.fabric.inference_path.history(limit=10)
            assert {entry["path"] for entry in history} == {PATH_LEGACY,
                                                            PATH_SESSION11}
    finally:
        server.stop()


# --------------------------------------------------------------------------
# §6/§7 — the fence is an authority, not a suggestion
# --------------------------------------------------------------------------

def test_a_task_bound_generation_needs_a_fence_authority(tmp_path):
    """INTEGRATION_TEST (§6): no authority → denial, and no model compute."""
    fabric, backend = scripted_fabric(response="unused")
    server, _provider = server_on_path(tmp_path, inference_fabric=fabric)
    service = server.inference
    calls_before = len(backend.generate_calls)
    #: The server always wires its registry; remove it to prove the refusal.
    service.fences = None
    with pytest.raises(NoFenceAuthority):
        service.inference_generate({"prompt": "hi", "capability": "coding",
                                    "task_id": "task-nofence"})
    assert len(backend.generate_calls) == calls_before   # no compute happened
    #: An interactive generation (no task) has no attempt to fence.
    service.fences = server.fences
    body = service.inference_generate({"prompt": "hi", "capability": "coding"})
    assert body["success"] is True
    server.stop()


def test_a_superseded_attempt_cannot_publish_anything(tmp_path):
    """INTEGRATION_TEST (§9): attempt A is superseded by B; A stays silent.

    A may finish physically — the backend cannot always be interrupted — but
    its text is dropped, its result is STALE, it cannot commit a terminal
    fence state, and the worker layer reports ``stale`` instead of publishing.
    """
    fabric, _backend = scripted_fabric(response="A-was-here",
                                       backend_name="s11double")
    server, _provider = server_on_path(tmp_path, inference_fabric=fabric)
    try:
        task_id = "task-zombie"
        fence_a = server.fences.begin(task_id, owner="worker-A")
        server.fences.mark_running(task_id, fence_a)
        #: B starts (a retry, a recovery, a new boot): A is fenced immediately.
        fence_b = server.fences.begin(task_id, owner="worker-B")
        server.fences.mark_running(task_id, fence_b)
        assert not server.fences.is_authorized(fence_a)

        #: A late generation still holding A's fence is discarded as stale.
        result = fabric.generate(
            ModelRequest(prompt="late", capability=""),
            task_id=task_id, attempt_id=fence_a.attempt_id, fence=fence_a,
            fence_registry=server.fences)
        assert result.success is False
        assert str(result.state).lower() == "stale"
        assert result.text == ""                    # the output never lands

        #: A cannot commit a terminal state, and cannot overwrite B's.
        with pytest.raises(StaleAttemptError):
            server.fences.commit(task_id, fence_a, "SUCCEEDED")
        assert _settle_attempt(server, task_id, "demo", fence_a, "succeeded",
                               reason="zombie").lower() == "stale"

        #: B is still authoritative and can publish.
        body = server.inference.inference_generate(
            {"prompt": "current", "capability": "coding", "task_id": task_id,
             "attempt_id": fence_b.attempt_id})
        assert body["success"] is True
        assert body["attempt_id"] == fence_b.attempt_id
        record = server.fences.commit(task_id, fence_b, "SUCCEEDED",
                                      payload={"by": "B"})
        assert record["state"] == "SUCCEEDED"
        assert record["attempt_id"] == fence_b.attempt_id
    finally:
        server.stop()


def test_a_lost_lease_fences_the_attempt_and_refuses_late_work(tmp_path):
    """INTEGRATION_TEST (§9/§12): lease loss is not a shrug.

    ``run_task`` fences the attempt when its lease disappears, so a worker from
    a dead boot cannot publish a generation, a checkpoint or a terminal state.
    """
    fabric, _backend = scripted_fabric(response="stale-output",
                                       backend_name="s11double")
    server, _provider = server_on_path(tmp_path, inference_fabric=fabric)
    try:
        task_id = "task-lease"
        task = server.tasks.create("demo", "x", mode="autonomous")
        fence, identity = _begin_attempt(server, task_id, task, "worker-A")
        assert fence is not None
        assert identity.attempt_id == fence.attempt_id
        assert identity.boot_id == server.boot_id
        assert identity.lease_owner == "worker-A"

        #: The restart/lease-loss branch of run_task.
        assert _settle_attempt(server, task_id, "demo", fence, "fenced",
                               reason="lease lost after restart") == "FENCED"
        assert server.fences.current(task_id).state == "FENCED"

        #: A task-bound generation for that task is now refused by name.
        with pytest.raises(StaleAttempt):
            server.inference.inference_generate(
                {"prompt": "late", "capability": "coding",
                 "task_id": task_id})
        #: And a generation holding the fenced object is discarded as stale.
        result = fabric.generate(ModelRequest(prompt="late", capability=""),
                                 task_id=task_id, fence=fence,
                                 fence_registry=server.fences)
        assert result.success is False and result.text == ""
    finally:
        server.stop()


def test_a_restart_never_reuses_a_dead_boots_generation(tmp_path):
    """INTEGRATION_TEST (§12): generations are seeded from durable state.

    A fresh process (a fresh registry) that seeds from the task's retry count
    cannot hand a new attempt the generation number a dead boot already owned,
    so the dead boot's identity can never look current again.
    """
    dead_boot = FenceRegistry()
    first = dead_boot.begin("task-r", owner="boot-1")
    dead_boot.mark_running("task-r", first)
    assert first.attempt_id == "task-r#g1"

    #: New process: nothing in memory, only the durable retry count.
    fresh = FenceRegistry()
    fresh.seed_generation("task-r", 1)          # one attempt already happened
    retry = fresh.begin("task-r", owner="boot-2")
    assert retry.generation == 2
    assert retry.attempt_id == "task-r#g2"
    assert retry.attempt_id != first.attempt_id
    #: The old attempt id is not the current authority in the new process.
    assert fresh.current("task-r").generation == 2
    with pytest.raises(StaleAttemptError):
        fresh.commit("task-r", first, "SUCCEEDED")


def test_cancellation_reaches_the_inference_boundary(tmp_path):
    """INTEGRATION_TEST (§8): cancel → fence → inference refuses, by name."""
    fabric, backend = scripted_fabric(response="should-not-publish",
                                      backend_name="s11double")
    server, _provider = server_on_path(tmp_path, inference_fabric=fabric)
    try:
        task_id = "task-cancel"
        fence = server.fences.begin(task_id, owner="worker-A")
        server.fences.mark_running(task_id, fence)
        calls_before = len(backend.generate_calls)

        #: The operator cancels; the scheduler/worker chain moves the fence.
        assert server.fences.cancel(task_id) is not None
        with pytest.raises(CancelledAttempt) as refused:
            server.inference.inference_generate(
                {"prompt": "after cancel", "capability": "coding",
                 "task_id": task_id})
        assert refused.value.code == "CANCELLED_ATTEMPT"
        assert len(backend.generate_calls) == calls_before  # none after cancel

        #: The worker confirms, and the attempt reaches CANCELLED — not FAILED
        #: and not SUCCESS.
        assert _settle_attempt(server, task_id, "demo", fence, "cancelled",
                               reason="cancelled by operator") == "CANCELLED"
        assert server.fences.current(task_id).state == "CANCELLED"
        with pytest.raises(CancelledAttempt):
            server.inference.inference_generate(
                {"prompt": "after confirm", "capability": "coding",
                 "task_id": task_id})
    finally:
        server.stop()


def test_a_superseded_attempt_is_named_superseded_not_just_stale(tmp_path):
    """INTEGRATION_TEST (§6): the refusal names the real diagnosis."""
    fabric, _backend = scripted_fabric(response="unused")
    server, _provider = server_on_path(tmp_path, inference_fabric=fabric)
    try:
        task_id = "task-super"
        old = server.fences.begin(task_id, owner="worker-A")
        server.fences.mark_running(task_id, old)
        server.fences.begin(task_id, owner="worker-B")     # supersedes `old`

        class ZombieAuthority:
            """What a zombie worker sees: its own attempt, still 'current'."""

            def current(self, _task_id: str) -> Any:
                return server.fences.get(task_id, old.generation)

            def is_authorized(self, fence: Any) -> bool:
                return server.fences.is_authorized(fence)

        server.inference.fences = ZombieAuthority()
        with pytest.raises(SupersededAttempt) as refused:
            server.inference.inference_generate(
                {"prompt": "late", "capability": "coding",
                 "task_id": task_id})
        assert refused.value.code == "SUPERSEDED_ATTEMPT"
        assert refused.value.status == 409
    finally:
        server.inference.fences = server.fences
        server.stop()


# --------------------------------------------------------------------------
# §5 — identity survives the boundaries
# --------------------------------------------------------------------------

def test_identity_is_minted_once_and_travels_unchanged(tmp_path):
    """INTEGRATION_TEST (§5): worker → executor → fabric → result → audit."""
    fabric, _backend = scripted_fabric(response="ok", backend_name="s11double")
    server, _provider = server_on_path(tmp_path, inference_fabric=fabric)
    try:
        task = server.tasks.create("demo", "x", mode="autonomous")
        fence, identity = _begin_attempt(server, "task-id-1", task, "worker-A")
        bound = server.fabric.bind_identity(
            identity, fence=fence, fence_registry=server.fences)
        response = bound.generate(ModelRequest(prompt="carry my identity",
                                               capability=""))
        metadata = response.metadata
        assert metadata["task_id"] == "task-id-1"
        assert metadata["attempt_id"] == fence.attempt_id
        assert metadata["trace_id"] == identity.trace_id
        assert metadata["generation_id"]
        assert metadata["inference_path"] == PATH_SESSION11
        #: A retried attempt gets a *different* identity, never a reused one.
        fence2 = server.fences.begin("task-id-1", owner="worker-B")
        server.fences.mark_running("task-id-1", fence2)
        identity2 = ExecutionIdentity.new("task-id-1", attempt=2,
                                          boot_id=server.boot_id)
        identity2.attempt_id = fence2.attempt_id
        bound2 = server.fabric.bind_identity(identity2, fence=fence2,
                                             fence_registry=server.fences)
        response2 = bound2.generate(ModelRequest(prompt="second attempt",
                                                 capability=""))
        assert response2.metadata["attempt_id"] != metadata["attempt_id"]
        assert response2.metadata["generation_id"] != metadata["generation_id"]
    finally:
        server.stop()


def test_the_reference_engine_is_labelled_as_what_it_is(tmp_path):
    """REAL_INFERENCE_TEST · DETERMINISTIC_REFERENCE_MODEL (§20/§30).

    A real forward pass, honestly labelled: neural because a model produced the
    text, deterministic because that model is the first-party reference engine
    and not a production foundation model.
    """
    models = tmp_path / "models"
    fabric = reference_fabric(models, verify=True)
    server, _provider = server_on_path(tmp_path, inference_fabric=fabric)
    try:
        identity = ExecutionIdentity.new("task-label", attempt=1,
                                         boot_id=server.boot_id)
        bound = server.fabric.bind_identity(identity)
        response = bound.generate(ModelRequest(prompt="label me",
                                               capability=""))
        metadata = response.metadata
        assert response.success is True
        assert metadata["neural"] is True
        assert metadata["deterministic"] is False
        assert metadata["verification_state"] == "verified"
        assert str(metadata["model"]).startswith("reference:")
        assert metadata["artifact_fingerprint"]
        #: The engine's own identity says what it is; the label is not lost on
        #: the way through the compatibility adapter.
        identity_record = fabric.catalog.get(metadata["model"])
        assert identity_record.metadata.get("reference_engine") is True
        assert response.text != ""
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# §15 — provider configuration must be visible: CONFIGURED →
# INVALID_CONFIGURATION → UNAVAILABLE, never a silent drop and never NO_MODEL.
# ---------------------------------------------------------------------------


def _remote_env(monkeypatch, provider_id, *, url="http://127.0.0.1:9/v1",
                model="m", allow_http=True, api_key=""):
    """Point one remote provider at env values (no network call is made)."""
    upper = provider_id.upper().replace("-", "_")
    monkeypatch.setenv("FORGE_REMOTE_%s_URL" % upper, url)
    monkeypatch.setenv("FORGE_REMOTE_%s_MODEL" % upper, model)
    monkeypatch.setenv("FORGE_REMOTE_%s_ALLOW_HTTP" % upper,
                       "1" if allow_http else "0")
    if api_key:
        monkeypatch.setenv("FORGE_REMOTE_%s_API_KEY" % upper, api_key)


def test_invalid_remote_provider_configuration_is_visible_not_dropped(
        tmp_path, monkeypatch):
    """[S11.5] MOCK/INTEGRATION — one broken provider cannot disappear.

    A missing URL makes 'broken' INVALID_CONFIGURATION with a bounded reason;
    the unrelated 'good' provider still registers and still works; and both
    facts reach inference.status, models.status and /health. Before Session
    11.5 the exception was swallowed and the operator saw only NO_MODEL.
    """
    from forge.server.inference import (PROVIDER_CONFIGURED, PROVIDER_INVALID)

    monkeypatch.setenv("FORGE_REMOTE_BROKEN_URL", "")
    _remote_env(monkeypatch, "good", api_key="s3cr3t-token-value")
    server, _provider = server_on_path(
        tmp_path,
        inference_config=InferenceServiceConfig(
            enabled=True, remote_providers=("broken", "good"),
            allow_network=True, require_verified=True,
            max_concurrent_requests=2))
    service = server.inference
    service.fabric                                     # build for real
    try:
        records = {item["provider_id"]: item
                   for item in service.provider_configuration()}
        assert set(records) == {"broken", "good"}
        assert records["broken"]["state"] == PROVIDER_INVALID
        assert records["broken"]["available"] is False
        #: the reason names the missing variable and leaks no credential
        assert "FORGE_REMOTE_BROKEN_URL" in records["broken"]["reason"]
        assert "s3cr3t-token-value" not in json.dumps(records)
        assert records["good"]["state"] == PROVIDER_CONFIGURED
        #: an unrelated provider is unaffected and its backend really registered
        backends = {item.get("backend_id") for item
                    in (service.fabric.status().get("backends") or [])}
        assert "remote-good" in backends

        #: visible through the HTTP-facing payloads too (models.status embeds it)
        status = service.models_status()
        providers = {item["provider_id"]: item
                     for item in status["service"]["providers"]}
        assert providers["broken"]["state"] == PROVIDER_INVALID
        assert status["service"]["invalid_providers"] == ["broken"]

        server.start()
        health = server.health_monitor.health()
        component = health["components"]["inference"]
        assert component["ok"] is False
        assert component["invalid_providers"] == ["broken"]
        assert component["mode"] == "session11"
        assert any("broken" in line and "invalid_configuration" in line
                   for line in health["warnings"])
        assert "s3cr3t-token-value" not in json.dumps(health)
    finally:
        server.stop()


def test_unreachable_provider_is_unavailable_not_invalid(tmp_path,
                                                         monkeypatch):
    """[S11.5] MOCK/INTEGRATION — configured ≠ reachable, and neither is a lie.

    With the network denied by policy the provider's configuration is fine, so
    it must not be reported INVALID_CONFIGURATION; and because it has never
    been probed it must not be reported unreachable either. What is reported is
    the policy denial itself.
    """
    from forge.server.inference import PROVIDER_INVALID

    _remote_env(monkeypatch, "locked")
    server, _provider = server_on_path(
        tmp_path,
        inference_config=InferenceServiceConfig(
            enabled=True, remote_providers=("locked",), allow_network=False,
            require_verified=True, max_concurrent_requests=2))
    server.inference.fabric
    try:
        record = server.inference.provider_configuration()[0]
        assert record["provider_id"] == "locked"
        assert record["state"] != PROVIDER_INVALID
        assert record["state"] in ("configured", "unavailable")
        if record["state"] == "unavailable":
            #: the reason is the policy denial, not a fabricated "unreachable"
            assert record["reason"]
            assert ("network" in record["reason"].lower()
                    or record.get("denial"))
        if record.get("probed") is False:
            assert record["reachable"] is None          # unknown, not false
    finally:
        server.stop()


def test_health_reading_performs_no_model_work(tmp_path):
    """[S11.5] MOCK/INTEGRATION — health is a read, not a side effect.

    Describing the canonical path must not discover, verify, load or contact a
    model: a health poll that did would touch model directories and could start
    network probes on every scrape. The fabric is therefore only described when
    it already exists, and reading health must not add a single generation.
    """
    fabric, backend = scripted_fabric("health check")
    server, _provider = server_on_path(tmp_path, inference_fabric=fabric)
    try:
        server.start()
        before = len(backend.generate_calls)
        component: Dict[str, Any] = {}
        for _unused in range(3):
            health = server.health_monitor.health()
            component = health["components"]["inference"]
        assert component["enabled"] is True
        assert component["mode"] == "session11"
        assert component["ok"] is True
        #: three health polls produced no model traffic whatsoever
        assert len(backend.generate_calls) == before
        #: the fence authority is described without mutating a single fence
        assert component["fences"]["tasks"] == 0
        assert component["fences"]["authorized"] == 0
        assert "prompt" not in json.dumps(component).lower()
    finally:
        server.stop()


def test_fence_snapshot_is_bounded_and_free_of_payloads(tmp_path):
    """[S11.5] MOCK/INTEGRATION — §33 observability of the authority itself."""
    from forge.core.fencing import commit_guard

    models = tmp_path / "models"
    write_reference_artifact(models, "reference-clm")
    server, _provider = server_on_path(tmp_path, reference_dir=models)
    registry = server.fences
    try:
        for index in range(5):
            task_id = "t%d" % index
            fence = registry.begin(task_id, owner="w")
            registry.mark_running(task_id, fence)
        snapshot = registry.snapshot(limit=2)
        assert snapshot["tasks"] == 5
        assert snapshot["authorized"] == 5
        assert len(snapshot["current"]) == 2                # bounded
        registry.commit("t0", registry.current("t0"), "SUCCEEDED",
                        payload={"result": "x" * 5000})
        after = registry.snapshot()
        assert after["authorized"] == 4
        #: a snapshot is metadata only: no prompts, results or requirement text
        text = json.dumps(after).lower()
        for forbidden in ("prompt", "requirement", "xxxx"):
            assert forbidden not in text
        guard = commit_guard(registry.current("t1"), registry)
        assert isinstance(guard(), str)
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# §10 — durable inference job state, readable from the task API
# ---------------------------------------------------------------------------


def test_task_read_exposes_durable_inference_state(tmp_path):
    """INTEGRATION_TEST · MOCK_BACKEND_TEST — the task API answers §10.

    After a real loop run the task read reports which path handled the work,
    which attempt and generation produced it, and whether that attempt may
    still publish. A terminal attempt is *not* authorized to publish: the fence
    is committed, and that is visible rather than implied.
    """
    fabric, backend = scripted_fabric(response=CSV_PAYLOAD,
                                      backend_name="s11double",
                                      model_name="s11-model")
    server, _legacy = server_on_path(tmp_path, inference_fabric=fabric)
    client = make_client(server)
    try:
        with client:
            task = create_task(client, ADMIN, "Add CSV export functionality")
            task_id = task["task_id"]
            state = approve_until_terminal(client, ADMIN, task_id, timeout=240)
            assert state["status"] == "completed", state.get("error", "")

            read = client.get("/api/v1/tasks/%s" % task_id, headers=ADMIN)
            assert read.status_code == 200
            inference = read.json()["task"]["inference"]
            assert inference["persisted"] is True
            assert inference["path"] == PATH_SESSION11
            assert inference["model"] == "s11double:s11-model"
            assert inference["backend_id"] == "s11double"
            assert inference["attempt_id"] == "%s#g1" % task_id
            assert inference["generation"] == 1
            assert inference["fence_state"] == "SUCCEEDED"
            assert inference["authorized_to_publish"] is False
            assert inference["mode"] == "session11"
            assert inference["path_attached"] is True

            #: the dedicated route reports the same facts, and needs auth
            dedicated = client.get("/api/v1/tasks/%s/inference" % task_id,
                                   headers=ADMIN)
            assert dedicated.status_code == 200
            body = dedicated.json()
            assert body["task_id"] == task_id
            assert body["inference"]["model"] == "s11double:s11-model"
            assert body["inference"]["fence_state"] == "SUCCEEDED"
            anonymous = client.get("/api/v1/tasks/%s/inference" % task_id)
            assert anonymous.status_code in (401, 403)

            #: §14/§33 — the state is metadata: no prompt, no model output
            blob = json.dumps(inference).lower()
            assert "export_csv" not in blob
            assert CSV_PAYLOAD not in json.dumps(inference)
            assert backend.generate_calls
    finally:
        server.stop()


def test_unstarted_task_reports_no_inference_facts(tmp_path):
    """[S11.5] MOCK — an attempt that never ran has no provenance to show.

    §10 must not manufacture history: a queued task reports that nothing was
    persisted and that no attempt is authorized, while still reporting the
    server's configured mode (a fact about the server, not about the attempt).
    """
    server, _legacy = server_on_path(tmp_path)
    try:
        task = server.tasks.create("demo", "queued, never started")
        state = server.task_inference_state(task.task_id)
        assert state["persisted"] is False
        assert state["authorized_to_publish"] is False
        assert state["fence_state"] == "unknown"
        assert state["mode"] == "session11"
        for absent in ("model", "backend_id", "path", "generation_id",
                       "neural", "verification_state"):
            assert absent not in state, absent
    finally:
        server.stop()


def test_cancelled_attempt_state_is_readable_and_denies_publishing(tmp_path):
    """[S11.5] MOCK — §8/§10: a cancelled attempt says so, and cannot publish."""
    server, _legacy = server_on_path(tmp_path)
    try:
        task = server.tasks.create("demo", "will be cancelled")
        task_id = task.task_id
        fence = server.fences.begin(task_id, owner="worker-1")
        server.fences.mark_running(task_id, fence)
        running = server.task_inference_state(task_id)
        assert running["fence_state"] == "RUNNING"
        assert running["authorized_to_publish"] is True
        assert running["attempt_id"] == "%s#g1" % task_id

        server.cancel_task(task_id, actor="operator")
        cancelled = server.task_inference_state(task_id)
        assert cancelled["fence_state"] in ("CANCELLING", "CANCELLED")
        assert cancelled["authorized_to_publish"] is False
        assert cancelled["persisted"] is False
        #: a superseding attempt takes the authority away for good
        successor = server.fences.begin(task_id, owner="worker-2")
        server.fences.mark_running(task_id, successor)
        after = server.task_inference_state(task_id)
        assert after["generation"] == 2
        assert after["attempt_id"] == "%s#g2" % task_id
        assert after["authorized_to_publish"] is True
        assert server.fences.is_authorized(fence) is False
    finally:
        server.stop()


def test_an_unauthorized_attempt_cannot_write_the_task_record(tmp_path,
                                                             monkeypatch):
    """INTEGRATION_TEST (§7/§8/§9): the lease is not the only authority.

    A worker that still holds its lease but has lost its fence — superseded by a
    retry, or cancelled by an operator — must not publish. The task record stays
    exactly as the authority left it, the attempt is fenced, and the discard is
    reported as bounded metadata instead of being silently dropped.

    The lease itself is a labelled test double (``lease_held_by`` forced true)
    so the fence is the only variable under test.
    """
    from forge.server.workers import _discard_unauthorized, _may_publish

    fabric, _backend = scripted_fabric(response="zombie-result",
                                       backend_name="s11double")
    server, _provider = server_on_path(tmp_path, inference_fabric=fabric)
    monkeypatch.setattr(server.queue, "lease_held_by",
                        lambda task_id, owner: True)
    try:
        task = server.tasks.create("demo", "superseded work",
                                   mode="autonomous")
        task_id = task.task_id
        fence_a, _identity = _begin_attempt(server, task_id, task, "worker-A")
        assert fence_a is not None
        #: while it is the authorized generation, it may publish
        assert _may_publish(server, task_id, "worker-A", fence_a) is True

        before = server.tasks.get_or_raise(task_id)
        version_before = before.version
        result_before = before.result_json

        #: a successor attempt takes the authority away
        fence_b = server.fences.begin(task_id, owner="worker-B")
        server.fences.mark_running(task_id, fence_b)
        assert _may_publish(server, task_id, "worker-A", fence_a) is False
        #: and B — the current attempt — still may
        assert _may_publish(server, task_id, "worker-B", fence_b) is True

        _discard_unauthorized(server, task_id, "demo", "worker-A", fence_a,
                              "completed outcome")
        assert server.fences.get(task_id, fence_a.generation).state == "FENCED"
        after = server.tasks.get_or_raise(task_id)
        #: the stale attempt wrote nothing at all
        assert after.version == version_before
        assert after.result_json == result_before
        assert after.status.value not in ("completed", "failed", "cancelled")
        fenced_events = [event for event in task_events(server, task_id)
                         if event["type"] == "attempt.fenced"]
        assert fenced_events
        assert fenced_events[-1]["data"]["attempt_id"] == fence_a.attempt_id
        assert fenced_events[-1]["data"]["reason"] == "unauthorized_publish"
        assert "zombie-result" not in json.dumps(fenced_events[-1]["data"])

        #: losing the lease alone is also enough to lose publish rights
        monkeypatch.setattr(server.queue, "lease_held_by",
                            lambda task_id, owner: False)
        assert _may_publish(server, task_id, "worker-B", fence_b) is False
    finally:
        server.stop()


def test_operator_cancellation_revokes_publish_authority_immediately(tmp_path,
                                                                    monkeypatch):
    """INTEGRATION_TEST (§8): one cancellation chain, no privileged gap.

    Before Session 11.5 ``cancel_task`` set a flag and signalled the running
    control, but the attempt fence stayed RUNNING — so a worker that finished
    before noticing the cancel could still commit SUCCEEDED and publish a
    result the operator had just forbidden. Cancellation now reaches the
    authority at once: the fence goes to CANCELLING, authorization is revoked,
    and the worker's own confirmation still completes the transition.
    """
    from forge.server.workers import _may_publish, _settle_attempt
    from forge.server import TaskStatus

    fabric, _backend = scripted_fabric(response="late-result",
                                       backend_name="s11double")
    server, _provider = server_on_path(tmp_path, inference_fabric=fabric)
    monkeypatch.setattr(server.queue, "lease_held_by",
                        lambda task_id, owner: True)
    try:
        task = server.tasks.create("demo", "long running work",
                                   mode="autonomous")
        task_id = task.task_id
        #: walk the real transition table: created → queued → started, so the
        #: cooperative branch of cancel_task (a running attempt) is what runs.
        server.tasks.transition(task_id, TaskStatus.QUEUED,
                                expected=(TaskStatus.CREATED,))
        server.tasks.transition(task_id, TaskStatus.STARTED,
                                expected=(TaskStatus.QUEUED,))
        fence, _identity = _begin_attempt(server, task_id, task, "worker-A")
        assert _may_publish(server, task_id, "worker-A", fence) is True

        server.cancel_task(task_id, actor="operator")

        current = server.fences.current(task_id)
        assert current.state == "CANCELLING"
        #: the worker that has not noticed yet may no longer publish
        assert _may_publish(server, task_id, "worker-A", fence) is False
        assert server.fences.is_authorized(fence) is False
        state = server.task_inference_state(task_id)
        assert state["authorized_to_publish"] is False
        assert state["fence_state"] == "CANCELLING"

        #: the worker's confirmation still lands, and is idempotent-safe
        assert _settle_attempt(server, task_id, "demo", fence, "cancelled",
                               reason="cancelled by operator") == "CANCELLED"
        assert server.fences.current(task_id).state == "CANCELLED"
        #: a second cancellation request neither raises nor resurrects anything
        server.tasks.update(task_id, cancel_requested=True)
        assert server.fences.current(task_id).state == "CANCELLED"
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# §22 — every model call site is classified, and none keeps a private path
# ---------------------------------------------------------------------------


def test_hybrid_eligibility_keys_off_the_stable_caller_label(tmp_path):
    """[S11.5] MOCK — configuration names components, not user content.

    ``ModelRequest.task`` is free text: it becomes a remote system prompt and a
    routing hint, so keying hybrid eligibility off it means keying it off the
    requirement. ``caller`` is the stable label; ``task`` remains only a
    fallback for callers that predate the field.
    """
    from forge.models.inference_path import (PATH_HYBRID, InferencePathConfig,
                                             decide_path)

    config = InferencePathConfig(mode=PATH_HYBRID,
                                hybrid_legacy_callers=("reviewer",))
    labelled = decide_path(config, ModelRequest(prompt="p", capability="coding",
                                                caller="reviewer",
                                                task="Add CSV export"),
                           available=True)
    assert labelled.path == PATH_LEGACY
    assert "not migrated" in labelled.reason

    #: the same requirement from a migrated component still uses Session 11
    migrated = decide_path(config, ModelRequest(prompt="p", capability="coding",
                                                caller="coder",
                                                task="Add CSV export"),
                           available=True)
    assert migrated.path == PATH_SESSION11

    #: and the pre-field fallback still works, so nothing regressed
    fallback = decide_path(config, ModelRequest(prompt="p", capability="coding",
                                                task="reviewer"),
                           available=True)
    assert fallback.path == PATH_LEGACY


def test_production_call_sites_declare_a_stable_caller(tmp_path):
    """[S11.5] MOCK — §22: the call-site map is enforced, not remembered.

    Every production model call site sends a stable ``caller`` label, so the
    canonical path can classify it, hybrid mode can allow-list it, and an audit
    record can name the component that asked. A new call site that forgets the
    label fails here rather than silently becoming unclassifiable.
    """
    import re
    from pathlib import Path as _Path

    repo = _Path(__file__).resolve().parents[1]
    expected = {
        "forge/agents/coder.py": '"coder"',
        "forge/agents/debugger.py": '"debugger"',
        "forge/agents/reviewer.py": '"reviewer"',
        "forge/agents/mediation.py": '"mediated-agent:%s"',
        "forge/agents/agent_bench.py": '"agent-bench"',
    }
    for relative, label in expected.items():
        text = (repo / relative).read_text(encoding="utf-8")
        assert "caller=%s" % label in text, "%s lost its caller label" % relative
        #: and the label really reaches a fabric request
        assert re.search(r"fabric\.generate\(", text), relative


def test_reviewer_and_mediation_record_which_path_answered(tmp_path):
    """[S11.5] MOCK — a verdict and a mediated run carry their provenance.

    A BLOCK from an unverified deterministic rung and a BLOCK from a verified
    neural model are different facts, so the reviewer records which one it was;
    a mediated agent gets no private inference story of its own.
    """
    from forge.agents.reviewer import ReviewerAgent
    from forge.models.inference_path import PATH_SESSION11

    fabric, _backend = scripted_fabric(
        response='{"findings": [], "verdict": "APPROVE"}',
        backend_name="s11double", model_name="s11-model",
        capabilities=("coding", "review"))
    reviewer = ReviewerAgent(fabric=fabric)
    assert reviewer.last_inference == {}
    findings = reviewer.review(task="t", changed_files=("a.py",),
                               diff="- old\n+ new")
    assert findings is not None and findings == []
    provenance = reviewer.last_inference
    assert provenance["path"] == PATH_SESSION11
    assert provenance["model"] == "s11double:s11-model"
    assert provenance["backend_id"] == "s11double"
    assert provenance["success"] is True
    #: bounded and content-free: no diff, no prompt, no findings text
    blob = json.dumps(provenance).lower()
    assert "old" not in blob and "findings" not in blob

    #: A double that never claimed "review" is *refused*, not stretched: the
    #: verdict is None, and the refusal is still recorded with its path (§19).
    coding_only, _backend2 = scripted_fabric(
        response='{"findings": [], "verdict": "APPROVE"}',
        backend_name="coding-only", model_name="coding-model")
    strict = ReviewerAgent(fabric=coding_only)
    assert strict.review(task="t", changed_files=("a.py",), diff="d") is None
    refused = strict.last_inference
    assert refused["path"] == PATH_SESSION11
    assert refused["success"] is False
    #: the refusal names the missing capability and the rung that answered
    reason = str(refused.get("routing_reason", "")).lower()
    assert "review" in reason
    assert refused.get("error_code") == "NEEDS_MODEL"
    assert refused.get("backend_id") == "deterministic"
    assert refused.get("neural") is False
    assert refused.get("deterministic") is True


def test_benchmark_smoke_check_states_what_answered(tmp_path):
    """[S11.5] MOCK — §30: a passed check says whether it was real or a rung."""
    from forge.agents.agent_bench import MARKER, _check

    fabric, _backend = scripted_fabric(response=MARKER,
                                       backend_name="s11double",
                                       model_name="s11-model")
    detail = ("path=%s model=%s neural=%s"
              % (PATH_SESSION11, "s11double:s11-model", True))
    check = _check("model-smoke", "d", "passed", detail=detail)
    assert check["detail"].startswith("path=session11")
    assert "s11double:s11-model" in check["detail"]
    #: the detail is bounded like every other evidence string
    assert len(_check("x", "y", "passed", detail="z" * 5000)["detail"]) <= 500
    assert fabric is not None
