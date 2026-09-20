"""Real pixel analysis without a cloud endpoint.

The ``vision`` capability has two levels and Forge keeps them apart:

* **semantic** understanding (what the picture *means*) needs a vision-language
  model — that is the ``FORGE_VISION_URL`` bridge in
  :mod:`forge.vision.local`, and it is reported as BLOCKED until such an
  endpoint is configured;
* **measurement** on the real pixels needs no model at all, and this provider
  does exactly that: it decodes the bytes a caller supplied and reports what it
  measured — size, orientation, brightness, contrast, palette, colourfulness,
  edge density, transparency, and the loudest structural facts.

Nothing here invents content. Every number comes from the image that was given,
the response says ``semantic=false``, and a request without an image is refused
instead of answered from imagination.
"""
from __future__ import annotations

import base64
import io
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from forge.models.errors import ProviderError
from forge.models.provider import ModelResult

#: Decoding needs a real decoder; Pillow is optional so a text-only deployment
#: (and CI) still works, and its absence is reported, never hidden.
try:                                                        # pragma: no cover
    from PIL import Image
except Exception:                                          # noqa: BLE001
    Image = None                                           # type: ignore[assignment]

DATA_URI = re.compile(r"data:image/(?P<fmt>[a-zA-Z0-9.+-]+);base64,(?P<body>[A-Za-z0-9+/=\s]+)")
FILE_PATH = re.compile(r"(?:file|path)\s*:\s*(?P<path>[^\s\"']+)")
MAX_IMAGE_BYTES = 16 * 1024 * 1024
MAX_REMOTE_BYTES = 16 * 1024 * 1024


class VisionToolUnavailable(ProviderError):
    """The local measurement backend is not installed on this machine."""


def local_vision_available() -> bool:
    return Image is not None


def local_vision_capability() -> dict[str, Any]:
    return {
        "backend": "Pillow (in-process pixel measurement)",
        "available": local_vision_available(),
        "semantic": False,
        "requirement": ("" if local_vision_available() else
                        "install Pillow (pip install Pillow) to measure images "
                        "locally; semantic understanding additionally needs "
                        "FORGE_VISION_URL"),
        "limitations": ("measures real pixels (size, brightness, contrast, "
                        "palette, edges); it does not recognise objects, read "
                        "text, or describe semantics — that is a VLM's job"),
    }


def image_bytes(text: str) -> tuple[bytes, str]:
    """Decode the image a request carries: a data URI or a local file path."""
    source = str(text or "")
    match = DATA_URI.search(source)
    if match:
        try:
            raw = base64.b64decode(match.group("body"), validate=False)
        except Exception as exc:                            # noqa: BLE001
            raise ProviderError(f"data URI is not valid base64: {exc}") from exc
        if len(raw) > MAX_IMAGE_BYTES:
            raise ProviderError(f"image is {len(raw)} bytes; the cap is "
                                f"{MAX_IMAGE_BYTES}")
        return raw, f"data-uri:{match.group('fmt')}"
    path_match = FILE_PATH.search(source)
    if path_match:
        from pathlib import Path

        path = Path(path_match.group("path")).expanduser()
        if not path.is_file():
            raise ProviderError(f"no such image file: {path}")
        raw = path.read_bytes()
        if len(raw) > MAX_IMAGE_BYTES:
            raise ProviderError(f"image is {len(raw)} bytes; the cap is "
                                f"{MAX_IMAGE_BYTES}")
        return raw, str(path)
    raise ProviderError(
        "a vision request must carry the image as a 'data:image/...;base64,' "
        "URI or as 'file:/path/to.png' — nothing is guessed from the prompt")


def _palette(image, count: int = 5) -> list[dict[str, Any]]:
    small = image.convert("RGB").resize((64, 64))
    quantized = small.quantize(colors=count, method=2).convert("RGB")
    colors = quantized.getcolors(maxcolors=64 * 64) or []
    total = sum(item[0] for item in colors) or 1
    ranked = sorted(colors, key=lambda item: item[0], reverse=True)[:count]
    return [{"hex": "#%02x%02x%02x" % rgb, "share": round(freq / total, 4)}
            for freq, rgb in ranked]


def _edge_density(image) -> float:
    """Share of pixels whose horizontal neighbour differs noticeably.

    A real, resolution-independent structural signal: a flat diagram and a
    photograph of a crowd do not look alike here.
    """
    grey = image.convert("L").resize((96, 96))
    pixels = list(grey.getdata())
    width = 96
    edges = 0
    total = 0
    for row in range(96):
        base = row * width
        for column in range(width - 1):
            total += 1
            if abs(pixels[base + column] - pixels[base + column + 1]) > 24:
                edges += 1
    return round(edges / (total or 1), 4)


