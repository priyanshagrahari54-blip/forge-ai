"""Session 11 — REAL_INFERENCE_TEST: a real local model really runs.

The repository ships **no** weights and downloads nothing. These tests
materialise a tiny first-party reference artifact on disk (deterministic
pseudo-random values, ~600 KB, 150k parameters, character-level) and then
exercise the whole inference path against it: a real forward pass per token,
real residency accounting, real streaming, real cancellation, real timeouts.

The reference network is honest about what it is: it advertises **no** agentic
capability, it reports ``trained=False``, and its output is character-level
noise. It exists to prove the fabric end to end on a machine with no model
server and no internet — not to answer questions.

Every test here is labelled ``REAL_INFERENCE_TEST``: an actual model produced
the text being asserted on.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers_s11 import (  # noqa: E402
    REAL_INFERENCE_TEST,
    default_governor,
    g560_governor,
    reference_fabric,
    reference_model_id,
    write_reference_artifact,
)

from forge.models.reference_engine import (  # noqa: E402
    ReferenceArtifactWriter,
    ReferenceBackendError,
    ReferenceLocalBackend,
    ReferenceModelConfig,
    describe_reference_artifact,
)
from forge.models.request import ModelRequest  # noqa: E402
from forge.runtime.model_runtime import (  # noqa: E402
    BackendUnavailableError,
    CancellationToken,
    RuntimeCancelledError,
    RuntimeRequest,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


# -- the artifact: explicit, offline, deterministic, fingerprinted --------------


def test_artifact_bytes_are_deterministic_and_fingerprinted(tmp_path):
    first = tmp_path / "a"
    second = tmp_path / "b"
    report_a = ReferenceArtifactWriter.write(
        str(first / "reference-clm.forgeref"), ReferenceModelConfig())
    report_b = ReferenceArtifactWriter.write(
        str(second / "reference-clm.forgeref"), ReferenceModelConfig())
    assert report_a["fingerprint"] == report_b["fingerprint"]
    assert report_a["size_bytes"] == report_b["size_bytes"] > 0
    assert report_a["trained"] is False
    assert "not a capable assistant" in report_a["note"]

    other = ReferenceArtifactWriter.write(
        str(tmp_path / "c.forgeref"), ReferenceModelConfig(seed=999))
    assert other["fingerprint"] != report_a["fingerprint"]


def test_writer_refuses_to_overwrite_without_an_explicit_force(tmp_path):
    path = write_reference_artifact(tmp_path)
    with pytest.raises(ReferenceBackendError) as excinfo:
        ReferenceArtifactWriter.write(path, ReferenceModelConfig())
    assert "refusing to overwrite" in str(excinfo.value)
    #: ``overwrite=True`` is the explicit opt-in (the CLI's --force).
    ReferenceArtifactWriter.write(path, ReferenceModelConfig(), overwrite=True)


def test_config_bounds_are_enforced():
    with pytest.raises(ReferenceBackendError):
        ReferenceModelConfig(vocab_size=1).validate()
    with pytest.raises(ReferenceBackendError):
        ReferenceModelConfig(hidden_size=0).validate()
    with pytest.raises(ReferenceBackendError):
        ReferenceModelConfig(context_chars=0).validate()
    with pytest.raises(ReferenceBackendError):
        ReferenceModelConfig(temperature=0.0).validate()


def test_describe_artifact_reports_what_the_bytes_say(tmp_path):
    path = write_reference_artifact(tmp_path)
    info = describe_reference_artifact(path)
    assert info["header_ok"] is True
    assert info["format"] == "forgeref"
    assert info["size_bytes"] > 0

    corrupted = tmp_path / "corrupt.forgeref"
    corrupted.write_bytes(b"NOTFORGEREF" + b"\0" * 64)
    bad = describe_reference_artifact(str(corrupted))
    assert bad["header_ok"] is False
    #: A truncated file is reported, never guessed at.
    truncated = tmp_path / "short.forgeref"
    truncated.write_bytes(b"FORGEREF")
    assert describe_reference_artifact(str(truncated))["header_ok"] is False


def test_repository_ships_no_model_weights():
    """No weights in git, and nothing large enough to be a model."""
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=str(REPO_ROOT), stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, universal_newlines=True,
        timeout=120).stdout.split()
    assert tracked, "git ls-files returned nothing"
    weight_suffixes = (".gguf", ".safetensors", ".forgeref", ".onnx", ".pt",
                       ".pth", ".bin", ".ckpt", ".tflite")
    weights = [name for name in tracked
               if name.lower().endswith(weight_suffixes)]
    assert weights == [], "model weights must never be committed: %s" % weights
    big = [name for name in tracked
           if (REPO_ROOT / name).exists()
           and (REPO_ROOT / name).stat().st_size > 1024 * 1024]
    assert big == [], "unexpectedly large tracked files: %s" % big


# -- the backend: real work, honest refusals ------------------------------------


def test_backend_is_unavailable_without_artifacts_and_never_fabricates(tmp_path):
    backend = ReferenceLocalBackend(model_dirs=(str(tmp_path),))
    available, detail = backend.available()
    assert available is False
    assert detail
    assert backend.list_models() == []
    with pytest.raises((ReferenceBackendError, BackendUnavailableError)) as excinfo:
        backend.generate(RuntimeRequest(prompt="hi", model="reference:x",
                                        backend="reference"))
    #: The refusal says why; nothing is synthesised in its place.
    assert "cannot run inference" in str(excinfo.value) or \
        "no reference artifact" in str(excinfo.value)


def test_backend_refuses_implicit_artifact_creation(tmp_path):
    """Creation is explicit: no artifact appears because a request asked."""
    backend = ReferenceLocalBackend(model_dirs=(str(tmp_path),))
    assert backend.allow_create is False
    with pytest.raises((ReferenceBackendError, BackendUnavailableError)):
        backend.generate(RuntimeRequest(prompt="hi",
                                        model="reference:reference-clm",
                                        backend="reference"))
    assert list(tmp_path.iterdir()) == []


def test_real_forward_pass_is_bounded_and_deterministic(tmp_path):
    """REAL_INFERENCE_TEST: the same seed produces the same characters."""
    path = write_reference_artifact(tmp_path)
    backend = ReferenceLocalBackend(model_dirs=(str(tmp_path),))
    models = backend.list_models()
    assert [model.model_id for model in models] == [reference_model_id()]
    loaded = backend.load_model(models[0])
    assert loaded.loaded is True
    assert backend.resident_bytes == Path(path).stat().st_size

    request = RuntimeRequest(prompt="the quick brown fox", max_output_tokens=24,
                             model=reference_model_id(), backend="reference",
                             seed=1234, temperature=0.9, timeout=30.0)
    first = backend.generate(request)
    second = backend.generate(request)
    assert first.success is True and second.success is True
    assert first.text == second.text and first.text
    assert len(first.text) <= 24
    assert first.finish_reason in ("length", "stop")
    assert first.latency_ms > 0.0
    assert first.metadata["engine"] == "forge-reference"
    assert first.metadata["trained"] is False
    assert first.metadata["fingerprint"]
    assert first.metadata["context_limit"] == 64

    #: A different seed really samples differently (it is not a canned string).
    other = backend.generate(RuntimeRequest(
        prompt="the quick brown fox", max_output_tokens=24,
        model=reference_model_id(), backend="reference", seed=99,
        temperature=0.9, timeout=30.0))
    assert other.text != first.text


def test_real_generation_honours_cancellation(tmp_path):
    """A cancelled attempt produces no text — and says it was cancelled."""
    write_reference_artifact(tmp_path)
    backend = ReferenceLocalBackend(model_dirs=(str(tmp_path),))
    request = RuntimeRequest(prompt="abcdef", max_output_tokens=200,
                             model=reference_model_id(), backend="reference",
                             timeout=30.0)

    #: Blocking path: the runtime's cancellation contract raises.
    token = CancellationToken()
    token.cancel("test")
    with pytest.raises(RuntimeCancelledError):
        backend.generate(request, token=token)

    #: Streaming path: the terminal chunk reports the cancellation honestly.
    stream_token = CancellationToken()
    stream_token.cancel("test")
    chunks = list(backend.stream(request, token=stream_token))
    assert [chunk.text for chunk in chunks if chunk.text] == []
    assert chunks[-1].done is True
    assert chunks[-1].finish_reason == "cancelled"


def test_real_streaming_delivers_the_first_character_early(tmp_path):
    """REAL_INFERENCE_TEST: time-to-first-token is not the whole generation."""
    write_reference_artifact(tmp_path)
    backend = ReferenceLocalBackend(model_dirs=(str(tmp_path),))
    request = RuntimeRequest(prompt="incremental", max_output_tokens=64,
                             model=reference_model_id(), backend="reference",
                             timeout=30.0)
    started = time.perf_counter()
    stamps = []
    for chunk in backend.stream(request):
        stamps.append((time.perf_counter() - started, chunk.text, chunk.done))
    assert len(stamps) >= 8, stamps
    first = stamps[0][0]
    total = stamps[-1][0]
    assert stamps[-1][2] is True
    assert first < total, (first, total)
    #: Characters arrive spread over the generation, not all at the end.
    assert sum(1 for stamp in stamps if stamp[1]) >= 8


def test_abandoning_a_real_stream_does_not_leak_the_producer(tmp_path):
    import threading

    write_reference_artifact(tmp_path)
    backend = ReferenceLocalBackend(model_dirs=(str(tmp_path),))
    generator = backend.stream(RuntimeRequest(
        prompt="abandon", max_output_tokens=512,
        model=reference_model_id(), backend="reference", timeout=30.0))
    assert next(generator).text is not None
    generator.close()
    deadline = time.time() + 3.0
    while time.time() < deadline and [
            thread for thread in threading.enumerate()
            if thread.name == "forge-reference-stream"]:
        time.sleep(0.02)
    assert not [thread for thread in threading.enumerate()
                if thread.name == "forge-reference-stream"]


def test_real_generation_honours_its_deadline(tmp_path):
    write_reference_artifact(tmp_path)
    backend = ReferenceLocalBackend(model_dirs=(str(tmp_path),))
    #: A large bound with an impossible deadline must time out honestly.
    response = backend.generate(RuntimeRequest(
        prompt="abcdef", max_output_tokens=200000,
        model=reference_model_id(), backend="reference", timeout=0.05))
    assert response.success is False
    assert response.timed_out is True
    assert response.finish_reason == "timeout"
    assert len(response.text) < 200000


def test_real_streaming_emits_every_character(tmp_path):
    write_reference_artifact(tmp_path)
    backend = ReferenceLocalBackend(model_dirs=(str(tmp_path),))
    chunks = list(backend.stream(RuntimeRequest(
        prompt="stream", max_output_tokens=16,
        model=reference_model_id(), backend="reference", timeout=30.0)))
    text = "".join(str(getattr(chunk, "text", "") or "") for chunk in chunks)
    assert len(text) <= 16
    assert text
    assert any(bool(getattr(chunk, "done", False)) for chunk in chunks)


def test_unload_releases_the_real_bytes(tmp_path):
    write_reference_artifact(tmp_path)
    backend = ReferenceLocalBackend(model_dirs=(str(tmp_path),))
    backend.load_model(backend.list_models()[0])
    assert backend.resident_bytes > 0
    assert backend.unload_model(reference_model_id()) is True
    assert backend.resident_bytes == 0
    assert backend.loaded_models() == []


# -- end to end through the fabric (REAL_INFERENCE_TEST) ------------------------


def test_fabric_runs_real_inference_after_real_verification(tmp_path):
    fabric = reference_fabric(tmp_path, governor=default_governor())
    model_id = reference_model_id()
    identity = fabric.catalog.find(model_id)
    assert identity.availability_state == "discovered"
    assert identity.verification_state == "unverified"
    assert not identity.usable

    #: An unverified model is never selected: the ladder falls through and the
    #: answer is labelled non-neural (or refused), never passed off as a model.
    before = fabric.generate(ModelRequest(prompt="hello", capability="",
                                          max_output_tokens=16))
    assert before.model_id != model_id or before.neural is False

    verification = fabric.catalog.verify(model_id)
    assert verification.verified is True
    assert verification.method == "fingerprint+health+probe"
    assert verification.probe_chars > 0
    assert verification.latency_ms > 0.0
    assert [check.name for check in verification.checks] == [
        "artifact_fingerprint", "backend_reachable", "identity_probe"]
    assert all(check.ok for check in verification.checks)
    assert identity.availability_state == "ready"
    assert identity.verification_state == "verified"
    assert identity.artifact_fingerprint == verification.fingerprint

    started = time.time()
    result = fabric.generate(ModelRequest(prompt="explain the architecture",
                                          capability="", max_output_tokens=32))
    elapsed = time.time() - started
    assert result.success is True, result.error
    assert result.model_id == model_id
    assert result.backend_id == "reference"
    assert result.neural is True
    assert result.text and len(result.text) <= 32
    assert result.verification_state == "verified"
    assert result.availability_state == "ready"
    assert result.latency_ms > 0.0 and elapsed < 30.0
    assert result.output_scan["scanned_chars"] == len(result.text)
    #: Provenance is explainable.
    assert result.routing["selected_model"] == model_id
    assert "verification=verified" in result.routing["reason"]
    assert result.resource_result["allowed"] is True
    assert result.context["feasible"] is True


def test_reference_model_advertises_no_agentic_capability(tmp_path):
    """It must never be selected for real work it cannot do."""
    fabric = reference_fabric(tmp_path, governor=default_governor(),
                              verify=True)
    identity = fabric.catalog.find(reference_model_id())
    assert identity.capabilities == ()

    result = fabric.generate(ModelRequest(prompt="write a parser",
                                          capability="coding",
                                          max_output_tokens=16))
    #: The capability filter is never relaxed, so the reference model is not
    #: chosen; the honest outcome is the labelled non-neural rung.
    assert result.neural is False
    assert result.model_id != reference_model_id()
    assert "coding" in result.routing["reason"] or \
        result.routing["selected_model"] != reference_model_id()


def test_real_streaming_through_the_fabric_is_monotonic_and_complete(tmp_path):
    fabric = reference_fabric(tmp_path, governor=default_governor(),
                              verify=True)
    handle = fabric.stream(ModelRequest(prompt="stream this", capability="",
                                        max_output_tokens=24))
    sequences = []
    for event in handle.events():
        sequences.append(event.sequence)
        if event.delta:
            assert len(event.delta) >= 1
    result = handle.wait(30.0)
    assert sequences == sorted(set(sequences)), sequences
    assert handle.complete is True
    assert result.success is True and result.neural is True
    assert result.time_to_first_token_ms > 0.0
    #: Real incremental delivery: the first token arrived before the last one.
    assert result.time_to_first_token_ms < result.latency_ms + 1.0
    snapshot = handle.stream.snapshot()
    assert snapshot["complete"] is True
    assert snapshot["truncated"] is False
    assert snapshot["chars"] == len(result.text)


def test_real_generation_can_be_cancelled_mid_stream(tmp_path):
    fabric = reference_fabric(tmp_path, governor=default_governor(),
                              verify=True)
    handle = fabric.stream(ModelRequest(prompt="a long generation",
                                        capability="", max_output_tokens=200))
    first = next(iter(handle.events()))
    assert first.sequence >= 1
    assert fabric.cancel(handle.result.request_id, reason="test") is True
    result = handle.wait(30.0)
    assert result.success is False
    assert result.state in ("cancelled", "stale", "failed")
    assert len(result.text) < 200


def test_g560_profile_denies_real_local_inference(tmp_path):
    """A Win7/2GB thin client must never become an inference machine."""
    fabric = reference_fabric(tmp_path, governor=g560_governor(), verify=True)
    result = fabric.generate(ModelRequest(prompt="hello", capability="",
                                          max_output_tokens=16))
    assert result.neural is False
    assert result.model_id != reference_model_id()
    assert result.resource_result["profile"] == "g560"
    assert result.resource_result["model_loading_allowed"] is False
    reasons = " ".join(str(item.get("reason", ""))
                       for item in result.routing["rejected"])
    assert "g560" in reasons
    #: And loading is refused outright, not merely deprioritised.
    loaded = fabric.models_load(reference_model_id())
    assert loaded["loaded"] is False
    assert loaded["error_code"] in ("RESOURCE_DENIED", "RAM", "RESIDENCY_FULL",
                                    "RESIDENCY_BUDGET", "LOAD_FAILED")


def test_a_changed_artifact_fails_verification_against_the_recorded_one(
        tmp_path):
    """REAL_INFERENCE_TEST: the recorded fingerprint is a real pin (§7, §25).

    Discovery registers the bytes it saw. If the artifact changes afterwards,
    verification must fail with ``FINGERPRINT_MISMATCH`` and demote the model
    -- it may not quietly adopt the new bytes and carry on.
    """
    path = write_reference_artifact(tmp_path)
    fabric = reference_fabric(tmp_path, governor=default_governor(),
                              verify=True)
    model_id = reference_model_id()
    assert fabric.catalog.find(model_id).verified is True

    #: One byte in the middle of the weights (the tail is a zero bias vector,
    #: so writing zeros there would change nothing at all).
    data = bytearray(Path(path).read_bytes())
    middle = len(data) // 2
    data[middle] = (data[middle] + 7) % 256
    Path(path).write_bytes(bytes(data))

    result = fabric.catalog.verify(model_id)
    assert result.verified is False
    assert result.state == "failed"
    assert result.error_code == "FINGERPRINT_MISMATCH"
    checks = {check.name: check for check in result.checks}
    assert checks["artifact_fingerprint"].ok is False
    assert "does not match the registered" in checks[
        "artifact_fingerprint"].detail

    #: The identity is demoted, so it can no longer serve anything.
    identity = fabric.catalog.find(model_id)
    assert identity.verification_state == "failed"
    assert identity.availability_state == "unverified"
    refused = fabric.generate(ModelRequest(prompt="hi", capability="",
                                           allow_deterministic=False))
    assert refused.success is False
    assert refused.state == "unverified"
    assert refused.neural is False
    assert refused.text == ""


def test_tampering_changes_the_fingerprint(tmp_path):
    """Verification is tied to real bytes, so a changed artifact is a new one."""
    path = write_reference_artifact(tmp_path)
    fabric = reference_fabric(tmp_path, governor=default_governor())
    model_id = reference_model_id()
    before = fabric.catalog.verify(model_id)
    assert before.verified is True

    data = bytearray(Path(path).read_bytes())
    data[-1] = (data[-1] + 1) % 256
    Path(path).write_bytes(bytes(data))

    fabric.catalog.discover()
    after = fabric.catalog.verify(model_id)
    assert after.fingerprint != before.fingerprint
    #: The registered identity now carries the new fingerprint; a response
    #: claiming the old one is treated as spoofing.
    identity = fabric.catalog.find(model_id)
    assert identity.artifact_fingerprint == after.fingerprint
    from forge.models.identity import ModelSpoofingError

    with pytest.raises(ModelSpoofingError):
        identity.assert_same_model(reported_fingerprint=before.fingerprint)


def test_real_inference_label_is_used_by_this_suite():
    """The label is part of the contract, not a comment."""
    assert REAL_INFERENCE_TEST == "REAL_INFERENCE_TEST"
    assert __doc__ and "REAL_INFERENCE_TEST" in __doc__
