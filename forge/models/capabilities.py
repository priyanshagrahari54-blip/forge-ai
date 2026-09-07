"""Canonical capability vocabulary for the Model Fabric.

The Model Fabric routes requests by *capability*. This module is the single
source of truth for the capability names Forge understands, so routing,
registry validation, configuration, and documentation all share one list.
"""
from __future__ import annotations

from enum import Enum


class Capability(str, Enum):
    """Capabilities a model may advertise and a request may require."""

    CODING = "coding"
    REASONING = "reasoning"
    PLANNING = "planning"
    DEBUGGING = "debugging"
    TESTING = "testing"
    REVIEW = "review"
    SECURITY = "security"
    RESEARCH = "research"
    DOCUMENTATION = "documentation"
    VISION = "vision"
    IMAGE_GENERATION = "image_generation"
    AUDIO = "audio"
    SPEECH_TO_TEXT = "speech_to_text"
    TEXT_TO_SPEECH = "text_to_speech"
    BROWSER = "browser"
    COMPUTER_USE = "computer_use"
    TOOL_USE = "tool_use"
    STRUCTURED_OUTPUT = "structured_output"
    LONG_CONTEXT = "long_context"


#: Every supported capability, in canonical order.
ALL_CAPABILITIES: tuple[str, ...] = tuple(cap.value for cap in Capability)

#: Text-generation capabilities a conservative local or text LLM can advertise.
TEXT_CAPABILITIES: tuple[str, ...] = (
    Capability.CODING.value,
    Capability.REASONING.value,
    Capability.PLANNING.value,
    Capability.DEBUGGING.value,
    Capability.TESTING.value,
    Capability.REVIEW.value,
    Capability.SECURITY.value,
    Capability.RESEARCH.value,
    Capability.DOCUMENTATION.value,
    Capability.STRUCTURED_OUTPUT.value,
)

#: Capabilities that require understanding or producing non-text content.
MULTIMODAL_CAPABILITIES: tuple[str, ...] = (
    Capability.VISION.value,
    Capability.IMAGE_GENERATION.value,
    Capability.AUDIO.value,
)

#: Capabilities that produce or consume spoken audio.
AUDIO_CAPABILITIES: tuple[str, ...] = (
    Capability.SPEECH_TO_TEXT.value,
    Capability.TEXT_TO_SPEECH.value,
)

#: Capabilities that let a model act on an environment. Forge never grants a
#: model ambient authority: these are advertised only when an adapter is
#: explicitly registered with an approved runtime behind it.
AGENTIC_CAPABILITIES: tuple[str, ...] = (
    Capability.BROWSER.value,
    Capability.COMPUTER_USE.value,
    Capability.TOOL_USE.value,
)


def is_capability(value: object) -> bool:
    """Return True when *value* is a recognized capability name."""
    return isinstance(value, str) and value in ALL_CAPABILITIES


def normalize_capability(value: str | Capability) -> str:
    """Return the canonical string for a capability value.

    Raises ``ValueError`` for unknown names so typos fail loudly instead of
    silently never routing.
    """
    if isinstance(value, Capability):
        return value.value
    if isinstance(value, str) and value in ALL_CAPABILITIES:
        return value
    raise ValueError(f"Unknown capability: {value!r}")
