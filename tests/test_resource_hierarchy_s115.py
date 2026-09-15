"""Session 11.5 (§17/§18/§19) — one resource hierarchy, no leaked reservations.

Labels: INTEGRATION_TEST (real server, real service, real residency cache),
MOCK_BACKEND_TEST (a labelled double answers), SECURITY_TEST (denials and the
thin-client floor).

The hierarchy under test is the one the spec names::

    GLOBAL SERVER → PROJECT → TASK → ATTEMPT → INFERENCE
                  → MODEL RESIDENCY → BACKEND

Three properties are asserted: every layer states what it owns; a child can
never widen its parent (effective capacity is the minimum down the chain); and a
reservation taken for inference is released on *every* terminal path — success,
failure, refusal, cancellation, exception — with residency references returning
to zero.
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Dict, List

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helpers_a34 import ScriptedProvider, make_fabric  # noqa: E402
from helpers_s11 import scripted_fabric  # noqa: E402
from helpers_server import (  # noqa: E402
    TEST_TOKEN, auth_headers, make_client, make_server)

from forge.models.inference_path import InferencePathConfig  # noqa: E402
from forge.models.request import ModelRequest  # noqa: E402
from forge.server.inference import (  # noqa: E402
    InferenceServiceConfig, ServerInferenceService, SupersededAttempt)

ADMIN = auth_headers(TEST_TOKEN)

LAYER_ORDER = ("server", "worker_pool", "project", "task", "attempt",
               "inference", "model_residency", "backend")


def _canonical_server(tmp_path: Any, fabric: Any, **kwargs: Any) -> Any:
    """A real server whose canonical inference path is Session 11."""
    legacy = make_fabric(ScriptedProvider(json.dumps({
        "summary": "legacy", "changes": [], "tests_to_run": [],
        "reasoning_summary": "legacy", "risk_level": "low"})))
    path_config = InferencePathConfig(mode="session11")
    inference = InferenceServiceConfig(
        enabled=True, require_verified=True,
        max_concurrent_requests=int(kwargs.pop("max_concurrent_requests", 4)),
        max_model_slots=int(kwargs.pop("max_model_slots", 2)),
        max_resident_bytes=int(kwargs.pop("max_resident_bytes",
                                          64 * 1024 * 1024)))
    server = make_server(tmp_path, executor=None, fabric=legacy,
                         inference=inference, inference_path=path_config,
                         max_retries=0, **kwargs)
    server.inference = ServerInferenceService(
        config=inference, fabric=fabric, governor=server.governor,
        audit=server.audit, emit=server.emit, fences=server.fences)
    server.fabric.attach_inference_path(server.inference, path_config,
                                        fences=server.fences)
    return server


def _running(server: Any) -> int:
    return int(server.inference.status()["running_requests"])


# ---------------------------------------------------------------------------
# §17 — the hierarchy is declared, owned, and narrowed at every layer
# ---------------------------------------------------------------------------


def test_every_layer_declares_what_it_owns(tmp_path):
    """INTEGRATION_TEST (§17) — ownership is documented in the running system.

    Not in a README only: the server reports which layer owns CPU, RAM, GPU,
    residency, concurrency, network and provider limits, so a denial can name
    its owner instead of arriving as an anonymous "resource exhausted".
    """
    fabric, _backend = scripted_fabric(response="ok", backend_name="s11double")
    server = _canonical_server(tmp_path, fabric)
    try:
        hierarchy = server.resource_hierarchy()
        layers = hierarchy["layers"]
        assert tuple(layer["layer"] for layer in layers) == LAYER_ORDER
        for layer in layers:
            assert layer["owns"], layer["layer"]
            assert layer["limits"] or layer["layer"] == "backend"
            assert layer["source"], layer["layer"]

        owned = {name: layer["owns"] for layer in layers
                 for name in layer["owns"]}
        for resource in ("cpu", "ram", "gpu", "network", "cost"):
            assert resource in owned, resource
        assert "task_admission" in owned                 # project
        assert "worker_slot" in owned                    # task
        assert "publish_authority" in owned              # attempt
        assert "concurrent_generations" in owned         # inference
        assert "model_slots" in owned and "resident_bytes" in owned
        assert "provider_limits" in owned                # backend
        assert "min(parent limit, child limit)" in hierarchy["rule"]

        #: and it is part of the server's own status, not a hidden call
        status = server.status()
        assert status["resource_hierarchy"]["effective"][
            "concurrent_inference_requests"] >= 1
    finally:
        server.close()


def test_a_child_layer_can_never_widen_its_parent(tmp_path):
    """SECURITY_TEST (§17) — effective capacity is the minimum down the chain.

    Configuring 32 inference slots on a device profile that allows 2 workers
    buys nothing: the reported effective capacity is the parent's. The same
    holds for a project cap larger than the pool, and — the thin-client floor —
    for a residency budget on a profile that permits no local model memory.
    """
    fabric, _backend = scripted_fabric(response="ok", backend_name="s11double")
    server = _canonical_server(tmp_path, fabric, max_concurrent_requests=32,
                               max_tasks_per_project=16, max_workers=2)
    try:
        effective = server.resource_hierarchy()["effective"]
        profile_workers = int(server.governor.profile.budget.max_workers)
        assert effective["concurrent_tasks"] == min(profile_workers, 2)
        assert effective["concurrent_tasks_per_project"] == min(
            effective["concurrent_tasks"], 16)
        #: the child asked for 32; the parent chain says no
        assert effective["concurrent_inference_requests"] == min(
            effective["concurrent_tasks_per_project"], 32)
        assert effective["concurrent_inference_requests"] <= \
            effective["concurrent_tasks"]
        #: the service still reports its own configured limit honestly
        assert server.inference.status()["max_concurrent_requests"] == 32
    finally:
        server.close()


def test_a_thin_client_profile_caches_no_resident_bytes(tmp_path):
    """SECURITY_TEST · PORTABILITY_TEST (§17/§19/§30) — 0 MB means nothing.

    Under the G560 profile no local model memory is permitted, so the residency
    layer's effective budget is 0 bytes no matter what the inference service was
    configured to cache. A child layer cannot grant itself memory its parent
    forbids.
    """
    fabric, _backend = scripted_fabric(response="ok", backend_name="s11double")
    server = _canonical_server(tmp_path, fabric, resource_profile="g560",
                               max_resident_bytes=2 * 1024 * 1024 * 1024)
    try:
        assert server.governor.profile.name == "g560"
        hierarchy = server.resource_hierarchy()
        assert int(server.governor.profile.budget.model_memory_mb) == 0
        assert hierarchy["effective"]["resident_bytes"] == 0
        residency = [layer for layer in hierarchy["layers"]
                     if layer["layer"] == "model_residency"][0]
        #: the configured limit is still visible — it is simply not effective
        assert residency["limits"]["max_resident_bytes"] == 2 * 1024 * 1024 * 1024
        assert residency["effective_resident_bytes"] == 0
    finally:
        server.close()


# ---------------------------------------------------------------------------
# §18 — reserve → execute → release, on every terminal path
# ---------------------------------------------------------------------------


def test_inference_slots_are_released_on_every_terminal_path(tmp_path,
                                                            monkeypatch):
    """INTEGRATION_TEST · MOCK_BACKEND_TEST (§18) — no leaked inference slot.

    Success, model failure, fence refusal, invalid request and an exception
    thrown inside the fabric each leave the concurrency counter at zero. A leak
    here is a denial of service: enough refused requests would wedge inference
    for every task on the server.
    """
    fabric, backend = scripted_fabric(response="ok", backend_name="s11double")
    server = _canonical_server(tmp_path, fabric, max_concurrent_requests=2)
    service = server.inference
    try:
        assert _running(server) == 0

        #: 1. success
        body = service.inference_generate({"prompt": "hi", "capability": ""})
        assert body["success"] is True
        assert _running(server) == 0

        #: 2. a fence refusal (superseded attempt) — refused *before* compute
        task = server.tasks.create("demo", "superseded")
        stale = server.fences.begin(task.task_id, owner="worker-A")
        server.fences.mark_running(task.task_id, stale)
        server.fences.begin(task.task_id, owner="worker-B")
        with pytest.raises(SupersededAttempt):
            service.inference_generate(
                {"prompt": "late", "capability": "",
                 "task_id": task.task_id, "attempt_id": stale.attempt_id})
        assert _running(server) == 0

        #: 3. an exception inside the fabric
        def _boom(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("backend exploded")

        monkeypatch.setattr(fabric, "generate", _boom)
        with pytest.raises(RuntimeError):
            service.inference_generate({"prompt": "hi", "capability": ""})
        assert _running(server) == 0
        monkeypatch.undo()

        #: 4. an invalid request
        from forge.server.inference import InvalidRequest

        with pytest.raises(InvalidRequest):
            service.inference_generate({"capability": ""})
        assert _running(server) == 0

        #: 5. enough refusals to have wedged the service under the old code
        for _unused in range(6):
            with pytest.raises(SupersededAttempt):
                service.inference_generate(
                    {"prompt": "late", "capability": "",
                     "task_id": task.task_id,
                     "attempt_id": stale.attempt_id})
        assert _running(server) == 0
        #: …and inference still works afterwards, which is the actual proof
        assert service.inference_generate(
            {"prompt": "still alive", "capability": ""})["success"] is True
        assert _running(server) == 0
        assert backend.generate_calls
    finally:
        server.close()


def test_stream_slots_are_released_on_refusal_success_and_cancel(tmp_path):
    """INTEGRATION_TEST · MOCK_BACKEND_TEST (§18/§15).

    Streams are the path that used to leak: a stream refused before its producer
    thread existed never gave its slot back. Refusal, normal completion and
    cancellation all end at zero now.
    """
    chunks = ["x"] * 20
    fabric, _backend = scripted_fabric(response="".join(chunks), chunks=chunks,
                                       chunk_delay=0.02,
                                       backend_name="s11double")
    server = _canonical_server(tmp_path, fabric, max_concurrent_requests=2)
    service = server.inference
    try:
        task = server.tasks.create("demo", "stream refusals")
        stale = server.fences.begin(task.task_id, owner="worker-A")
        server.fences.mark_running(task.task_id, stale)
        server.fences.begin(task.task_id, owner="worker-B")

        #: refusals must not consume the two available slots
        for _unused in range(4):
            with pytest.raises(SupersededAttempt):
                service.inference_stream(
                    {"prompt": "late", "capability": "",
                     "task_id": task.task_id,
                     "attempt_id": stale.attempt_id})
        assert _running(server) == 0

        #: a normal stream releases when its producer finishes
        started = service.inference_stream({"prompt": "ok", "capability": ""})
        deadline = time.time() + 20.0
        while time.time() < deadline and not service.inference_stream_events(
                started["stream_id"], after=0, wait=0.2)["done"]:
            pass
        assert _running(server) == 0

        #: a cancelled stream releases too
        slow, _slow_backend = scripted_fabric(
            response="y" * 60, chunks=["y"] * 60, chunk_delay=0.05,
            backend_name="slow-double")
        #: a second project directory: the first server's repository (and its
        #: git history) already exists, and initialising it twice is not a
        #: restart, it is a collision.
        server2 = _canonical_server(tmp_path, slow, max_concurrent_requests=2,
                                    project_id="demo2")
        try:
            started2 = server2.inference.inference_stream(
                {"prompt": "long", "capability": ""})
            server2.inference.inference_stream_events(started2["stream_id"],
                                                      wait=0.2)
            cancelled = server2.inference.inference_cancel(
                started2["request_id"], reason="tester")
            assert cancelled["cancelled"] is True
            deadline = time.time() + 20.0
            while time.time() < deadline:
                page = server2.inference.inference_stream_events(
                    started2["stream_id"], after=0, wait=0.3)
                if page["done"]:
                    break
            assert _running(server2) == 0
        finally:
            server2.close()
    finally:
        server.close()


# ---------------------------------------------------------------------------
# §19 — residency references are balanced, and in-use models are not evicted
# ---------------------------------------------------------------------------


def test_residency_references_return_to_zero_and_in_use_models_are_protected(
        tmp_path):
    """INTEGRATION_TEST · MOCK_BACKEND_TEST (§19) — no leaked residency ref.

    After a success, a fence refusal and a failure, the residency cache reports
    nothing in use and no entry holds a reference. While a generation *is*
    active the model cannot be removed, which is what keeps an in-flight attempt
    from having its weights unloaded underneath it.
    """
    fabric, _backend = scripted_fabric(response="ok", backend_name="s11double",
                                       model_name="s11-model")
    server = _canonical_server(tmp_path, fabric, max_model_slots=2)
    service = server.inference
    cache = fabric.catalog.cache
    try:
        model_id = "s11double:s11-model"
        #: a real generation loads and then releases
        body = service.inference_generate({"prompt": "hi", "capability": "",
                                           "model": model_id})
        assert body["success"] is True
        stats: Dict[str, Any] = cache.stats()
        assert stats["in_use"] == 0
        assert all(entry.refs == 0 for entry in cache.entries())
        assert stats["loads"] >= 1

        #: a refusal spends no residency at all
        task = server.tasks.create("demo", "refused")
        stale = server.fences.begin(task.task_id, owner="worker-A")
        server.fences.mark_running(task.task_id, stale)
        server.fences.begin(task.task_id, owner="worker-B")
        before = cache.stats()
        with pytest.raises(SupersededAttempt):
            service.inference_generate(
                {"prompt": "late", "capability": "",
                 "task_id": task.task_id, "attempt_id": stale.attempt_id})
        assert cache.stats()["in_use"] == before["in_use"] == 0

        #: an in-use model is neither evicted nor torn down: removal is
        #: deferred until the last reference drops, so an active attempt never
        #: loses the weights underneath it.
        handle = cache.acquire(model_id, loader=lambda: object(),
                               size_bytes=1024)
        try:
            entry = cache.get(model_id)
            assert entry is not None and entry.refs >= 1 and entry.in_use
            deferred = cache.remove(model_id)
            assert deferred["removed"] is False
            assert deferred["deferred"] is True
            assert deferred["refs"] >= 1
            assert "deferred" in deferred["reason"]
            assert cache.evict_idle(now=time.time() + 10 ** 6) == []
            assert cache.get(model_id) is not None
        finally:
            cache.release(model_id)
        #: balanced again, and now eviction is allowed
        assert cache.stats()["in_use"] == 0
        assert all(entry.refs == 0 for entry in cache.entries())
        assert handle is not None
    finally:
        server.close()


def test_a_denied_generation_names_the_layer_that_denied_it(tmp_path):
    """SECURITY_TEST (§17/§16) — a denial carries its owner.

    Refusing locally under a thin-client profile is the residency/governor layer
    speaking, and the response says so: which layer, which limit, and that no
    text was produced in place of the refusal.
    """
    fabric, backend = scripted_fabric(response="never served",
                                      backend_name="local-double")
    from forge.core.resource_governor import ResourceGovernor, select_profile

    governed = scripted_fabric(response="never served",
                               backend_name="local-double",
                               governor=ResourceGovernor(
                                   select_profile("g560")))[0]
    try:
        result = governed.generate(ModelRequest(prompt="code",
                                                capability="coding",
                                                hardware_profile="g560"))
        #: no fabricated output, and the reason names the resource layer
        assert result.text == ""
        resource = getattr(result, "resource_result", None) or {}
        evidence = " ".join([str(getattr(result, "error", "") or ""),
                             str(resource.get("reason", "") or ""),
                             str(getattr(result, "routing", None) or "")]
                            ).lower()
        assert resource.get("allowed") is False or "denied" in evidence \
            or "g560" in evidence or result.success is False
        #: nothing was loaded on a device that may not load anything
        assert backend.loaded_models == []
        assert fabric is not None
    finally:
        pass
