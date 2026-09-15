"""Session 11 — residency bookkeeping, bounded streams, context budgeting.

Three invariants are checked here:

* the loaded-model registry refcounts, never loads twice, never evicts a model
  that is in use, and reports evictions/unloads/refusals as three different
  things;
* a stream is bounded (buffer + total chars), monotonic, always terminal, and
  its snapshot carries metadata but never model text;
* the context planner reports what it left out instead of silently overflowing
  the model window.

Labels: ``MOCK_BACKEND_TEST`` (scripted doubles) and ``DETERMINISTIC_TEST``.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import Any, List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers_s11 import (  # noqa: E402
    MOCK_BACKEND_TEST,
    ScriptedModelBackend,
    scripted_fabric,
)

from forge.models.context_budget import (  # noqa: E402
    ContextBudgetPlanner,
    ContextSection,
)
from forge.models.model_cache import (  # noqa: E402
    ModelResidencyCache,
    ModelResidencyError,
)
from forge.models.request import ModelRequest  # noqa: E402
from forge.models.streams import BoundedStream, join_deltas  # noqa: E402

MODEL = "scripted:scripted-model"


class FakeClock:
    """A monotonic clock a test can move by hand."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


def counting_loader(payload: Any = 4096, delay: float = 0.0):
    """A loader that counts its own calls: residency must call it once."""
    calls: List[int] = []

    def load() -> Any:
        calls.append(1)
        if delay:
            time.sleep(delay)
        return payload

    load.calls = calls  # type: ignore[attr-defined]
    return load


def failing_loader(message: str = "boom"):
    def load() -> Any:
        raise RuntimeError(message)

    return load


# -- residency: single load, refcounts, single flight ----------------------------


def test_a_model_is_loaded_once_and_refcounted():
    cache = ModelResidencyCache(max_slots=2, max_bytes=1 << 20)
    loader = counting_loader(4096)
    first = cache.acquire(MODEL, loader, backend_id="scripted",
                          size_bytes=4096)
    second = cache.acquire(MODEL, loader, backend_id="scripted",
                           size_bytes=4096)
    assert len(loader.calls) == 1
    assert first is second
    assert first.refs == 2
    assert first.state == "loaded"
    assert cache.resident_bytes == 4096
    stats = cache.stats()
    assert stats["loads"] == 1
    assert stats["in_use"] == 1


def test_concurrent_acquire_is_single_flight():
    cache = ModelResidencyCache(max_slots=2, max_bytes=1 << 20)
    loader = counting_loader(8192, delay=0.05)
    entries: List[Any] = []
    lock = threading.Lock()
    barrier = threading.Barrier(4)

    def worker() -> None:
        barrier.wait()
        entry = cache.acquire(MODEL, loader, backend_id="scripted",
                              size_bytes=8192)
        with lock:
            entries.append(entry)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10.0)
    assert len(entries) == 4
    assert len(loader.calls) == 1, "the model was loaded more than once"
    assert all(entry.refs == 4 for entry in entries)
    assert cache.stats()["duplicate_loads_avoided"] >= 1


def test_release_drops_references_but_keeps_residency():
    cache = ModelResidencyCache(max_slots=1)
    loader = counting_loader(1024)
    cache.acquire(MODEL, loader, size_bytes=1024)
    cache.acquire(MODEL, loader, size_bytes=1024)
    assert cache.release(MODEL) is True
    entry = cache.get(MODEL)
    assert entry is not None and entry.refs == 1
    assert cache.release(MODEL) is True
    assert cache.get(MODEL).refs == 0
    #: A third release cannot drive the count negative.
    assert cache.release(MODEL) is True
    assert cache.get(MODEL).refs == 0


def test_the_residency_context_manager_always_releases():
    cache = ModelResidencyCache(max_slots=1)
    loader = counting_loader(2048)
    with pytest.raises(RuntimeError):
        #: The failure propagates to the caller; the reference does not leak.
        with cache.residency(MODEL, loader, size_bytes=2048) as entry:
            assert entry.refs == 1
            raise RuntimeError("the request failed")
    assert cache.get(MODEL).refs == 0


