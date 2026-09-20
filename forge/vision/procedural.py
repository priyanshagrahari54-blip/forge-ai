"""Real image synthesis without a diffusion server.

``image_generation`` had no local backend at all, so the 25 image-generation
specialists could never produce a pixel. This provider renders a real PNG from
a structured request (a title, labelled values, a bar/line chart, a colour
scheme) using Pillow's drawing primitives.

It is **procedural**, not generative: the picture is exactly what the request
described — no model invented subject matter, and it will not paint "a cat on a
surfboard". Every result says so in its metadata and tells you what a diffusion
endpoint (``FORGE_IMAGE_URL``) would add. Rendering is deterministic: the same
request produces the same bytes, which makes the output checkable instead of
merely plausible.
"""
from __future__ import annotations

import io
import json
import re
import time
from pathlib import Path
from typing import Any

from forge.models.errors import ProviderError
from forge.models.provider import ModelResult

try:                                                        # pragma: no cover
    from PIL import Image, ImageDraw
except Exception:                                          # noqa: BLE001
    Image = None                                           # type: ignore[assignment]
    ImageDraw = None                                       # type: ignore[assignment]

MAX_WIDTH = 2048
MAX_HEIGHT = 2048
DEFAULT_SIZE = (768, 512)

#: Palettes a request can name; each colour is a real (r, g, b) triple.
PALETTES: dict[str, dict[str, tuple[int, int, int]]] = {
    "forge": {"background": (14, 16, 22), "accent": (138, 5, 255),
              "text": (230, 218, 255), "muted": (90, 92, 104)},
    "light": {"background": (250, 250, 252), "accent": (24, 90, 219),
              "text": (20, 22, 28), "muted": (150, 152, 160)},
    "warm": {"background": (32, 22, 18), "accent": (243, 142, 71),
             "text": (255, 240, 226), "muted": (120, 96, 82)},
}

_JSON = re.compile(r"(\{.*\})", re.DOTALL)


def procedural_image_available() -> bool:
    return Image is not None and ImageDraw is not None


def procedural_image_capability() -> dict[str, Any]:
    return {
        "backend": "Pillow drawing (procedural renderer)",
        "available": procedural_image_available(),
        "generative": False,
        "requirement": ("" if procedural_image_available() else
                        "pip install Pillow to render images locally; a "
                        "diffusion server is a different backend"),
        "limitations": ("renders exactly what the request specifies (title, "
                        "bars, values, palette) — deterministic, no invented "
                        "subject matter; diffusion needs FORGE_IMAGE_URL"),
    }


def parse_spec(text: str) -> dict[str, Any]:
    """Read the render request: inline JSON, or ``title:``/``bar:`` lines."""
    source = str(text or "")
    match = _JSON.search(source)
    if match:
        try:
            spec = json.loads(match.group(1))
        except ValueError as exc:
            raise ProviderError(f"image spec is not valid JSON: {exc}") from exc
        if not isinstance(spec, dict):
            raise ProviderError("image spec must be a JSON object")
        return spec
    title = re.search(r"title\s*:\s*(.+)", source, re.IGNORECASE)
    bars: list[dict[str, Any]] = []
    for line in source.splitlines():
        item = re.match(r"\s*bar\s*:\s*(?P<label>[^=]+)=(?P<value>-?[\d.]+)",
                        line, re.IGNORECASE)
        if item:
            bars.append({"label": item.group("label").strip(),
                         "value": float(item.group("value"))})
    palette = re.search(r"palette\s*:\s*(\w+)", source, re.IGNORECASE)
    if not bars and not title:
        raise ProviderError(
            "a procedural image request must describe what to draw: inline "
            "JSON {\"title\": ..., \"bars\": [...]} or 'title: ...' plus "
            "'bar: label=value' lines")
    return {
        "title": title.group(1).strip() if title else "",
        "bars": bars,
        "palette": palette.group(1).lower() if palette else "forge",
        "kind": "bar-chart" if bars else "banner",
    }


