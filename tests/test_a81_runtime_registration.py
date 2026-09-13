"""A81 — runtime backend registration, routing, and backend isolation.

Registration is explicit (no import-by-name), routing never crosses backends
implicitly, and one misbehaving backend can never affect another.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from helpers_a81 import (  # noqa: E402
    BrokenBackend, ExplodingBackend, ModelServingBackend, ScriptedBackend,
    make_runtime,
)

from forge.runtime.model_runtime import (  # noqa: E402
    BUILTIN_BACKENDS, BackendKind, BackendNotFoundError, BackendProtocolError,
    ModelBackend, ModelRuntime, ModelRuntimeError, RuntimeConfig,
    RuntimeRequest, create_backend,
)


class _NotABackend:
    """A plain object: registration must refuse it."""


# -- registration ------------------------------------------------------------


def test_register_backend_and_list_it():
    runtime = ModelRuntime(RuntimeConfig())
    backend = ScriptedBackend(name="alpha")
    runtime.register_backend(backend)

    assert runtime.has_backend("alpha") is True
    assert runtime.get_backend("alpha") is backend
    infos = runtime.backends()
    assert [info.name for info in infos] == ["alpha"]
    assert infos[0].kind == BackendKind.CUSTOM.value
    assert infos[0].available is True


def test_default_native_backend_is_registered_by_from_defaults():
    runtime = ModelRuntime.from_defaults(RuntimeConfig(), load_config=False)
    assert runtime.has_backend("native") is True
    assert runtime.config.default_backend == "native"


def test_duplicate_registration_is_refused_unless_explicit():
    runtime = ModelRuntime(RuntimeConfig())
    runtime.register_backend(ScriptedBackend(name="dup"))
    with pytest.raises(BackendProtocolError):
        runtime.register_backend(ScriptedBackend(name="dup"))
    replacement = ScriptedBackend(name="dup", response="replaced")
    runtime.register_backend(replacement, replace=True)
    assert runtime.get_backend("dup") is replacement


def test_registration_refuses_non_backends_and_bad_names():
    runtime = ModelRuntime(RuntimeConfig())
    with pytest.raises(BackendProtocolError):
        runtime.register_backend(_NotABackend())
    with pytest.raises(BackendProtocolError):
        runtime.register_backend(None)

    class _Nameless(ModelBackend):
        name = ""

    with pytest.raises(BackendProtocolError):
        runtime.register_backend(_Nameless())

    class _Colon(ModelBackend):
        name = "a:b"

    with pytest.raises(BackendProtocolError):
        runtime.register_backend(_Colon())


def test_unregister_removes_backend_and_its_models():
    backend = ModelServingBackend(name="serving", models=["alpha"])
    runtime = make_runtime(backend)
    runtime.discover("serving")
    assert runtime.models("serving")

    assert runtime.unregister_backend("serving") is True
    assert runtime.has_backend("serving") is False
    assert runtime.models("serving") == []
    assert runtime.unregister_backend("serving") is False


# -- routing -----------------------------------------------------------------


def test_request_is_routed_to_the_named_backend():
    alpha = ScriptedBackend(name="alpha", response="from-alpha")
    beta = ScriptedBackend(name="beta", response="from-beta")
    runtime = make_runtime(alpha, beta)

    response = runtime.generate(RuntimeRequest(prompt="x", backend="beta"))
    assert response.success is True
    assert response.text == "from-beta"
    assert response.backend == "beta"
    assert alpha.generate_calls == []
    assert len(beta.generate_calls) == 1


def test_default_backend_is_used_when_none_is_named():
    alpha = ScriptedBackend(name="alpha", response="from-alpha")
    beta = ScriptedBackend(name="beta", response="from-beta")
    runtime = make_runtime(alpha, beta, default_backend="beta")

    response = runtime.generate(RuntimeRequest(prompt="x"))
    assert response.text == "from-beta"
    assert alpha.generate_calls == []


def test_unknown_backend_fails_without_implicit_failover():
    alpha = ScriptedBackend(name="alpha", response="from-alpha")
    runtime = make_runtime(alpha)

    response = runtime.generate(RuntimeRequest(prompt="x", backend="ghost"))
    assert response.success is False
    assert response.error_kind == "not_found"
    assert "ghost" in response.error
    # The registered backend was never asked to serve another's request.
    assert alpha.generate_calls == []

    with pytest.raises(BackendNotFoundError):
        runtime.get_backend("ghost")


def test_select_backend_is_explicit():
    alpha = ScriptedBackend(name="alpha")
    beta = ScriptedBackend(name="beta")
    runtime = make_runtime(alpha, beta, default_backend="alpha")

    assert runtime.select_backend().name == "alpha"
    assert runtime.select_backend("beta").name == "beta"
    with pytest.raises(BackendNotFoundError):
        runtime.select_backend("nope")


def test_backend_names_come_from_an_allowlist_only():
    with pytest.raises(BackendNotFoundError) as exc:
        create_backend("my.package.EvilBackend")
    assert "my.package.EvilBackend" in str(exc.value)
    for name in BUILTIN_BACKENDS:
        assert name in str(exc.value)

    native = create_backend("native", RuntimeConfig())
    assert native.name == "native"
    ollama = create_backend("ollama", RuntimeConfig())
    assert ollama.name == "ollama"


def test_from_defaults_rejects_an_unregistered_default_backend():
    config = RuntimeConfig(default_backend="ghost", backends=("native",))
    with pytest.raises(ValueError):
        ModelRuntime.from_defaults(config, load_config=False)


# -- isolation ---------------------------------------------------------------


def test_one_backend_failing_does_not_affect_another():
    boom = ExplodingBackend(name="boom")
    good = ScriptedBackend(name="good", response="still-working")
    runtime = make_runtime(good, boom)

    failed = runtime.generate(RuntimeRequest(prompt="x", backend="boom"))
    assert failed.success is False

    ok = runtime.generate(RuntimeRequest(prompt="x", backend="good"))
    assert ok.success is True
    assert ok.text == "still-working"

    health = {item.backend: item for item in runtime.health()}
    assert health["good"].failures == 0
    assert health["good"].generations == 1
    assert health["boom"].failures == 1


def test_discovery_isolates_a_backend_that_raises():
    broken = ExplodingBackend(name="broken")
    serving = ModelServingBackend(name="serving", models=["alpha", "beta"])
    runtime = make_runtime(serving, broken)

    results = runtime.discover()
    assert sorted(results["serving"]["discovered"]) == ["serving:alpha",
                                                        "serving:beta"]
    assert results["broken"]["discovered"] == []
    assert "error" in results["broken"]
    assert "SUPERSECRET" not in results["broken"]["error"]
    assert len(runtime.models("serving")) == 2
    assert runtime.models("broken") == []


def test_health_isolates_a_backend_whose_probe_raises():
    class _BadHealth(ModelBackend):
        name = "badhealth"

        def available(self):
            return (True, "ok")

        def health(self, probe=True):
            raise RuntimeError("probe blew up")

    good = ScriptedBackend(name="good")
    runtime = make_runtime(good, _BadHealth())

    health = {item.backend: item for item in runtime.health()}
    assert health["good"].status == "ready"
    assert health["badhealth"].status == "unavailable"
    assert "probe blew up" in health["badhealth"].error


def test_broken_backend_contract_is_reported_not_crashed():
    runtime = make_runtime(BrokenBackend(name="broken"))

    response = runtime.generate(RuntimeRequest(prompt="x"))
    assert response.success is False
    assert response.error_kind == "protocol"

    health = runtime.health()[0]
    assert health.status == "unavailable"
    assert "invalid health report" in health.detail


def test_runtime_reports_outcomes_per_backend():
    alpha = ScriptedBackend(name="alpha", response="a")
    beta = ScriptedBackend(name="beta", response="b")
    runtime = make_runtime(alpha, beta)

    runtime.generate(RuntimeRequest(prompt="x", backend="alpha"))
    runtime.generate(RuntimeRequest(prompt="x", backend="beta"))
    runtime.generate(RuntimeRequest(prompt="x", backend="boom"))

    counters = runtime.status()["counters"]
    assert counters["alpha"]["generations"] == 1
    assert counters["beta"]["generations"] == 1
    assert counters["alpha"]["failures"] == 0
    assert "boom" not in counters
    # history() is newest-first, and only real outcomes are recorded: the
    # request that named an unregistered backend never reached a backend.
    assert [entry["backend"] for entry in runtime.history(3)] == [
        "beta", "alpha"]


def test_closed_runtime_refuses_new_work():
    runtime = make_runtime(ScriptedBackend(name="alpha"))
    runtime.close()
    assert runtime.closed is True

    response = runtime.generate(RuntimeRequest(prompt="x"))
    assert response.success is False
    assert response.error_kind == "closed"

    with pytest.raises(ModelRuntimeError):
        runtime.stream(RuntimeRequest(prompt="x"))
    with pytest.raises(ModelRuntimeError):
        runtime.discover()
    runtime.close()  # idempotent


def test_duplicate_in_flight_request_ids_are_refused():
    from helpers_a81 import HangingBackend

    runtime = make_runtime(HangingBackend(name="hang"), default_backend="hang",
                           timeout_seconds=2.0)
    request = RuntimeRequest(prompt="x", request_id="fixed-id")
    assert request.request_id == "fixed-id"

    from helpers_a81 import run_in_thread

    thread, results = run_in_thread(runtime.generate, request)
    from helpers_a81 import wait_until

    assert wait_until(lambda: runtime.in_flight())
    clash = runtime.generate(RuntimeRequest(prompt="x",
                                            request_id="fixed-id"))
    assert clash.success is False
    assert clash.error_kind == "conflict"
    runtime.cancel("fixed-id")
    thread.join(5)
    assert results and results[0].cancelled is True