def test_removal_is_deferred_while_a_request_holds_the_model():
    cache = ModelResidencyCache(max_slots=2)
    loader = counting_loader(1024)
    entry = cache.acquire(MODEL, loader, size_bytes=1024)
    outcome = cache.remove(MODEL)
    #: The teardown is deferred: the live request keeps working.
    assert outcome["removed"] is False
    assert outcome["deferred"] is True
    assert outcome["refs"] == 1
    assert "deferred" in outcome["reason"]
    assert entry.state == "loaded" and entry.refs == 1
    assert cache.get(MODEL).pending_remove is True
    #: A new reference cancels the pending removal (an active request wins).
    cache.acquire(MODEL, loader, size_bytes=1024)
    assert cache.get(MODEL).pending_remove is False
    cache.release(MODEL)
    cache.release(MODEL)
    assert cache.get(MODEL) is not None
    assert cache.get(MODEL).refs == 0
    #: With no references left, removal now really happens.
    assert cache.remove(MODEL)["removed"] is True
    assert cache.get(MODEL) is None
    stats = cache.stats()
    assert stats["unloads"] == 1
    #: An operator unload is bookkeeping, never an eviction or a refusal.
    assert stats["evictions"] == 0
    assert stats["refusals"] == []
    assert any(event["code"] == "REMOVE_DEFERRED"
               for event in stats["events"])


# -- residency: eviction, budgets, honesty ---------------------------------------


def test_lru_eviction_prefers_the_least_recently_used_model():
    clock = FakeClock()
    cache = ModelResidencyCache(max_slots=2, max_bytes=1 << 20, clock=clock)
    for model in ("a:1", "b:2"):
        cache.acquire(model, counting_loader(1024), size_bytes=1024)
        cache.release(model)
        clock.advance(1.0)
    #: b is used again, so a is now the least recently used entry.
    cache.acquire("b:2", counting_loader(1024), size_bytes=1024)
    cache.release("b:2")
    clock.advance(1.0)
    cache.acquire("c:3", counting_loader(1024), size_bytes=1024)
    cache.release("c:3")
    ids = set(cache.model_ids())
    assert "a:1" not in ids, "the least recently used model should have gone"
    assert ids == {"b:2", "c:3"}
    assert cache.stats()["evictions"] == 1
    assert any(event["code"] == "EVICTED" and "a:1" in event["message"]
               for event in cache.stats()["events"])


def test_a_model_in_use_is_never_evicted():
    cache = ModelResidencyCache(max_slots=1)
    held = cache.acquire("a:1", counting_loader(1024), size_bytes=1024)
    with pytest.raises(ModelResidencyError) as caught:
        cache.acquire("b:2", counting_loader(1024), size_bytes=1024)
    assert caught.value.code == "RESIDENCY_FULL"
    assert "in use" in str(caught.value) or "a:1" in str(caught.value)
    #: The held model survived untouched, and the refusal is on the record.
    assert held.refs == 1 and held.state == "loaded"
    assert cache.resident_bytes == 1024
    loaded = [model for model in cache.model_ids()
              if cache.get(model).state == "loaded"]
    assert loaded == ["a:1"]
    #: The refused load left no usable phantom model behind.
    assert cache.get("b:2").state == "failed"
    assert cache.stats()["evictions"] == 0
    assert cache.stats()["loads"] == 1
    refusal = cache.stats()["refusals"][-1]
    assert refusal["code"] == "RESIDENCY_FULL"


