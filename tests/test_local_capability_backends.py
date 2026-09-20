"""The local capability backends: probed, honest, and registered only if real.

The vision / audio / speech / browser / computer-use / image capabilities used
to have no model on this deployment at all. They are now served in-process, and
the thing that must never regress is the *gate*: a capability is registered
only after its probe actually executed on this machine, and when a probe cannot
run, the report names the missing requirement instead of quietly claiming the
capability.

The probes are real (Pillow rasterises pixels, espeak-ng synthesises speech,
pocketsphinx decodes it, a loopback HTTP server serves a page scored by the
local DOM browser), so on a machine without those libraries these tests assert
the honest-negative path rather than skipping the contract entirely.
"""
from __future__ import annotations

import json

import pytest

from forge.models import local_capabilities as local
from forge.models.fabric import ModelFabric
from forge.models.local_capabilities import (
    local_capability_specs, probe_image_generation, probe_local_capabilities,
    probe_vision, register_local_capability_models)
from forge.vision.procedural import (
    PALETTES, parse_spec, procedural_image_available, render)


def _fabric() -> ModelFabric:
    return ModelFabric.from_defaults()


def _models(fabric: ModelFabric) -> list:
    return list(fabric.registry)


def _registered(fabric: ModelFabric, names) -> list:
    return [fabric.registry.get(name) for name in names]


def test_vision_probe_measures_real_pixels():
    report = probe_vision()
    if report.get("probe") != "passed":
        # No raster library here: the report must say why, and must not claim
        # a capability it could not exercise.
        assert report.get("reason")
        assert report.get("sample") is None
        return
    sample = report["sample"]
    assert sample["width"] > 0 and sample["height"] > 0
    assert 0.0 <= sample["mean_brightness"] <= 255.0


def test_a_failed_probe_is_never_reported_as_a_capability():
    reports = probe_local_capabilities()["reports"]
    for capability, report in reports.items():
        if report.get("probe") == "passed":
            continue
        #: "skipped" counts too: a probe that did not run must say why, or a
        #: reader cannot tell a missing backend from an unnoticed failure.
        assert report.get("reason"), f"{capability} was not probed and did " \
                                     "not say why"
    verified = probe_local_capabilities()["verified"]
    for capability in verified:
        assert reports[capability]["probe"] == "passed"


def test_registration_only_covers_probed_capabilities_and_hides_nothing():
    fabric = _fabric()
    result = register_local_capability_models(fabric, verify=True)
    registered = _registered(fabric, result["registered"])
    registered_capabilities = {
        capability for model in registered for capability in model.capabilities
    }
    probed = set(result["verified"])
    #: Everything registered really was probed...
    assert registered_capabilities == probed
    #: ...and nothing was registered for a capability that stayed unverified.
    assert len(registered) == len(result["registered"])
    assert registered_capabilities <= {spec["capability"]
                                       for spec in local_capability_specs()}
    for model in registered:
        assert model.metadata["runtime_verified"] is True
        assert model.metadata["simulated"] is False
        assert model.capability_status == {next(iter(model.capabilities)):
                                           "verified"}
    #: ...and each skipped backend states the requirement that is missing.
    for skipped in result["skipped"]:
        assert skipped["reason"].strip()


def test_a_capability_whose_probe_failed_is_refused(monkeypatch):
    """The gate itself: force one probe to fail and watch it be skipped."""
    healthy = probe_local_capabilities()
    broken = dict(healthy)
    broken["verified"] = [name for name in healthy["verified"]
                          if name != "image_generation"]
    reports = dict(healthy["reports"])
    broken["reports"] = dict(
        reports, image_generation={"probe": "failed",
                                   "reason": "no raster backend on this "
                                             "machine"})
    monkeypatch.setattr(local, "probe_local_capabilities",
                        lambda policy=None: broken)
    fabric = _fabric()
    result = register_local_capability_models(fabric, verify=True)
    registered = _registered(fabric, result["registered"])
    assert "forge-local/image-procedural" not in result["registered"]
    assert all("image_generation" not in model.capabilities
               for model in registered)
    skipped = {entry["capability"]: entry["reason"]
               for entry in result["skipped"]}
    #: Which sentence explains the skip depends on the machine: with the
    #: renderer installed the reason is the probe the test forced to fail,
    #: without it the reason is the package that is missing. Both name what is
    #: actually wrong, which is the property under test.
    reason = skipped["image_generation"]
    assert "raster" in reason or "Pillow" in reason


def test_unverified_registration_claims_nothing():
    """``verify=False`` must not advertise a capability it never exercised."""
    fabric = _fabric()
    before = sorted(model.name for model in _models(fabric))
    result = register_local_capability_models(fabric, verify=False)
    assert result["registered"] == []
    assert result["verified"] == []
    assert sorted(model.name for model in _models(fabric)) == before


def test_every_spec_declares_where_it_would_be_upgraded_from_here():
    for spec in local_capability_specs():
        assert spec["capability"]
        assert spec["provider"]
        if not spec["available"]:
            assert spec["requirement"].strip(), spec["capability"]


@pytest.mark.skipif(not procedural_image_available(),
                    reason="Pillow is not installed on this machine")