def measure(raw: bytes, *, source: str) -> dict[str, Any]:
    """Everything that can be measured about ``raw`` without a model."""
    if Image is None:
        raise VisionToolUnavailable(
            "Pillow is not installed, so no image can be decoded locally: "
            "pip install Pillow (semantic vision also needs FORGE_VISION_URL)")
    started = time.time()
    with Image.open(io.BytesIO(raw)) as image:
        image.load()
        rgb = image.convert("RGB")
        width, height = image.size
        stats = rgb.convert("L").getextrema()
        histogram = rgb.convert("L").histogram()
        pixels = sum(histogram) or 1
        mean = sum(index * count for index, count in enumerate(histogram)) / pixels
        variance = sum(count * (index - mean) ** 2
                       for index, count in enumerate(histogram)) / pixels
        darkness = sum(histogram[:64]) / pixels
        brightness = sum(histogram[192:]) / pixels
        # Colourfulness after Hasler & Süsstrunk: how far from grey the pixels are.
        colourfulness = 0.0
        sample = rgb.resize((48, 48))
        data = list(sample.getdata())
        for red, green, blue in data:
            colourfulness += (abs(red - green) + abs(green - blue)
                              + abs(red - blue)) / 3.0
        colourfulness = round(colourfulness / (len(data) or 1), 2)
        result = {
            "source": source,
            "format": (image.format or "").upper(),
            "mode": image.mode,
            "width": width,
            "height": height,
            "aspect_ratio": round(width / height, 4) if height else 0.0,
            "orientation": ("square" if width == height else
                            "landscape" if width > height else "portrait"),
            "megapixels": round(width * height / 1_000_000, 4),
            "greyscale_range": {"min": stats[0], "max": stats[1]},
            "mean_brightness": round(mean, 2),
            "contrast_stddev": round(variance ** 0.5, 2),
            "dark_share": round(darkness, 4),
            "bright_share": round(brightness, 4),
            "colourfulness": colourfulness,
            "edge_density": _edge_density(image),
            "has_alpha": image.mode in ("RGBA", "LA") or "transparency" in image.info,
            "format_details": {
                key: image.info.get(key) for key in
                ("dpi", "gamma", "icc_profile", "exif") if key in image.info
            },
            "bytes": len(raw),
            "palette": _palette(rgb),
            "measured_ms": round((time.time() - started) * 1000, 2),
            "semantic": False,
        }
        #: EXIF stays out of the answer: it can carry GPS and device identity.
        result["format_details"].pop("exif", None)
        result["format_details"].pop("icc_profile", None)
    return result


class LocalPixelVisionProvider:
    """The ``vision`` capability served by real measurement, honestly labelled."""

    name = "forge-local-vision"

    def __init__(self, *, allow_remote: bool = False, timeout: float = 20.0) -> None:
        self.allow_remote = bool(allow_remote)
        self.timeout = float(timeout)

    def generate(self, prompt: str, *, context: str = "", task: str = "",
                 instructions: str = "", max_output_tokens: int | None = None,
                 temperature: float | None = None) -> ModelResult:
        del context, max_output_tokens, temperature
        body = "\n".join(part for part in (str(task or ""), str(prompt or ""),
                                           str(instructions or "")) if part)
        if not local_vision_available():
            raise VisionToolUnavailable(local_vision_capability()["requirement"])
        started = time.time()
        raw, source = image_bytes(body)
        measured = measure(raw, source=source)
        summary = (
            f"image: {measured['width']}x{measured['height']} "
            f"{measured['format']} ({measured['orientation']}, "
            f"{measured['megapixels']} MP)\n"
            f"brightness {measured['mean_brightness']}/255, contrast "
            f"{measured['contrast_stddev']}, colourfulness "
            f"{measured['colourfulness']}, edge density "
            f"{measured['edge_density']}\n"
            f"dominant colours: "
            + ", ".join(f"{item['hex']} ({item['share']:.0%})"
                        for item in measured["palette"][:3])
            + "\nmeasured locally from real pixels; no semantic recognition "
              "(configure FORGE_VISION_URL for a vision-language model)")
        return ModelResult(
            text=summary,
            model="forge-vision/local-pixels",
            input_tokens=len(body.split()),
            output_tokens=len(summary.split()),
            latency=time.time() - started,
            metadata={
                "provider": self.name,
                "simulated": False,
                "semantic": False,
                "backend": "Pillow pixel measurement",
                "measurement": measured,
            })


def remote_measure(url: str, *, timeout: float = 20.0) -> dict[str, Any]:
    """Fetch an image from a URL and measure it — explicit, never implicit.

    Only used when a caller passes ``allow_remote``; the URL is fetched once,
    size-capped, and nothing about it is cached.
    """
    parsed = urllib.parse.urlsplit(str(url or ""))
    if parsed.scheme not in ("http", "https"):
        raise ProviderError("remote vision only fetches http(s) URLs")
    request = urllib.request.Request(str(url), headers={"User-Agent": "ForgeVision/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_REMOTE_BYTES + 1)
    except urllib.error.URLError as exc:
        raise ProviderError(f"could not fetch {url}: {exc}") from exc
    if len(raw) > MAX_REMOTE_BYTES:
        raise ProviderError(f"remote image exceeds {MAX_REMOTE_BYTES} bytes")
    return measure(raw, source=str(url))
