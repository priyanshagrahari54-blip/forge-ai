"""A81 — runtime health checks, resource reporting, status, and failures.

Health is reported, never assumed: an unreachable backend is *unavailable*,
a discovery-only backend is *degraded*, and unmeasurable resources stay zero
rather than being invented.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from helpers_a81 import (  # noqa: E402
    BrokenBackend, ExplodingBackend, ModelServingBackend, ScriptedBackend,
    make_runtime,
)

from forge.runtime.model_runtime import (  # noqa: E402
    RUNTIME_VERSION, BackendUnavailableError, ErrorKind, ModelBackend,
    RuntimeConfig, RuntimeHealth, RuntimeModel, RuntimeRequest,
    RuntimeResources, RuntimeState, redact, redact_text, system_resources,
)


# -- health ------------------------------------------------------------------


def test_ready_backend_reports_ready():
    runtime = make_runtime(ScriptedBackend(name="alpha"))
    health = runtime.health()[0]
    assert isinstance(health, RuntimeHealth)
    assert health.status == RuntimeState.READY.value
    assert health.ok is True
    assert health.usable is True
    assert health.probed is True
    assert health.checked_at > 0
    assert health.to_dict()["status"] == "ready"


def test_unreachable_backend_reports_unavailable_with_a_redacted_error():
    boom = ExplodingBackend(name="boom")
    runtime = make_runtime(boom)
    health = runtime.health()[0]

    assert health.status == RuntimeState.UNAVAILABLE.value
    assert health.ok is False
    assert health.usable is False
    assert ExplodingBackend.SECRET not in health.error
    assert "[REDACTED]" in health.error


def test_health_can_skip_the_probe():
    runtime = make_runtime(ScriptedBackend(name="alpha"))
    health = runtime.health(probe=False)[0]
    assert health.probed is False
    assert health.status == RuntimeState.READY.value


def test_health_for_one_backend_only():
    runtime = make_runtime(ScriptedBackend(name="alpha"),
                           ScriptedBackend(name="beta"))
    items = runtime.health("beta")
    assert [item.backend for item in items] == ["beta"]


def test_health_of_an_unregistered_backend_is_reported_not_raised():
    runtime = make_runtime(ScriptedBackend(name="alpha"))
    health = runtime.health("ghost")[0]
    assert health.backend == "ghost"
    assert health.status == RuntimeState.UNAVAILABLE.value
    assert "not registered" in health.detail


def test_health_counters_accumulate_from_real_outcomes():
    runtime = make_runtime(ScriptedBackend(name="alpha"),
                           ExplodingBackend(name="boom"))
    runtime.generate(RuntimeRequest(prompt="x", backend="alpha"))
    runtime.generate(RuntimeRequest(prompt="x", backend="boom"))
    runtime.generate(RuntimeRequest(prompt="x", backend="boom"))

    health = {item.backend: item for item in runtime.health()}
    assert health["alpha"].generations == 1
    assert health["alpha"].failures == 0
    assert health["boom"].generations == 2
    assert health["boom"].failures == 2


def test_native_backend_is_degraded_without_an_inference_adapter():
    from forge.runtime.model_runtime import ModelRuntime, RuntimeConfig

    runtime = ModelRuntime.from_defaults(RuntimeConfig(), load_config=False)
    health = runtime.health()[0]
    assert health.backend == "native"
    assert health.status == RuntimeState.DEGRADED.value
    assert "inference adapter" in health.detail.lower()
    # Degraded is explicitly *not* ready: the runtime will not claim it can
    # run inference when it cannot.
    assert health.ok is False
    assert health.usable is True


def test_backend_that_needs_network_is_unavailable_until_enabled():
    from forge.runtime.model_runtime import OllamaBackend

    runtime = make_runtime(OllamaBackend(allow_network=False))
    health = runtime.health()[0]
    assert health.status == RuntimeState.UNAVAILABLE.value
    assert health.requires_network is True
    assert health.network_allowed is False
    assert "Network access is disabled" in health.detail


# -- resources ---------------------------------------------------------------


def test_resources_are_measured_never_invented():
    runtime = make_runtime(ScriptedBackend(name="alpha"))
    resources = runtime.resources()
    assert isinstance(resources, RuntimeResources)
    assert resources.cpu_count >= 0
    assert resources.python_version
    assert resources.platform
    assert resources.backends == 1
    assert resources.in_flight == 0
    # Accelerators are only reported when a backend reports one.
    assert resources.accelerators == ()

    payload = resources.to_dict()
    assert payload["memory_total_mb"] >= 0.0
    assert "extra" in payload


def test_system_resources_never_raises_and_degrade_to_zero():
    host = system_resources()
    assert set(host) == {"cpu_count", "memory_total_bytes",
                         "memory_available_bytes", "platform",
                         "python_version"}
    assert isinstance(host["cpu_count"], int)


def test_windows_memory_parsing_is_pure_and_safe():
    from forge.runtime.model_runtime import _memory_from_status

    class _Status:
        ullTotalPhys = 8 * 1024 ** 3
        ullAvailPhys = 4 * 1024 ** 3

    total, available = _memory_from_status(_Status())
    assert total == 8 * 1024 ** 3
    assert available == 4 * 1024 ** 3
    assert _memory_from_status(object()) == (0, 0)


def test_backend_resources_are_merged_into_the_report():
    class _Reporting(ScriptedBackend):
        def resources(self):
            return {"device": "cpu", "accelerators": ["stub-accel"],
                    "note": "value from the backend"}

    runtime = make_runtime(_Reporting(name="rep"))
    resources = runtime.resources()
    assert resources.accelerators == ("stub-accel",)
    assert resources.extra["rep"]["device"] == "cpu"


def test_a_backend_with_a_broken_resources_hook_is_ignored():
    class _BadResources(ScriptedBackend):
        def resources(self):
            raise RuntimeError("resource probe failed")

    runtime = make_runtime(_BadResources(name="bad"))
    resources = runtime.resources()
    assert resources.backends == 1
    assert "bad" not in resources.extra


# -- status ------------------------------------------------------------------


def test_status_snapshot_shape():
    serving = ModelServingBackend(name="serving", models=["alpha"])
    runtime = make_runtime(serving, ScriptedBackend(name="plain"))
    runtime.discover("serving")

    status = runtime.status()
    assert status["runtime"]["version"] == RUNTIME_VERSION
    assert status["runtime"]["closed"] is False
    assert status["runtime"]["config"]["allow_network"] is False
    assert sorted(info["name"] for info in status["backends"]) == [
        "plain", "serving"]
    assert status["models"]["total"] == 1
    assert status["models"]["by_backend"] == {"serving": 1}
    assert status["models"]["loaded"] == 0
    assert len(status["health"]) == 2
    assert status["resources"]["backends"] == 2
    assert status["in_flight"] == []
    # No prompt/completion text anywhere in the snapshot.
    assert "scripted-ok" not in str(status)


def test_status_contains_no_secrets_even_after_a_secret_leaking_failure():
    runtime = make_runtime(ExplodingBackend(name="boom"))
    runtime.generate(RuntimeRequest(prompt="x", backend="boom"))
    status = runtime.status()
    assert ExplodingBackend.SECRET not in str(status)
    assert "[REDACTED]" in str(status)


def test_history_is_bounded():
    runtime = make_runtime(ScriptedBackend(name="alpha"), history_size=5)
    for _ in range(12):
        runtime.generate(RuntimeRequest(prompt="x"))
    assert len(runtime.history(100)) == 5
    assert len(runtime.history(2)) == 2
    assert all("text" not in entry for entry in runtime.history(100))


# -- failures ----------------------------------------------------------------


def test_backend_failure_is_a_value_not_an_exception():
    runtime = make_runtime(ExplodingBackend(name="boom"))
    response = runtime.generate(RuntimeRequest(prompt="x", model="m"))

    assert response.success is False
    assert response.ok is False
    assert response.error_kind == ErrorKind.BACKEND.value
    assert response.finish_reason == "error"
    assert response.text == ""
    assert response.backend == "boom"


def test_unavailable_backend_error_kind_is_preserved():
    class _Unavailable(ModelBackend):
        name = "unavail"

        def available(self):
            return (False, "not configured")

        def generate(self, request, token=None):
            raise BackendUnavailableError("no model is resident")

    runtime = make_runtime(_Unavailable())
    response = runtime.generate(RuntimeRequest(prompt="x"))
    assert response.success is False
    assert response.error_kind == ErrorKind.UNAVAILABLE.value
    assert "no model is resident" in response.error


def test_failure_response_helper_masks_secrets():
    from forge.runtime.model_runtime import RuntimeResponse

    response = RuntimeResponse.failure(
        "connect failed with token=abcdefgh12345678", backend="b")
    assert response.success is False
    assert "abcdefgh12345678" not in response.error
    assert response.to_dict()["success"] is False


def test_a_backend_reporting_failure_without_a_kind_is_normalised():
    class _Vague(ModelBackend):
        name = "vague"

        def available(self):
            return (True, "ok")

        def generate(self, request, token=None):
            return RuntimeResponse(text="", success=False, error="something "
                                   "went wrong", backend="vague")

    runtime = make_runtime(_Vague())
    response = runtime.generate(RuntimeRequest(prompt="x"))
    assert response.success is False
    assert response.error_kind == ErrorKind.BACKEND.value
    assert response.finish_reason == "error"


def test_redaction_helpers():
    assert redact_text("api_key=hunter2secret") == "api_key=[REDACTED]"
    assert "hunter2" not in redact_text("Authorization: Bearer abcdef123456")
    assert redact_text("plain text") == "plain text"
    assert redact_text("") == ""
    assert redact({"a": "sk-abcdefghijklmnop1234",
                   "b": ["token=zzzzzzzzzzzzzzzzzz"], "c": 3}) == {
        "a": "[REDACTED]", "b": ["token=[REDACTED]"], "c": 3}


def test_nothing_is_logged_with_prompt_or_completion_content(caplog):
    backend = ScriptedBackend(name="alpha", response="SECRET-COMPLETION")
    runtime = make_runtime(backend)
    with caplog.at_level(logging.DEBUG, logger="forge.runtime.model"):
        runtime.generate(RuntimeRequest(prompt="SECRET-PROMPT-TEXT",
                                        context="SECRET-CONTEXT"))
        runtime.stream(RuntimeRequest(prompt="SECRET-PROMPT-TEXT"))
        list(runtime.stream(RuntimeRequest(prompt="SECRET-PROMPT-TEXT")))

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert logged, "the runtime is expected to log bounded diagnostics"
    assert "SECRET-PROMPT-TEXT" not in logged
    assert "SECRET-CONTEXT" not in logged
    assert "SECRET-COMPLETION" not in logged


def test_secret_leaking_backend_error_is_redacted_in_logs(caplog):
    runtime = make_runtime(ExplodingBackend(name="boom"))
    with caplog.at_level(logging.DEBUG, logger="forge.runtime.model"):
        runtime.generate(RuntimeRequest(prompt="x", backend="boom"))
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "SUPERSECRETVALUE123" not in logged