class TestProceduralImage:
    def test_render_produces_a_real_png_of_the_requested_size(self):
        raw = render({"title": "shelves", "width": 320, "height": 200,
                      "bars": [{"label": "a", "value": 3}]})
        assert raw[:8] == b"\x89PNG\r\n\x1a\n"
        from forge.vision.pixels import measure

        measured = measure(raw, source="test")
        assert (measured["width"], measured["height"]) == (320, 200)
        assert measured["edge_density"] > 0.0

    def test_the_same_request_renders_the_same_bytes(self):
        spec = {"title": "stable", "bars": [{"label": "a", "value": 1},
                                            {"label": "b", "value": 2}]}
        assert render(spec) == render(spec)

    def test_a_different_request_renders_different_pixels(self):
        first = render({"title": "one", "bars": [{"label": "a", "value": 1}]})
        second = render({"title": "two", "bars": [{"label": "a", "value": 9}]})
        assert first != second

    def test_the_report_says_it_is_not_generative(self):
        report = probe_image_generation()
        assert report["probe"] == "passed"
        from forge.vision.procedural import procedural_image_capability

        capability = procedural_image_capability()
        assert capability["generative"] is False
        assert "FORGE_IMAGE_URL" in capability["limitations"]

    def test_requests_are_read_from_json_or_from_labelled_lines(self):
        inline = parse_spec(
            '{"title": "t", "bars": [{"label": "a", "value": 1}]}')
        assert inline["title"] == "t"
        assert inline["bars"] == [{"label": "a", "value": 1}]
        labelled = parse_spec("title: Report\nbar: A-12=3\nbar: B-07=1\n")
        assert labelled["kind"] == "bar-chart"
        assert [bar["label"] for bar in labelled["bars"]] == ["A-12", "B-07"]
        from forge.models.errors import ProviderError

        with pytest.raises(ProviderError):
            parse_spec("please draw something nice")
        with pytest.raises(ProviderError):
            parse_spec("{not json at all}")

    def test_unknown_palette_and_oversized_canvas_are_refused(self):
        from forge.models.errors import ProviderError

        with pytest.raises(ProviderError):
            render({"title": "x", "palette": "chartreuse"})
        with pytest.raises(ProviderError):
            render({"title": "x", "width": 99999})
        assert set(PALETTES) >= {"forge", "light"}


def test_json_evidence_survives_a_round_trip_of_the_capability_names():
    """The evidence file is machine-read; its capability names must be stable."""
    from pathlib import Path

    path = (Path(__file__).resolve().parent.parent / "docs" / "evidence"
            / "capability-fleet-2026-09-20.json")
    if not path.is_file():
        pytest.skip("fleet evidence has not been produced in this checkout")
    payload = json.loads(path.read_text(encoding="utf-8"))
    for entry in payload["records"] + payload.get("multimodal_records", []):
        assert entry["capability"] in {
            "vision", "audio", "browser", "computer_use", "image_generation",
            "speech_to_text", "text_to_speech"}
        if entry["success"]:
            assert entry["output"].strip()
            assert entry["routed_model"]
        else:
            assert entry["error"].strip()


class TestLocalSpeechStability:
    """espeak-ng is a global-state C library; the wrapper must treat it as one.

    The engine used to be re-created for every request. ``espeak_Initialize``
    rebuilds the phoneme tables, and doing that hundreds of times inside a long
    run corrupted them: the compiler started printing "Invalid instruction ...
    for phoneme" and the process eventually died with SIGSEGV (exit 139). These
    tests pin the two fixes — one engine per process, one synthesis at a time —
    because losing either reintroduces an intermittent crash, not a wrong value.
    """

    @staticmethod
    def _speech():
        from forge.voice import local_speech

        if not local_speech.local_speech_state()["text_to_speech"]["available"]:
            pytest.skip("no local TTS engine on this machine")
        return local_speech

    def test_the_engine_is_initialised_once_per_process(self):
        speech = self._speech()
        assert speech._load_espeak() is speech._load_espeak()

    def test_repeated_synthesis_keeps_producing_valid_audio(self):
        import io
        import wave

        speech = self._speech()
        produced = []
        for index in range(3):
            raw = speech.synthesize(f"stability check number {index}")
            with wave.open(io.BytesIO(raw), "rb") as handle:
                assert handle.getnchannels() == 1
                assert handle.getframerate() == speech.SAMPLE_RATE
                assert handle.getnframes() > 0
            produced.append(raw)
        assert len(set(produced)) == len(produced)

    def test_concurrent_synthesis_is_serialised_and_does_not_corrupt(self):
        import io
        import threading
        import wave

        speech = self._speech()

        def worker(index: int, results: dict) -> None:
            results[index] = speech.synthesize(f"line spoken by worker {index}")

        results: dict = {}
        threads = [threading.Thread(target=worker, args=(index, results))
                   for index in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(results) == 4
        for raw in results.values():
            with wave.open(io.BytesIO(raw), "rb") as handle:
                assert handle.getnframes() > 0

    def test_the_lock_is_reentrant_so_a_nested_call_cannot_deadlock(self):
        speech = self._speech()
        with speech._ENGINE_LOCK:
            with speech._ENGINE_LOCK:
                assert speech._load_espeak() is speech._load_espeak()
