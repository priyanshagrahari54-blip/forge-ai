"""A81 — runtime generation, streaming, bounded timeouts, and cancellation.

Nothing here asserts model quality: the doubles replay caller-supplied text,
so these tests verify the *runtime's* guarantees — ordering, bounds,
cancellation, and honest failure reporting.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from helpers_a81 import (  # noqa: E402
    CooperativeBackend, HangingBackend, ScriptedBackend, make_runtime,
    run_in_thread, wait_until,
)

from forge.runtime.model_runtime import (  # noqa: E402
    CancelReason, CancellationToken, ModelRuntimeError, RuntimeCancelledError,
    RuntimeChunk, RuntimeRequest, RuntimeResponse, RuntimeStream,
    RuntimeTimeoutError,
)


# -- generation --------------------------------------------------------------


def test_generate_returns_structured_response():
    backend = ScriptedBackend(name="alpha", response="hello-runtime")
    runtime = make_runtime(backend)

    response = runtime.generate(RuntimeRequest(prompt="abcd", model="m1",
                                               trace_id="t-1"))
    assert response.success is True
    assert response.text == "hello-runtime"
    assert response.backend == "alpha"
    assert response.model == "m1"
    assert response.trace_id == "t-1"
    assert response.input_tokens == 4
    assert response.finish_reason == "stop"
    assert response.latency_ms >= 0.0
    # The loggable view never contains the prompt or the completion.
    payload = response.to_dict()
    assert "hello-runtime" not in str(payload)
    assert "abcd" not in str(payload)
    assert payload["text_chars"] == len("hello-runtime")


def test_request_prompt_composition_keeps_every_part():
    backend = ScriptedBackend(name="alpha")
    runtime = make_runtime(backend)
    runtime.generate(RuntimeRequest(prompt="do the thing", task="add export",
                                    context="repo context",
                                    instructions="no new deps",
                                    system="be terse"))

    sent = backend.generate_calls[0]
    composed = sent.compose_prompt()
    for part in ("be terse", "add export", "do the thing", "repo context",
                 "no new deps"):
        assert part in composed


def test_generate_rejects_non_requests():
    runtime = make_runtime(ScriptedBackend(name="alpha"))
    with pytest.raises(ModelRuntimeError):
        runtime.generate("just a string")
    with pytest.raises(ModelRuntimeError):
        runtime.stream(None)


def test_request_timeout_is_clamped_to_the_configured_bound():
    runtime = make_runtime(ScriptedBackend(name="alpha"), timeout_seconds=1.0,
                           max_timeout_seconds=3.0)
    assert runtime.config.clamp_timeout(None) == 1.0
    assert runtime.config.clamp_timeout(999.0) == 3.0
    assert runtime.config.clamp_timeout(-5.0) == 1.0
    assert runtime.config.clamp_timeout(0.5) == 0.5


# -- bounded timeouts --------------------------------------------------------


def test_generation_timeout_is_bounded_and_reported():
    runtime = make_runtime(HangingBackend(name="hang", hold=30.0),
                           default_backend="hang", timeout_seconds=0.4)
    started = time.perf_counter()
    response = runtime.generate(RuntimeRequest(prompt="x"))
    elapsed = time.perf_counter() - started

    assert response.success is False
    assert response.timed_out is True
    assert response.error_kind == "timeout"
    assert response.finish_reason == "timeout"
    assert response.text == ""
    assert 0.3 < elapsed < 3.0
    # The abandoned worker is a daemon: it must not keep the runtime busy.
    assert runtime.in_flight() == []
    assert runtime.health()[0].timeouts == 1


def test_stream_chunk_timeout_is_bounded():
    class _Stalling(ScriptedBackend):
        def stream(self, request, token=None):
            yield "first"
            time.sleep(10.0)
            yield "never"

    runtime = make_runtime(_Stalling(name="stall"), default_backend="stall",
                           timeout_seconds=5.0, chunk_timeout_seconds=0.3)
    stream = runtime.stream(RuntimeRequest(prompt="x"))
    received = []
    with pytest.raises(RuntimeTimeoutError) as exc:
        for chunk in stream:
            received.append(chunk.text)

    assert received == ["first"]
    assert "within" in str(exc.value)
    assert stream.response.success is False
    assert stream.response.error_kind == "timeout"
    assert stream.response.timed_out is True
    assert stream.response.text == "first"
    assert stream.response.metadata.get("partial") is True
    assert runtime.health()[0].timeouts == 1


def test_overall_stream_deadline_is_bounded():
    class _SlowDrip(ScriptedBackend):
        def stream(self, request, token=None):
            for _ in range(100):
                time.sleep(0.05)
                yield "drip"

    runtime = make_runtime(_SlowDrip(name="drip"), default_backend="drip",
                           timeout_seconds=0.4, chunk_timeout_seconds=5.0)
    stream = runtime.stream(RuntimeRequest(prompt="x"))
    with pytest.raises(RuntimeTimeoutError):
        list(stream)
    assert stream.response.timed_out is True
    assert stream.response.finish_reason == "timeout"


# -- cancellation ------------------------------------------------------------


def test_cancel_a_running_generation_promptly():
    hang = HangingBackend(name="hang", hold=30.0)
    runtime = make_runtime(hang, default_backend="hang", timeout_seconds=5.0)
    request = RuntimeRequest(prompt="x")
    thread, results = run_in_thread(runtime.generate, request)

    assert wait_until(lambda: runtime.in_flight())
    in_flight = runtime.in_flight()[0]
    assert in_flight["request_id"] == request.request_id
    assert in_flight["streaming"] is False

    assert runtime.cancel(request.request_id) is True
    thread.join(5)
    response = results[0]
    assert response.success is False
    assert response.cancelled is True
    assert response.error_kind == "cancelled"
    assert response.timed_out is False
    assert runtime.in_flight() == []
    assert runtime.health()[0].cancellations == 1


def test_cooperative_backend_observes_cancellation():
    coop = CooperativeBackend(name="coop")
    runtime = make_runtime(coop, default_backend="coop", timeout_seconds=5.0)
    request = RuntimeRequest(prompt="x")
    thread, results = run_in_thread(runtime.generate, request)

    assert wait_until(lambda: runtime.in_flight())
    runtime.cancel(request.request_id)
    thread.join(5)

    assert coop.observed_cancellation is True
    assert results[0].cancelled is True
    assert results[0].error_kind == "cancelled"


def test_cancel_unknown_request_is_false_and_cancel_all_is_bounded():
    runtime = make_runtime(ScriptedBackend(name="alpha"))
    assert runtime.cancel("never-existed") is False
    assert runtime.cancel_all() == 0

    hang = HangingBackend(name="hang", hold=10.0)
    runtime.register_backend(hang)
    threads = []
    for index in range(3):
        thread, _ = run_in_thread(
            runtime.generate, RuntimeRequest(prompt="x", backend="hang",
                                             request_id=f"r-{index}"))
        threads.append(thread)
    assert wait_until(lambda: len(runtime.in_flight()) == 3)
    assert runtime.cancel_all() == 3
    for thread in threads:
        thread.join(5)
    assert runtime.in_flight() == []


def test_stream_can_be_cancelled_mid_iteration():
    backend = ScriptedBackend(name="alpha",
                              chunks=["one ", "two ", "three ", "four "],
                              chunk_delay=0.02)
    runtime = make_runtime(backend, timeout_seconds=5.0)
    stream = runtime.stream(RuntimeRequest(prompt="x"))
    iterator = iter(stream)

    assert next(iterator).text == "one "
    assert stream.cancel() is True
    with pytest.raises(RuntimeCancelledError):
        for _ in iterator:
            pass

    assert stream.response.success is False
    assert stream.response.cancelled is True
    assert stream.response.error_kind == "cancelled"
    assert stream.response.finish_reason == "cancelled"
    assert stream.response.text == "one "
    assert stream.response.metadata["partial"] is True


def test_abandoning_a_stream_cancels_it():
    backend = ScriptedBackend(name="alpha",
                              chunks=["a", "b", "c", "d"], chunk_delay=0.02)
    runtime = make_runtime(backend, timeout_seconds=5.0)
    stream = runtime.stream(RuntimeRequest(prompt="x"))

    for chunk in stream:
        assert chunk.text == "a"
        break  # consumer walks away

    assert stream.cancelled is True
    assert stream.response.success is False
    assert stream.response.error_kind == "cancelled"


def test_stream_is_single_use():
    runtime = make_runtime(ScriptedBackend(name="alpha", chunks=["a"]))
    stream = runtime.stream(RuntimeRequest(prompt="x"))
    assert list(stream) == [RuntimeChunk(text="a", index=1,
                                         request_id=stream.request.request_id,
                                         output_tokens=1)]
    with pytest.raises(ModelRuntimeError):
        list(stream)


def test_cancellation_token_semantics():
    token = CancellationToken()
    assert token.cancelled is False
    assert token.cancel(CancelReason.USER.value) is True
    assert token.cancel() is False  # idempotent
    assert token.reason == CancelReason.USER.value
    assert token.wait(0.01) is True
    with pytest.raises(RuntimeCancelledError):
        token.raise_if_cancelled()

    timeout_token = CancellationToken()
    timeout_token.cancel(CancelReason.TIMEOUT.value)
    with pytest.raises(RuntimeTimeoutError):
        timeout_token.raise_if_cancelled()


# -- streaming behaviour -----------------------------------------------------


def test_stream_yields_chunks_in_order_with_a_final_response():
    backend = ScriptedBackend(name="alpha", chunks=["Hel", "lo, ", "world"])
    runtime = make_runtime(backend, timeout_seconds=5.0)
    stream = runtime.stream(RuntimeRequest(prompt="x", model="m1"))

    seen = []
    for chunk in stream:
        assert isinstance(chunk, RuntimeChunk)
        seen.append(chunk)

    assert [chunk.text for chunk in seen] == ["Hel", "lo, ", "world"]
    assert [chunk.index for chunk in seen] == [1, 2, 3]
    assert seen[-1].output_tokens == 3

    final = stream.response
    assert isinstance(final, RuntimeResponse)
    assert final.success is True
    assert final.text == "Hello, world"
    assert final.backend == "alpha"
    assert final.finish_reason == "stop"
    assert final.metadata["chunks"] == 3
    assert final.to_dict()["text_chars"] == len("Hello, world")


def test_stream_accepts_plain_strings_from_a_backend():
    class _Stringy(ScriptedBackend):
        def stream(self, request, token=None):
            for chunk in ("x", "y"):
                yield chunk

    runtime = make_runtime(_Stringy(name="stringy"), default_backend="stringy",
                           timeout_seconds=5.0)
    stream = runtime.stream(RuntimeRequest(prompt="p"))
    assert [chunk.text for chunk in stream] == ["x", "y"]
    assert stream.response.text == "xy"


def test_stream_reports_backend_failures_loudly():
    class _FailsMidway(ScriptedBackend):
        def stream(self, request, token=None):
            yield "start"
            raise RuntimeError("upstream disconnected")

    runtime = make_runtime(_FailsMidway(name="midway"),
                           default_backend="midway", timeout_seconds=5.0)
    stream = runtime.stream(RuntimeRequest(prompt="p"))
    with pytest.raises(ModelRuntimeError) as exc:
        list(stream)
    assert "upstream disconnected" in str(exc.value)
    assert stream.response.success is False
    assert stream.response.error_kind == "backend_error"
    assert stream.response.text == "start"
    assert stream.response.metadata["partial"] is True


def test_stream_protocol_violation_is_reported():
    from helpers_a81 import BrokenBackend

    runtime = make_runtime(BrokenBackend(name="broken"),
                           default_backend="broken", timeout_seconds=5.0)
    stream = runtime.stream(RuntimeRequest(prompt="p"))
    with pytest.raises(ModelRuntimeError) as exc:
        list(stream)
    assert "RuntimeChunk" in str(exc.value)
    assert stream.response.error_kind == "protocol"


def test_stream_is_lazy_until_iterated():
    backend = ScriptedBackend(name="alpha", chunks=["a"])
    runtime = make_runtime(backend, timeout_seconds=5.0)
    stream = runtime.stream(RuntimeRequest(prompt="p"))

    assert isinstance(stream, RuntimeStream)
    assert backend.stream_calls == []
    assert runtime.in_flight() == []
    assert list(stream)
    assert len(backend.stream_calls) == 1
    assert runtime.in_flight() == []


def test_stream_and_generate_do_not_share_request_ids():
    backend = ScriptedBackend(name="alpha", chunks=["a"])
    runtime = make_runtime(backend, timeout_seconds=5.0)
    stream = runtime.stream(RuntimeRequest(prompt="p"))
    iterator = iter(stream)
    next(iterator)
    with pytest.raises(ModelRuntimeError):
        list(runtime.stream(RuntimeRequest(
            prompt="p", request_id=stream.request.request_id)))
    list(iterator)