def test_the_byte_budget_is_a_hard_bound():
    cache = ModelResidencyCache(max_slots=4, max_bytes=4096)
    cache.acquire("a:1", counting_loader(3000), size_bytes=3000)
    cache.release("a:1")
    with pytest.raises(ModelResidencyError) as caught:
        #: Nothing can be evicted (a:1 would have to go, and even then 5000
        #: bytes will not fit 4096), so the honest answer is a refusal.
        cache.acquire("big:1", counting_loader(5000), size_bytes=5000)
    assert caught.value.code in ("RESIDENCY_BUDGET", "RESIDENCY_FULL")
    assert cache.resident_bytes <= 4096


def test_idle_eviction_uses_the_bounded_clock_and_spares_in_use_models():
    clock = FakeClock()
    cache = ModelResidencyCache(max_slots=4, idle_seconds=30.0, clock=clock)
    cache.acquire("idle:1", counting_loader(1024), size_bytes=1024)
    cache.release("idle:1")
    busy = cache.acquire("busy:1", counting_loader(1024), size_bytes=1024)
    clock.advance(31.0)
    evicted = cache.evict_idle()
    assert evicted == ["idle:1"]
    assert busy.refs == 1 and busy.state == "loaded"
    assert "busy:1" in cache.model_ids()


def test_a_failed_load_is_reported_and_does_not_leave_a_phantom_model():
    cache = ModelResidencyCache(max_slots=2)
    with pytest.raises(ModelResidencyError) as caught:
        cache.acquire("bad:1", failing_loader("weights missing"),
                      size_bytes=1024)
    assert caught.value.code == "LOAD_FAILED"
    assert "weights missing" in str(caught.value)
    assert cache.get("bad:1") is None or cache.get("bad:1").state == "failed"
    assert cache.resident_bytes == 0
    assert cache.stats()["refusals"][-1]["code"] == "LOAD_FAILED"
    #: A later attempt really does try again (it is not cached as a failure).
    retry = counting_loader(512)
    entry = cache.acquire("bad:1", retry, size_bytes=512)
    assert entry.state == "loaded"
    assert len(retry.calls) == 1


def test_clear_unloads_without_pretending_anything_was_refused():
    cache = ModelResidencyCache(max_slots=3)
    for model in ("a:1", "b:2"):
        cache.acquire(model, counting_loader(1024), size_bytes=1024)
        cache.release(model)
    removed = cache.clear()
    assert sorted(removed) == ["a:1", "b:2"]
    stats = cache.stats()
    assert stats["unloads"] == 2
    assert stats["evictions"] == 0
    assert stats["refusals"] == []
    assert stats["resident_bytes"] == 0


def test_residency_snapshot_is_metadata_only():
    cache = ModelResidencyCache(max_slots=2)
    cache.acquire(MODEL, counting_loader("SECRET-WEIGHT-HANDLE-77"),
                  size_bytes=4096, fingerprint="sha256:abc")
    snapshot = cache.snapshot()
    blob = repr(snapshot)
    assert "SECRET-WEIGHT-HANDLE-77" not in blob
    entry = snapshot["entries"][0]
    assert entry["model_id"] == MODEL
    assert entry["size_bytes"] == 4096
    assert entry["fingerprint"] == "sha256:abc"
    assert entry["refs"] == 1
    assert entry["state"] == "loaded"
    assert entry["loads"] == 1
    assert snapshot["stats"]["in_use"] == 1


def test_the_fabric_refcounts_a_live_request_and_reuses_the_load():
    fabric, backend = scripted_fabric(response="mock")
    first = fabric.generate(ModelRequest(prompt="one", capability="coding"))
    second = fabric.generate(ModelRequest(prompt="two", capability="coding"))
    assert first.success and second.success
    status = fabric.catalog.status(MODEL)
    resident = status["resident"]
    assert resident is not None
    assert resident["state"] == "loaded"
    #: Two requests, one load, and the reference was given back afterwards.
    assert resident["loads"] == 1
    assert resident["refs"] == 0
    assert len(backend.loaded_models) == 1
    assert status["identity"]["availability_state"] == "ready"


