"""Real media for capability runs: a real bitmap, real samples, real pages.

The vision, audio, browser and computer-use specialists can only be shown to
work against inputs that are actually real — a bitmap of real bytes, samples
with real structure in them, and pages served over HTTP that a browser runtime
really fetches and acts on. Anything else (a placeholder file, an empty buffer,
a "pretend" URL) proves the wiring and nothing about the capability.

The inputs are built here once so every harness uses the same ones:
:mod:`scripts.run_capability_fleet` executes the 200 capability and multimodal
specialists with them, and the production-readiness report runs the same
specialists on the same inputs through the deployment's own fabric. Both then
report executions, not intentions.

Nothing here needs a network: the bitmap is written with zlib, the audio with
the ``wave`` module, and the pages are served on loopback.
"""
from __future__ import annotations

import struct
import threading
import time
import wave
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from typing import Any

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

#: Sample rate of the generated audio. 22050 Hz keeps the file small and is
#: what the local synthesizer emits, so the analyzer sees the same shape it
#: would from speech.
SAMPLE_RATE = 22050

#: Capabilities whose request carries media, and therefore cannot be executed
#: by a generic text task.
MEDIA_CAPABILITIES = ("vision", "audio", "browser", "computer_use")

#: The pages the local browser reads and acts on. Each has a title, headings,
#: links and a form, so navigation, extraction and actions all have something
#: real to work with.
PAGES: dict[str, bytes] = {
    "/": (b"<html><head><title>Warehouse console</title></head><body>"
          b"<h1>Warehouse console</h1>"
          b"<p>Stock levels are refreshed every five minutes.</p>"
          b"<a href='/orders'>Orders</a> <a href='/alerts'>Alerts</a>"
          b"<form action='/search' method='get'>"
          b"<input name='q' value=''><input type='submit'></form>"
          b"</body></html>"),
    "/orders": (b"<html><head><title>Orders</title></head><body>"
                b"<h1>Orders</h1><p>41 orders are waiting to ship today.</p>"
                b"<a href='/'>back</a></body></html>"),
    "/alerts": (b"<html><head><title>Alerts</title></head><body>"
                b"<h1>Alerts</h1><p>Two shelves are below their reorder "
                b"point.</p></body></html>"),
    "/search": (b"<html><head><title>Search</title></head><body>"
                b"<h1>Search results</h1><p>Found 3 matching SKUs.</p>"
                b"</body></html>"),
}


class _PageHandler(BaseHTTPRequestHandler):
    """Serve :data:`PAGES` on loopback; anything else is a real 404."""

    def do_GET(self) -> None:                              # noqa: N802
        path = self.path.split("?", 1)[0]
        body = PAGES.get(path)
        if body is None:
            body = b"<html><head><title>Not found</title></head><body>" \
                   b"<h1>Not found</h1></body></html>"
            self.send_response(404)
        else:
            self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:                   # noqa: D102
        return


