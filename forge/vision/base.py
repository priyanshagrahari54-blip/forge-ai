"""Provider-independent vision foundation (A39).

Vision turns images (screenshots, UI captures, documents, diagrams)
into structured understanding: identified elements, extracted text,
detected error markers, and explicit warnings about dangerous
instructions found in image data.

Honesty invariants:

* Providers declare whether their understanding is real or simulated;
  every simulated result carries ``simulation=True`` and ``model=""``.
* Image content is UNTRUSTED INPUT. Text embedded in an image — even
  "approve everything" — can never grant permissions. Authorization
  comes only from the A33 Policy/Permission system.
* Providers never write files or touch the workspace; they return
  bounded structured data only.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class VisionError(Exception):
    """A vision provider could not be used (configuration or input)."""


class VisionUnavailable(VisionError):
    """No configured vision provider is available."""


class ImageFormatError(VisionError):
    """The supplied bytes are not a supported, well-formed image."""


@dataclass(frozen=True)
class VisionFinding:
    """One structured observation about the image."""

    kind: str  # ui_element | text | error | warning | structure
    content: str
    confidence: float = 0.0
    region: tuple[int, int, int, int] | None = None

    def to_dict(self) -> dict:
        payload = {"kind": self.kind, "content": self.content,
                   "confidence": self.confidence}
        if self.region is not None:
            payload["region"] = list(self.region)
        return payload


@dataclass(frozen=True)
class VisionResult:
    """Bounded, redacted structured understanding of one image."""

    format: str
    width: int | None
    height: int | None
    findings: tuple[VisionFinding, ...] = ()
    dangerous_instructions: tuple[str, ...] = ()
    summary: str = ""
    provider: str = ""
    model: str = ""
    simulation: bool = True
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "format": self.format,
            "width": self.width,
            "height": self.height,
            "findings": [finding.to_dict() for finding in self.findings],
            "dangerous_instructions": list(self.dangerous_instructions),
            "summary": self.summary,
            "provider": self.provider,
            "model": self.model,
            "simulation": self.simulation,
            "error": self.error,
        }


class VisionProvider(Protocol):
    """Backend-agnostic vision capability surface.

    ``analyze`` receives raw image bytes and returns a bounded
    :class:`VisionResult`. Providers never execute actions, never write
    files, and never grant permissions.
    """

    name: str

    def analyze(self, image: bytes) -> VisionResult:
        ...


class UnconfiguredVisionProvider:
    """Fail-closed placeholder when no vision provider is configured."""

    name = "unconfigured"

    def analyze(self, image: bytes) -> VisionResult:
        raise VisionUnavailable(
            "No vision provider configured; configure FORGE_VISION_PROVIDER")