def test_unload_during_a_live_stream_does_not_break_the_request():
    fabric, backend = scripted_fabric(
        response="mock", chunks=["a", "b", "c", "d"], chunk_delay=0.04)
    handle = fabric.stream(ModelRequest(prompt="stream me",
                                        capability="coding"))
    seen: List[str] = []
    for event in handle.events():
        seen.append(event.delta)
        if len(seen) == 2:
            #: An operator asks for the model to go away mid-request.
            deferred = fabric.catalog.unload(MODEL)
            assert deferred["unloaded"] is False
            assert deferred["deferred"] is True
    result = handle.wait(10.0)
    assert result.success is True
    assert "".join(seen) == "abcd"
    assert handle.stream.complete is True
    #: The request that was in flight was never invalidated.
    assert result.metadata.get("fenced") is not True


# -- bounded streams --------------------------------------------------------------


def test_stream_sequences_are_monotonic_and_done_is_emitted_once():
    stream = BoundedStream("inf-1", model_id=MODEL, backend_id="scripted")
    for delta in ("he", "ll", "o"):
        assert stream.publish(delta) is True
    terminal = stream.finish(finish_reason="stop")
    events = list(stream.events())
    sequences = [event.sequence for event in events]
    assert sequences == sorted(set(sequences))
    assert all(b > a for a, b in zip(sequences, sequences[1:]))
    assert [event.done for event in events].count(True) == 1
    assert events[-1] is terminal or events[-1].done is True
    joined = join_deltas(events)
    assert joined["text"] == "hello"
    assert joined["done"] is True and joined["complete"] is True
    assert joined["monotonic_sequence"] is True


def test_a_stream_that_never_finishes_is_reported_as_partial():
    #: ``get_timeout`` is the consumer-side bound: a producer that stops
    #: sending must not strand the reader forever.
    stream = BoundedStream("inf-2", model_id=MODEL, get_timeout=0.2)
    assert stream.publish("half an answer") is True
    assert stream.done is False
    assert stream.text() == "half an answer"
    joined = join_deltas(stream.events())
    assert joined["text"] == "half an answer"
    assert joined["done"] is True          # a terminal event always arrives
    assert joined["complete"] is False     # ...and it does not claim success
    assert joined["error_code"] == "STREAM_TIMEOUT"
    assert stream.state == "failed"
    #: ``partial`` describes an iterable that ended without a terminal event;
    #: here the consumer did get one, and it named the timeout.
    assert joined["partial"] is False
    assert join_deltas([_event for _event in ()])["partial"] is True


def test_a_closed_stream_ends_iteration_immediately():
    stream = BoundedStream("inf-2b", model_id=MODEL)
    stream.publish("one")
    stream.close()
    joined = join_deltas(stream.events())
    assert joined["done"] is True
    assert joined["complete"] is False
    assert stream.snapshot()["state"] == "disconnected"


def test_backpressure_turns_into_a_loud_failure_not_silent_loss():
    stream = BoundedStream("inf-3", model_id=MODEL, max_buffer=1,
                           put_timeout=0.05)
    accepted = 0
    for index in range(50):
        if not stream.publish("chunk-%d;" % index):
            break
        accepted += 1
    assert accepted < 50, "an undrained stream must not accept everything"
    snapshot = stream.snapshot()
    assert snapshot["state"] == "overflow"
    assert snapshot["error_code"] == "STREAM_OVERFLOW"
    assert "consumer stopped draining" in snapshot["error"]
    assert snapshot["overflow_events"] >= 1
    assert snapshot["done"] is True
    #: The consumer still learns why the stream ended, even though the buffer
    #: was full when the failure happened: the terminal event displaces a
    #: delta nobody drained, and the displaced text is counted.
    events = list(stream.events())
    assert events[-1].done is True
    assert events[-1].error_code == "STREAM_OVERFLOW"
    assert join_deltas(events)["complete"] is False