def serve_pages() -> tuple[ThreadingHTTPServer, str]:
    """Serve :data:`PAGES` on a loopback port; returns ``(server, base_url)``."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PageHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def real_png(width: int = 96, height: int = 64) -> bytes:
    """A real bitmap with structure: a gradient, a dark block and a light bar.

    Structure matters: a flat colour would make "measure the image" pass while
    saying nothing, and would hide an extractor that returns a constant.
    """
    rows = []
    for y in range(height):
        row = bytearray(b"\x00")            # filter type 0 for this scanline
        for x in range(width):
            if 20 <= x <= 60 and 16 <= y <= 44:
                row += bytes((24, 24, 32))          # the dark block
            elif y >= height - 10 and (x // 4) % 2 == 0:
                row += bytes((240, 240, 240))       # a striped light bar
            else:
                #: Clamped on purpose: the gradient must stay inside the byte
                #: range for any requested width (wide canvases used to raise
                #: "bytes must be in range(0, 256)").
                row += bytes((min(255, 40 + x * 2), min(255, 90 + y * 2), 160))
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (PNG_SIGNATURE + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def wav_bytes(seconds: float = 1.6, rate: int = SAMPLE_RATE) -> bytes:
    """Real 16-bit mono audio: a tone, a silent gap, then a higher tone.

    The gap is deliberate — silence detection has to find something — and the
    two tones differ, so a feature extractor cannot pass by returning one
    constant for every window.
    """
    import math

    frames = bytearray()
    gap_start = int(len(_tone_plan(seconds)) * 0.45)
    gap_end = gap_start + int(rate * 0.25)
    for index, (frequency, amplitude) in enumerate(_tone_plan(seconds)):
        if gap_start <= index < gap_end:
            frames += struct.pack("<h", 0)
            continue
        #: A gentle envelope keeps the waveform off the clipping rail, so a
        #: "clipped" verdict would mean a real defect rather than a bad input.
        phase = 2 * math.pi * frequency * (index / rate)
        value = int(amplitude * 0.35 * math.sin(phase))
        frames += struct.pack("<h", max(-32768, min(32767, value)))
    buffer = BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(bytes(frames))
    return buffer.getvalue()


def _tone_plan(seconds: float) -> list[tuple[float, int]]:
    """Two tones with an envelope, one entry per sample."""
    rate = SAMPLE_RATE
    total = int(rate * seconds)
    plan: list[tuple[float, int]] = []
    for index in range(total):
        if index < total // 2:
            frequency, amplitude = 440.0, 9000
        else:
            frequency, amplitude = 880.0, 14000
        plan.append((frequency, amplitude))
    return plan


def speech_bytes(text: str) -> tuple[bytes, str]:
    """Real synthesized speech when an engine exists, else generated audio.

    Returns ``(audio, source)``. ``source`` is what the harness prints, so a
    reader can tell which of the two the run used: a real synthesiser, or the
    generated signal (still real samples — just not speech).
    """
    try:
        from forge.voice.local_speech import synthesize

        audio = synthesize(text)
        if audio:
            return audio, "espeak-ng (in-process)"
    except Exception:                                       # noqa: BLE001
        pass
    return wav_bytes(), "generated signal (no speech engine on this machine)"


def write_inputs(directory: str | Path, *,
                 speech: str = "the warehouse console shows two shelves "
                               "below their reorder point") -> dict[str, Any]:
    """Write the media a capability run needs and describe it.

    The returned mapping is what the request builders read, plus the facts the
    evidence file records: how many bytes each input really is and where the
    speech came from.
    """
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    image = real_png()
    audio, source = speech_bytes(speech)
    inputs: dict[str, Any] = {
        "image": str(target / "capability-probe.png"),
        "audio": str(target / "capability-probe.wav"),
        "spoken": str(target / "capability-probe-spoken.wav"),
        "chart": str(target / "capability-probe-chart.png"),
        "image_bytes": len(image),
        "audio_bytes": len(audio),
        "audio_source": source,
    }
    Path(inputs["image"]).write_bytes(image)
    Path(inputs["audio"]).write_bytes(audio)
    return inputs


def request_for(capability: str, media: dict[str, Any], *,
                agent: str = "") -> str:
    """The request a specialist of *capability* needs, media included.

    Written per capability, not per fleet: a vision request names an image, an
    audio request names a recording, a browser request names a URL, and a
    computer-use request names both a URL and the actions to perform. A generic
    text task would "fail" for all four while telling you nothing about them.
    """
    who = agent or f"{capability} specialist"
    if capability == "vision":
        return (f"Acting as {who}, inspect this image and report the first "
                f"concrete UI or layout problem you would fix and why.\n"
                f"file:{media['image']}")
    if capability == "audio":
        return (f"Acting as {who}, listen to this recording and report the "
                f"first concrete action you would take.\n"
                f"file:{media['audio']}")
    if capability == "browser":
        return (f"Acting as {who}, open the warehouse console, read the stock "
                f"status, and report the first concrete action you would "
                f"take.\nurl:{media['base']}/orders")
    if capability == "computer_use":
        return (f"Acting as {who}, operate the warehouse console: search for "
                f"the alerting shelves and report what you found.\n"
                f"url:{media['base']}/ actions:[{{\"click\": \"Alerts\"}}, "
                f"{{\"navigate\": \"{media['base']}/\"}}, "
                "{\"type\": {\"q\": \"low stock\"}}, "
                "{\"submit\": {\"q\": \"low stock\"}}]")
    return ""


def media_inputs(directory: str | Path = ".forge") -> tuple[Any, dict[str, Any]]:
    """Serve the pages and write the media: everything a run needs, started.

    The caller owns the returned server and should shut it down; the ``base``
    entry in the mapping is the URL the browser specialists use.
    """
    server, base = serve_pages()
    media = write_inputs(directory)
    media["base"] = base
    media["started_at"] = time.time()
    return server, media


def stop(server: Any) -> None:
    """Shut a server returned by :func:`serve_pages` down."""
    try:
        server.shutdown()
        server.server_close()
    except Exception:                                       # noqa: BLE001
        pass
