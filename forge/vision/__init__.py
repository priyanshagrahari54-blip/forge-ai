"""Vision foundation (A39): provider-independent image understanding.

Simulated provider shipped in A39 (honestly labeled); real providers
plug in behind :class:`VisionProvider`.
"""
from forge.vision.base import (ImageFormatError, VisionError,
                               VisionFinding, VisionProvider, VisionResult,
                               VisionUnavailable,
                               UnconfiguredVisionProvider)
from forge.vision.image_io import (MAX_IMAGE_BYTES,
                                   extract_png_text_chunks, sniff_image,
                                   validate_image_bytes)
from forge.vision.pipeline import (AVAILABLE_PROVIDERS, build_vision_provider,
                                   propose_actions)
from forge.vision.simulated import SimulatedVisionProvider

__all__ = [
    "AVAILABLE_PROVIDERS",
    "ImageFormatError",
    "MAX_IMAGE_BYTES",
    "SimulatedVisionProvider",
    "UnconfiguredVisionProvider",
    "VisionError",
    "VisionFinding",
    "VisionProvider",
    "VisionResult",
    "VisionUnavailable",
    "build_vision_provider",
    "extract_png_text_chunks",
    "propose_actions",
    "sniff_image",
    "validate_image_bytes",
]