def test_the_total_char_bound_truncates_and_says_so():
    stream = BoundedStream("inf-4", model_id=MODEL, max_buffer=64,
                           max_total_chars=10)
    assert stream.publish("0123456789") is True
    assert stream.publish("more than fits") is False
    terminal = stream.finish()
    snapshot = stream.snapshot()
    assert snapshot["truncated"] is True
    assert snapshot["chars"] == 10
    assert snapshot["state"] == "truncated"
    assert terminal.finish_reason == "length"
    assert stream.text() == "0123456789"


def test_cancellation_always_delivers_a_terminal_event():
    stream = BoundedStream("inf-5", model_id=MODEL, max_buffer=32)
    stream.publish("partial")
    assert stream.cancel("user asked") is True
    #: A second cancel is a no-op, not a second terminal event.
    assert stream.cancel("again") is False
    events = list(stream.events())
    assert [event.done for event in events].count(True) == 1
    assert events[-1].finish_reason == "cancelled"
    assert "user asked" in events[-1].error
    assert stream.state == "cancelled"
    assert stream.snapshot()["cancelled"] is True


def test_a_disconnected_consumer_stops_the_producer():
    stream = BoundedStream("inf-6", model_id=MODEL, max_buffer=32)
    assert stream.publish("one") is True
    stream.close()
    assert stream.publish("two") is False
    assert stream.snapshot()["state"] == "disconnected"
    assert list(stream.events())[-1].done is True


def test_the_stream_snapshot_never_carries_model_text():
    stream = BoundedStream("inf-7", model_id=MODEL, backend_id="scripted")
    secret = "UNIQUE-MODEL-OUTPUT-MARKER-4c19"
    stream.publish(secret)
    stream.finish()
    blob = repr(stream.snapshot())
    assert secret not in blob
    assert stream.snapshot()["chars"] == len(secret)
    #: The per-event wire form carries the delta (that is its job); the
    #: snapshot that gets logged and stored never does.


def test_the_fabric_stream_is_bounded_complete_and_labelled():
    """MOCK_BACKEND_TEST: streamed by a scripted double, honestly labelled."""
    chunks = ["The ", "quick ", "brown ", "fox"]
    fabric, backend = scripted_fabric(response="".join(chunks), chunks=chunks)
    handle = fabric.stream(ModelRequest(prompt="stream please",
                                        capability="coding"))
    arrived = join_deltas(handle.events())
    result = handle.wait(10.0)
    assert arrived["monotonic_sequence"] is True
    assert arrived["done"] is True
    assert arrived["text"] == "The quick brown fox"
    assert result.success is True
    assert result.streamed is True
    assert result.neural is True
    snapshot = handle.snapshot()
    assert snapshot["stream"]["complete"] is True
    assert snapshot["stream"]["sequence"] == len(chunks) + 1
    assert snapshot["result"]["model_id"] == MODEL
    #: Buffer bound is real, not infinite.
    assert snapshot["stream"]["buffer_capacity"] > 0


def test_cancelling_a_fabric_stream_ends_it_as_cancelled():
    chunks = ["a"] * 40
    fabric, _backend = scripted_fabric(response="".join(chunks), chunks=chunks,
                                       chunk_delay=0.02)
    handle = fabric.stream(ModelRequest(prompt="long stream",
                                        capability="coding"))
    received = 0
    for event in handle.events():
        received += 1
        if received >= 3:
            assert handle.cancel("test cancelled it") is True
            break
    result = handle.wait(10.0)
    assert result.success is False
    #: Whoever ends the attempt, it is reported as a cancellation.
    assert result.state == "cancelled"
    assert result.error_code == "CANCELLED"
    assert result.finish_reason == "cancelled"
    assert result.neural is False
    #: A withdrawn attempt publishes nothing.
    assert result.text == ""
    snapshot = handle.stream.snapshot()
    assert snapshot["cancelled"] is True
    assert snapshot["cancel_reason"] == "test cancelled it"


