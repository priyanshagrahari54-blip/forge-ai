"""Real capability inputs: a bitmap, samples, and pages that are really served.

A capability run is only as honest as its inputs. If the bitmap is a
placeholder, the audio is silence, or the "page" is a string literal, then
"the specialist executed" means nothing. These tests pin what the inputs are:
a PNG whose pixels really differ, audio with a silent gap between two tones,
pages that answer over HTTP (and 404 properly), and requests that carry the
media a capability actually needs.
"""
from __future__ import annotations

import io
import struct
import urllib.error
import urllib.request
import wave

import pytest

from forge.capabilities.live_inputs import (
    PAGES, PNG_SIGNATURE, SAMPLE_RATE, media_inputs, real_png, request_for,
    speech_bytes, stop, wav_bytes, write_inputs)


def _pillow_missing() -> bool:
    """Pillow is an optional media extra; without it the local pixel backend
    honestly reports BLOCKED, so measurement tests skip here and its probes
    are what pin that behaviour."""
    try:
        from forge.vision import pixels
        return pixels.Image is None
    except Exception:
        return True


_needs_pillow = pytest.mark.skipif(
    _pillow_missing(),
    reason="Pillow (forge-ai[media]) is not installed on this interpreter")


class TestBitmap:
    def test_the_png_is_a_real_png_with_the_requested_geometry(self):
        raw = real_png(96, 64)
        assert raw[:8] == PNG_SIGNATURE
        width, height, depth, colour = struct.unpack(">IIBB", raw[16:26])
        assert (width, height, depth, colour) == (96, 64, 8, 2)

    @_needs_pillow
    def test_the_pixels_are_not_one_flat_colour(self):
        """A constant image would let a constant extractor look correct."""
        from forge.vision.pixels import measure

        measured = measure(real_png(), source="test")
        assert len(measured["palette"]) > 1, measured["palette"]
        assert measured["palette"][0]["share"] < 1.0
        assert measured["edge_density"] > 0.0
        assert 0.0 < measured["mean_brightness"] < 255.0

    @_needs_pillow
    def test_the_size_is_honoured(self):
        from forge.vision.pixels import measure

        measured = measure(real_png(140, 50), source="test")
        assert (measured["width"], measured["height"]) == (140, 50)


class TestAudio:
    def test_the_wav_is_real_pcm_at_the_stated_rate(self):
        raw = wav_bytes(seconds=1.0)
        with wave.open(io.BytesIO(raw), "rb") as handle:
            assert handle.getnchannels() == 1
            assert handle.getsampwidth() == 2
            assert handle.getframerate() == SAMPLE_RATE
            assert handle.getnframes() == pytest.approx(SAMPLE_RATE, abs=2)

    def test_it_contains_a_silent_gap_so_silence_detection_has_something(self):
        raw = wav_bytes(seconds=1.6)
        with wave.open(io.BytesIO(raw), "rb") as handle:
            frames = struct.unpack(f"<{handle.getnframes()}h",
                                   handle.readframes(handle.getnframes()))
        quiet = sum(1 for value in frames if abs(value) < 32)
        assert quiet > len(frames) * 0.05, "the generated audio has no silence"

    def test_the_two_halves_differ_so_a_constant_cannot_pass(self):
        raw = wav_bytes(seconds=1.0)
        with wave.open(io.BytesIO(raw), "rb") as handle:
            frames = struct.unpack(f"<{handle.getnframes()}h",
                                   handle.readframes(handle.getnframes()))
        half = len(frames) // 2
        first = max(abs(value) for value in frames[:half])
        second = max(abs(value) for value in frames[half:])
        assert first != second

    def test_speech_reports_which_source_it_used(self):
        audio, source = speech_bytes("two shelves are below their reorder point")
        assert audio[:4] == b"RIFF"
        assert source.strip()
        #: Either a real engine spoke, or the generator produced samples — both
        #: are real audio and the caller is told which one it got.
        assert "espeak" in source or "generated" in source


class TestPages:
    def test_the_pages_are_served_and_missing_ones_404(self):
        server, base = None, ""
        from forge.capabilities.live_inputs import serve_pages

        server, base = serve_pages()
        try:
            for path in PAGES:
                with urllib.request.urlopen(base + path, timeout=5) as response:
                    assert response.status == 200
                    assert b"<html" in response.read()
            with pytest.raises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(base + "/does-not-exist", timeout=5)
            assert raised.value.code == 404
        finally:
            stop(server)


class TestInputsAndRequests:
    def test_write_inputs_puts_real_files_on_disk(self, tmp_path):
        inputs = write_inputs(tmp_path)
        image = tmp_path / "capability-probe.png"
        audio = tmp_path / "capability-probe.wav"
        assert image.is_file() and audio.is_file()
        assert inputs["image_bytes"] == image.stat().st_size > 1000
        assert inputs["audio_bytes"] == audio.stat().st_size > 1000
        assert inputs["audio_source"].strip()

    def test_media_inputs_starts_the_server_and_the_caller_can_stop_it(
            self, tmp_path):
        server, media = media_inputs(tmp_path)
        try:
            with urllib.request.urlopen(media["base"] + "/alerts",
                                        timeout=5) as response:
                assert b"reorder" in response.read()
            assert media["base"].startswith("http://127.0.0.1:")
        finally:
            stop(server)

    @pytest.mark.parametrize("capability,marker", [
        ("vision", "file:"), ("audio", "file:"),
        ("browser", "url:"), ("computer_use", "actions:"),
    ])
    def test_each_capability_gets_a_request_carrying_its_media(
            self, capability, marker):
        media = {"image": "/tmp/i.png", "audio": "/tmp/a.wav",
                 "base": "http://127.0.0.1:9"}
        request = request_for(capability, media, agent=f"{capability}-01-0001")
        assert marker in request
        assert f"{capability}-01-0001" in request

    def test_a_capability_without_media_gets_no_media_request(self):
        assert request_for("coding", {"base": "http://127.0.0.1:9"}) == ""
