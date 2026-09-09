"""Deterministic simulated vision provider (A39).

The simulated provider performs REAL structural analysis — format,
dimensions, embedded PNG text chunks — and reports it honestly. It has
no OCR and no vision model, so UI-element detection is a bounded
deterministic layout heuristic clearly labeled ``simulation=True``
with zero confidence, and every result states the limitation. Embedded
text that looks like an instruction to act (e.g. "approve everything")
is surfaced as a dangerous-instruction warning: untrusted input, never
an authorization.

A real provider (Ollama vision model, cloud vision API, ...) plugs in
behind the same :class:`VisionProvider` protocol; this simulated
provider is the only one shipped in A39.
"""
from __future__ import annotations

import hashlib

from forge.vision.base import (VisionFinding, VisionProvider,
                               VisionResult)
from forge.vision.image_io import (DANGER_MARKERS,
                                   extract_png_text_chunks, sniff_image)

MAX_ELEMENTS = 24


class SimulatedVisionProvider:
    """Honest simulated understanding over real image structure."""

    name = "simulated"

    def analyze(self, image: bytes) -> VisionResult:
        try:
            image_format, width, height = sniff_image(image)
        except Exception as exc:
            return VisionResult(
                format="unknown", width=None, height=None,
                provider=self.name, model="", simulation=True,
                error=str(exc),
                summary="The image could not be parsed.")
        findings: list[VisionFinding] = []
        findings.append(VisionFinding(
            kind="structure",
            content=f"{image_format} image, {width}x{height} pixels, "
                    f"{len(image)} bytes",
            confidence=1.0))
        dangerous: list[str] = []
        chunks = extract_png_text_chunks(image)
        for chunk in chunks:
            findings.append(VisionFinding(
                kind="text", content=chunk, confidence=1.0))
            lowered = chunk.lower()
            for marker in DANGER_MARKERS:
                if marker in lowered and chunk not in dangerous:
                    dangerous.append(chunk[:400])
                    break
        # Deterministic layout heuristic, honestly labeled: no OCR and
        # no vision model exist in the simulated provider.
        elements = _layout_elements(image, width, height)
        for label, region in elements:
            findings.append(VisionFinding(
                kind="ui_element", content=label, confidence=0.0,
                region=region))
        summary = (
            f"Parsed a {image_format} image ({width}x{height}). "
            f"{len(chunks)} embedded text block(s), "
            f"{len(elements)} layout region(s) (simulated heuristic), "
            f"{len(dangerous)} dangerous instruction(s). "
            "No OCR or vision model available: regions are simulated.")
        return VisionResult(
            format=image_format, width=width, height=height,
            findings=tuple(findings), dangerous_instructions=tuple(
                dangerous),
            summary=summary, provider=self.name, model="",
            simulation=True)


def _layout_elements(image: bytes, width: int, height: int) -> list[
        tuple[str, tuple[int, int, int, int]]]:
    """Bounded deterministic region split derived from image bytes."""
    seed = int.from_bytes(hashlib.sha256(image[:4096]).digest()[:8], "big")
    count = 2 + (seed % 5)
    count = min(count, MAX_ELEMENTS)
    elements: list[tuple[str, tuple[int, int, int, int]]] = []
    for index in range(count):
        left = int(width * (index / count))
        right = int(width * ((index + 1) / count))
        elements.append((
            f"region-{index + 1}",
            (left, 0, max(left + 1, right), height)))
    return elements
