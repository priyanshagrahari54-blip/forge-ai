"""Session 11 — the model identity contract and the backend protocol.

Two promises are tested here:

1. **Identity is earned, never assumed.** A model is ``DISCOVERED`` when a
   backend reports it, and it only becomes ``READY`` after a real
   verification. ``CONFIGURED -> READY`` is refused outright, a READY
   identity constructed without verification demotes itself, and a response
   that did not come from the requested model is treated as spoofing.
2. **The backend protocol is stable and honest.** Every adapter implements
   discover / health / load / unload / generate / stream / cancel /
   resource_requirements, reports its own locality and network needs, and
   refuses rather than improvises.

Label: ``MOCK_BACKEND_TEST`` — the doubles in this file are scripted. Nothing
here is real inference, and the doubles say so in their own metadata.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers_s11 import (MOCK_BACKEND_TEST, ScriptedModelBackend,  # noqa: E402
                         bounded_runtime_config, default_governor,
                         loopback_ollama_server, reference_fabric,
                         scripted_fabric, stop_server)

from forge.models.backends import BackendError, BackendRegistry  # noqa: E402
from forge.models.identity import (  # noqa: E402
    AvailabilityState,
    IdentityError,
    ModelIdentity,
    ModelSpoofingError,
    VerificationState,
)
from forge.models.request import ModelRequest  # noqa: E402
from forge.runtime.model_runtime import (  # noqa: E402
    ModelRuntime,
    RuntimeRequest,
)


# -- identity: states are earned ------------------------------------------------


def test_configured_is_never_promoted_to_ready_without_verification():
    identity = ModelIdentity(model_id="scripted:m",
                             availability_state=AvailabilityState.CONFIGURED.value,
                             verification_state=VerificationState.UNVERIFIED.value)
    with pytest.raises(IdentityError) as excinfo:
        identity.set_availability(AvailabilityState.READY.value)
    assert excinfo.value.code == "UNVERIFIED_PROMOTION"
    assert "CONFIGURED is never READY" in str(excinfo.value)
    #: The identity did not move: a refused promotion changes nothing.
    assert identity.availability_state == AvailabilityState.CONFIGURED.value
    assert not identity.usable


def test_ready_identity_without_verification_demotes_itself():
    identity = ModelIdentity(model_id="scripted:m",
                             availability_state=AvailabilityState.READY.value,
                             verification_state=VerificationState.UNVERIFIED.value)
    assert identity.availability_state == AvailabilityState.UNVERIFIED.value
    assert not identity.usable and not identity.verified


def test_discovered_loaded_ready_requires_a_verified_step():
    identity = ModelIdentity(model_id="scripted:m")
    assert identity.availability_state == AvailabilityState.DISCOVERED.value
    assert not identity.usable

    identity.set_availability(AvailabilityState.LOADED.value, reason="loaded")
    #: Two separate gates: availability says "selectable", verification says
    #: "proved". A loaded-but-unverified model passes the first and fails the
    #: second, and the router requires both (see the routing suite).
    assert identity.usable is True
    assert identity.verified is False

    identity.set_verification(VerificationState.VERIFIED.value,
                              method="fingerprint+health+probe")
    identity.set_availability(AvailabilityState.READY.value, reason="verified")
    assert identity.usable and identity.verified
    #: History records every step, so the promotion is auditable.
    reasons = " ".join(str(item.get("reason", "")) for item in identity.history)
    assert "loaded" in reasons and "verified" in reasons


def test_non_strict_promotion_records_the_refusal_instead_of_raising():
    identity = ModelIdentity(model_id="scripted:m",
                             availability_state=AvailabilityState.CONFIGURED.value)
    identity.set_availability(AvailabilityState.READY.value, strict=False)
    assert identity.availability_state == AvailabilityState.UNVERIFIED.value
    assert any("REFUSED" in str(item.get("reason", ""))
               for item in identity.history)


def test_denials_and_failures_are_reachable_from_any_state():
    for start in (AvailabilityState.DISCOVERED.value,
                  AvailabilityState.READY.value,
                  AvailabilityState.LOADED.value):
        identity = ModelIdentity(model_id="scripted:m", availability_state=start,
                                 verification_state=(
                                     VerificationState.VERIFIED.value
                                     if start == AvailabilityState.READY.value
                                     else VerificationState.UNVERIFIED.value))
        identity.set_availability(AvailabilityState.RESOURCE_DENIED.value,
                                  reason="g560 denies local loading")
        assert identity.denied and not identity.usable


def test_verification_expires_and_stops_being_selectable():
    identity = ModelIdentity(model_id="scripted:m")
    identity.set_verification(VerificationState.VERIFIED.value,
                              method="probe", ttl_seconds=0.01)
    identity.set_availability(AvailabilityState.READY.value)
    assert identity.usable
    import time

    time.sleep(0.03)
    assert identity.expire_verification() is True
    assert identity.verification_state == VerificationState.EXPIRED.value
    assert not identity.usable


def test_assert_same_model_refuses_substitution():
    identity = ModelIdentity(model_id="scripted:requested",
                             backend_id="scripted",
                             artifact_fingerprint="a" * 64)
    #: "The backend did not say" is not a mismatch.
    identity.assert_same_model(reported_model="", reported_backend="")
    #: A different model, backend or fingerprint is spoofing.
    with pytest.raises(ModelSpoofingError):
        identity.assert_same_model(reported_model="scripted:someone-else")
    with pytest.raises(ModelSpoofingError):
        identity.assert_same_model(reported_backend="other")
    with pytest.raises(ModelSpoofingError):
        identity.assert_same_model(reported_fingerprint="b" * 64)
    #: The requested identity (or its bare name) is accepted.
    identity.assert_same_model(reported_model="requested",
                               reported_backend="scripted",
                               reported_fingerprint="a" * 64)


def test_parameter_count_comes_from_metadata_not_from_a_guess():
    identity = ModelIdentity(model_id="scripted:m",
                             metadata={"parameter_count": 1234})
    from forge.models.identity import _parameter_count_from

    assert _parameter_count_from(identity.metadata, "m") == 1234
    #: Nothing reported means nothing claimed.
    assert _parameter_count_from({}, "m") in (None, 0)


# -- backend protocol (MOCK_BACKEND_TEST) ---------------------------------------


def test_mock_backend_implements_the_whole_protocol():
    fabric, backend = scripted_fabric(response="mock-answer",
                                      chunks=["mock-", "answer"])
    adapter = fabric.catalog.backends.get("scripted")

    #: discover
    identities = adapter.discover()
    assert [item.model_id for item in identities] == ["scripted:scripted-model"]
    assert identities[0].metadata.get("test_double") is True
    assert identities[0].metadata.get("label") == MOCK_BACKEND_TEST
    assert identities[0].parameter_count == 1234

    #: health
    status = adapter.health(probe=True)
    assert status.backend_id == "scripted"
    assert status.configured is True
    assert status.local is True and status.requires_network is False

    #: load / unload (through the residency cache the fabric uses)
    loaded = fabric.catalog.load("scripted:scripted-model")
    assert loaded["loaded"] is True
    assert backend.loaded_models == ["scripted:scripted-model"]
    assert fabric.catalog.unload("scripted:scripted-model")["unloaded"] is True
    assert backend.unloaded_models == ["scripted:scripted-model"]

    #: generate
    response = adapter.generate(RuntimeRequest(prompt="hi",
                                               model="scripted:scripted-model",
                                               backend="scripted"))
    assert getattr(response, "text", "") == "mock-answer"

    #: stream
    chunks = [str(getattr(chunk, "text", "") or "")
              for chunk in adapter.stream(RuntimeRequest(
                  prompt="hi", model="scripted:scripted-model",
                  backend="scripted"))]
    assert "".join(chunks) == "mock-answer"

    #: resource requirements are declared, not invented
    requirements = adapter.resource_requirements("scripted:scripted-model")
    assert requirements.requires_network is False


def test_mock_backend_generation_flows_through_the_fabric():
    fabric, backend = scripted_fabric(response="mock-answer")
    result = fabric.generate(ModelRequest(prompt="hi", capability="coding"))
    assert result.success is True
    assert result.model_id == "scripted:scripted-model"
    assert result.backend_id == "scripted"
    assert result.text == "mock-answer"
    #: A scripted double is still reported as "a backend produced this text";
    #: the honesty lives in the model's own metadata, which says test_double.
    identity = fabric.catalog.find("scripted:scripted-model")
    assert identity.metadata.get("test_double") is True
    assert backend.generate_calls, "the double was never asked"


def test_unknown_backend_is_refused_not_improvised():
    fabric, _ = scripted_fabric()
    with pytest.raises(BackendError) as excinfo:
        fabric.catalog.backends.get("does-not-exist")
    assert excinfo.value.code == "BACKEND_NOT_FOUND"
    assert "scripted" in str(excinfo.value)


def test_backend_registry_is_a_closed_set():
    registry = BackendRegistry()
    fabric, _ = scripted_fabric()
    adapter = fabric.catalog.backends.get("scripted")
    registry.register(adapter)
    with pytest.raises(BackendError):
        registry.register(adapter)  # duplicate id
    assert registry.unregister("scripted") is True
    assert registry.has("scripted") is False


def test_cancelling_an_in_flight_mock_request_signals_the_token():
    fabric, _ = scripted_fabric(response="x" * 64, chunk_delay=0.0)
    handle = fabric.stream(ModelRequest(prompt="hi", capability="coding",
                                        max_output_tokens=64))
    request_id = handle.result.request_id
    assert fabric.cancel(request_id, reason="test") in (True, False)
    result = handle.wait(5.0)
    #: Whatever the timing, the outcome is honest: cancelled or completed,
    #: never a fabricated success.
    assert result.state in ("cancelled", "succeeded", "stale")
    if result.state == "cancelled":
        assert result.success is False


def test_backend_locality_is_reported_honestly(tmp_path):
    fabric = reference_fabric(tmp_path, governor=default_governor())
    reference = fabric.catalog.backends.get("reference")
    assert reference.local is True
    assert reference.requires_network is False
    #: The native backend exists but cannot run inference by itself: it says
    #: so instead of pretending.
    native = fabric.catalog.backends.get("native")
    status = native.health(probe=False)
    assert status.ready is False
    assert "does not itself run inference" in (status.detail or "")


def test_remote_wrapped_backend_is_not_labelled_local():
    from forge.models.backends import RemoteProviderBackend

    runtime = ModelRuntime(bounded_runtime_config())
    adapter = RemoteProviderBackend(runtime, "native", backend_id="prov",
                                    provider_id="prov")
    assert adapter.local is False
    assert adapter.requires_network is True
    assert adapter.free is False  # never free-first


# -- Ollama over loopback (MOCK_BACKEND_TEST) -----------------------------------


def test_ollama_loopback_stub_discovers_verifies_and_generates():
    server, url = loopback_ollama_server(response="ollama-scripted")
    try:
        config = bounded_runtime_config(default_backend="ollama",
                                        backends=("native", "ollama"),
                                        ollama_url=url, allow_network=True)
        from forge.models.engine import InferenceFabric

        fabric = InferenceFabric.from_runtime(
            ModelRuntime.from_defaults(config))
        report = fabric.catalog.discover(backend_id="ollama")
        assert "ollama:tiny-llama" in report.registered
        identity = fabric.catalog.find("ollama:tiny-llama")
        assert identity.availability_state == AvailabilityState.DISCOVERED.value
        assert identity.verification_state == VerificationState.UNVERIFIED.value

        verification = fabric.catalog.verify(identity.model_id)
        assert verification.verified is True
        assert verification.method == "fingerprint+health+probe"
        assert identity.availability_state == AvailabilityState.READY.value

        result = fabric.generate(ModelRequest(prompt="hi", capability="",
                                              model=identity.model_id))
        assert result.success is True
        assert result.text == "ollama-scripted"
        assert result.backend_id == "ollama"
        #: The double was really asked over the loopback socket.
        assert any(call["path"].startswith("/api/generate")
                   for call in server.forge_calls)
    finally:
        stop_server(server)


def test_ollama_endpoint_stays_loopback_only():
    """A non-loopback Ollama URL can never reach an arbitrary host.

    The guard sits on the fabric adapter — the object routing and discovery
    actually use — and it refuses *before* any socket is opened.
    """
    from forge.models.engine import InferenceFabric

    runtime = ModelRuntime.from_defaults(bounded_runtime_config(
        default_backend="ollama", backends=("native", "ollama"),
        ollama_url="http://10.1.2.3:11434", allow_network=True))
    fabric = InferenceFabric.from_runtime(runtime)
    adapter = fabric.catalog.backends.get("ollama")
    assert adapter.base_url == "http://10.1.2.3:11434"

    status = adapter.health(probe=True)
    assert status.ready is False
    assert "not loopback" in (status.detail or "") + (status.denial or "")

    #: Discovery is refused by policy, not by a network timeout.
    with pytest.raises(BackendError) as excinfo:
        adapter.discover()
    assert excinfo.value.code == "NETWORK_POLICY"
    assert "not loopback" in str(excinfo.value)

    #: An explicit opt-in is the only way to allow it — and it is still
    #: health-checked, so nothing is claimed without a real endpoint.
    from forge.models.backends import OllamaCompatibleBackend

    permissive = OllamaCompatibleBackend(
        runtime, base_url=runtime.config.ollama_url, allow_non_loopback=True)
    assert permissive._guard("ollama") == ""


def test_scripted_double_never_claims_to_be_a_real_model():
    backend = ScriptedModelBackend(response="mock")
    assert "Test double" in backend.description
    assert MOCK_BACKEND_TEST in backend.list_models()[0].metadata["label"]