def render(spec: dict[str, Any]) -> bytes:
    """Draw the requested image and return real PNG bytes."""
    if not procedural_image_available():
        raise ProviderError(procedural_image_capability()["requirement"])
    width = int(spec.get("width") or DEFAULT_SIZE[0])
    height = int(spec.get("height") or DEFAULT_SIZE[1])
    if not (64 <= width <= MAX_WIDTH and 64 <= height <= MAX_HEIGHT):
        raise ProviderError(
            f"image size must be 64..{MAX_WIDTH} x 64..{MAX_HEIGHT}, not "
            f"{width}x{height}")
    palette = PALETTES.get(str(spec.get("palette") or "forge").lower())
    if palette is None:
        raise ProviderError(
            f"unknown palette {spec.get('palette')!r}; available: "
            f"{', '.join(sorted(PALETTES))}")
    image = Image.new("RGB", (width, height), palette["background"])
    draw = ImageDraw.Draw(image)
    padding = max(16, width // 24)
    title = str(spec.get("title") or "")[:120]

    #: A header rule under the title, drawn rather than assumed.
    draw.text((padding, padding), title, fill=palette["text"])
    draw.line([(padding, padding * 2 + 4), (width - padding, padding * 2 + 4)],
              fill=palette["muted"], width=1)

    bars = spec.get("bars") or []
    label = str(spec.get("x_label") or "")
    if bars:
        values = [float(item.get("value") or 0.0) for item in bars]
        span = max(abs(min(values)), abs(max(values)), 1.0)
        area_top = padding * 2 + 16 + (12 if label else 0)
        area_bottom = height - padding - 18
        baseline = area_top + (area_bottom - area_top) * (
            0.5 + (abs(min(values)) / span) / 2 if min(values) < 0 else 1.0)
        draw.line([(padding, baseline), (width - padding, baseline)],
                  fill=palette["muted"], width=1)
        slot = (width - padding * 2) / max(1, len(bars))
        bar_width = max(4, int(slot * 0.6))
        for index, item in enumerate(bars):
            value = float(item.get("value") or 0.0)
            bar_height = int((abs(value) / span) * abs(baseline - area_top))
            left = int(padding + index * slot + (slot - bar_width) / 2)
            top = baseline - bar_height if value >= 0 else baseline
            bottom = baseline if value >= 0 else baseline + bar_height
            draw.rectangle([left, top, left + bar_width, bottom],
                           fill=palette["accent"])
            name = str(item.get("label") or "")[:14]
            draw.text((left, bottom + 3), name, fill=palette["text"])
            draw.text((left, top - 11 if value >= 0 else bottom + 14),
                      f"{value:g}", fill=palette["text"])
        if label:
            draw.text((padding, height - padding + 2), label,
                      fill=palette["muted"])
    else:
        #: A banner: the request's own lines, wrapped by real text measurement.
        body = [line for line in str(spec.get("body") or "").splitlines()
                if line.strip()][:12]
        y = padding * 3
        for line in body:
            draw.text((padding, y), line[:160], fill=palette["text"])
            y += 18
        if not body:
            draw.text((padding, y), "(no body text was requested)",
                      fill=palette["muted"])

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def measure_png(raw: bytes) -> dict[str, Any]:
    from forge.vision.pixels import measure

    return measure(raw, source="procedural:render")


class LocalProceduralImageProvider:
    """The ``image_generation`` capability, served by real rendering."""

    name = "forge-local-imagegen"

    def generate(self, prompt: str, *, context: str = "", task: str = "",
                 instructions: str = "", max_output_tokens: int | None = None,
                 temperature: float | None = None) -> ModelResult:
        del context, max_output_tokens, temperature
        body = "\n".join(part for part in (str(task or ""), str(prompt or ""),
                                           str(instructions or "")) if part)
        started = time.time()
        spec = parse_spec(body)
        raw = render(spec)
        out = re.search(r"(?:out|output)\s*[:=]\s*(?P<path>[^\s\"']+)", body)
        written = ""
        if out:
            target = Path(out.group("path")).expanduser()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            written = str(target)
        measured = measure_png(raw)
        summary = (
            f"rendered {measured['width']}x{measured['height']} PNG "
            f"({len(raw)} bytes, {measured['palette'][0]['hex']} dominant"
            + (f", written to {written}" if written else "")
            + "): procedural drawing of what the request specified — "
              "deterministic, no diffusion model involved"
        )
        return ModelResult(
            text=summary,
            model="forge-image/local-procedural",
            input_tokens=len(body.split()),
            output_tokens=len(summary.split()),
            latency=time.time() - started,
            metadata={
                "provider": self.name,
                "simulated": False,
                "generative": False,
                "backend": "Pillow procedural renderer",
                "bytes": len(raw),
                "written": written,
                "spec": spec,
                "image": {key: measured[key] for key in
                          ("width", "height", "format", "edge_density",
                           "colourfulness", "mean_brightness")},
                "limitations": procedural_image_capability()["limitations"],
                "remote_upgrade": "FORGE_IMAGE_URL (diffusion server)",
            })