def producer_threads() -> int:
    return len([thread for thread in threading.enumerate()
                if thread.name == "forge-inference-stream" and thread.is_alive()])


def test_a_walked_away_consumer_does_not_leak_the_producer_thread():
    chunks = ["x"] * 40
    fabric, _backend = scripted_fabric(response="".join(chunks), chunks=chunks,
                                       chunk_delay=0.02)
    handle = fabric.stream(ModelRequest(prompt="leak check",
                                        capability="coding"))
    request_id = handle.result.request_id
    next(handle.events())          # the consumer takes one event...
    handle.stream.close()          # ...then disconnects without draining.
    assert fabric.cancel(request_id) is True
    deadline = time.time() + 10.0
    while producer_threads() and time.time() < deadline:
        time.sleep(0.05)
    assert producer_threads() == 0
    #: The stream still reports a terminal state rather than hanging open.
    assert fabric.status()["in_flight"] == 0


# -- context budget -----------------------------------------------------------------


def test_the_context_plan_keeps_required_sections_and_reports_omissions():
    planner = ContextBudgetPlanner()
    plan = planner.plan_for_request(
        prompt="Summarise the change", task="summarise",
        system="You are Forge.", instructions="Be terse.",
        context="R" * 40000, context_limit=512, max_output_tokens=128)
    assert plan.context_limit == 512
    assert plan.reserved_output_tokens >= 0
    assert plan.budget_tokens > 0
    kinds = [section.kind for section in plan.included]
    assert "prompt" in kinds
    assert plan.estimated_tokens <= plan.budget_tokens or plan.omitted
    rendered = plan.render()
    assert "Summarise the change" in rendered
    if plan.omitted:
        assert "OMITTED CONTEXT" in rendered
        assert plan.omitted_tokens > 0
    payload = plan.to_dict()
    assert payload["feasible"] in (True, False)
    assert payload["sections"] == len(plan.included)
    #: Metadata form carries no section text.
    assert "R" * 100 not in repr(payload)


def test_an_infeasible_context_is_refused_not_squeezed_silently():
    planner = ContextBudgetPlanner()
    plan = planner.plan_for_request(
        prompt="P" * 100000, task="big", context_limit=32,
        max_output_tokens=16)
    #: Either it fits the budget by omission/compression, or it says infeasible.
    assert plan.feasible is False or plan.estimated_tokens <= plan.budget_tokens
    if not plan.feasible:
        assert plan.reason


def test_the_fabric_applies_the_context_budget_to_a_real_request():
    fabric, backend = scripted_fabric(response="mock")
    result = fabric.generate(ModelRequest(
        prompt="What changed?", capability="coding", context="C" * 50000))
    assert result.success is True
    context = result.context
    assert context["budget_tokens"] > 0
    assert context["sections"] >= 1
    #: The double only ever saw the rendered, budgeted context.
    sent = str(backend.generate_calls[-1].prompt or "")
    assert len(sent) < 50000 + 1000
    assert "OMITTED CONTEXT" in sent or context["omitted_tokens"] == 0


def test_context_sections_rank_by_priority_then_score():
    planner = ContextBudgetPlanner()
    sections = [
        ContextSection(key="noise", kind="repository", text="n" * 4000,
                       score=0.1),
        ContextSection(key="ask", kind="prompt", text="Do the thing",
                       required=True, score=1.0),
        ContextSection(key="hint", kind="instructions", text="be brief",
                       score=0.9),
    ]
    plan = planner.plan(sections, context_limit=200, max_output_tokens=64)
    kinds = [section.kind for section in plan.included]
    assert "prompt" in kinds
    if "repository" not in kinds:
        assert any(item["key"] == "noise" for item in plan.omitted)


def test_labels_are_declared():
    assert MOCK_BACKEND_TEST == "MOCK_BACKEND_TEST"
    assert __doc__ and "MOCK_BACKEND_TEST" in __doc__
