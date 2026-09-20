"""Model-class abstraction and parameter-scale metadata (A84 Stage A).

Forge historically treated every registry entry as "a text LLM". This module
separates three things that must never be conflated:

``model class``
    What *kind* of neural model a registry entry is (transformer LLM,
    multimodal, vision, audio, speech, embedding, reranker, reasoning,
    coding, long-context, tool-use, planning, specialist). The class decides
    which modalities and capability families the entry can serve; it is
    declarative metadata, never an inference result.

``parameter scale``
    How large the *model* is (parameter count, active-parameter count for
    mixture-of-experts, architecture family, context length, quantization).
    Every field is optional. A count that a provider does not disclose stays
    ``None`` / ``"unknown"`` — this module never fills in a plausible number,
    and :class:`ParameterScale.from_mapping` only reads explicitly declared
    values.

``infrastructure capability``
    What *Forge* can do: route to a model (``can_route``), host its weights
    (``forge_hosts``), or rely on a provider actually serving it
    (``provider_offers``). Routing to a trillion-parameter model is Forge's
    fabric exercising a provider; it is not Forge hosting trillion-parameter
    weights, and it is not proof the provider is reachable.

The honesty rules enforced by this module:

* ``parameter_count = unknown`` is a real, permanent state, not an error to
  hide — the serialized form always carries ``disclosed: false``.
* Scale bands are derived from *declared* counts only. No declared count,
  no band (``UNKNOWN``), and routing never rewards a model for an
  undisclosed size.
* :func:`describe_availability` renders a sentence that keeps the three axes
  apart (routing capability vs hosting vs provider availability) so no
  caller can launder "registered" into "running it ourselves".

Python floor: 3.8 (Windows 7 reference target). Stdlib only.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from forge.models.capabilities import ALL_CAPABILITIES

__all__ = [
    "ModelClass",
    "ParameterScale",
    "ScaleBand",
    "describe_availability",
    "is_model_class",
    "model_classes_for_capabilities",
    "normalize_model_class",
]


#: One billion parameters — the unit the bands below are expressed in.
_BILLION = 1_000_000_000


class ModelClass(str, Enum):
    """The kinds of neural models Forge can register and route to."""

    TRANSFORMER_LLM = "transformer_llm"
    MULTIMODAL = "multimodal"
    VISION = "vision"
    AUDIO = "audio"
    SPEECH = "speech"
    EMBEDDING = "embedding"
    RERANKER = "reranker"
    REASONING = "reasoning"
    CODING = "coding"
    LONG_CONTEXT = "long_context"
    TOOL_USE = "tool_use"
    PLANNING = "planning"
    SPECIALIST = "specialist"

    @classmethod
    def values(cls) -> Tuple[str, ...]:
        return tuple(member.value for member in cls)


ALL_MODEL_CLASSES: Tuple[str, ...] = ModelClass.values()

#: Which canonical fabric capabilities each model class can serve. This is
#: the *compatibility* table used for class-aware routing and for validating
#: that a declared capability set is coherent with the declared class. It is
#: descriptive only: routing still filters on the model's actual declared
#: capabilities, so a mislabelled class can never grant a capability.
CLASS_CAPABILITIES: Dict[str, Tuple[str, ...]] = {
    ModelClass.TRANSFORMER_LLM.value: (
        "coding", "reasoning", "planning", "debugging", "testing", "review",
        "security", "research", "documentation", "structured_output",
        "tool_use", "long_context",
    ),
    ModelClass.MULTIMODAL.value: (
        "vision", "audio", "coding", "reasoning", "research", "documentation",
        "structured_output",
    ),
    ModelClass.VISION.value: ("vision", "image_generation"),
    ModelClass.AUDIO.value: ("audio",),
    ModelClass.SPEECH.value: ("speech_to_text", "text_to_speech"),
    ModelClass.EMBEDDING.value: (),
    ModelClass.RERANKER.value: (),
    ModelClass.REASONING.value: (
        "reasoning", "planning", "research", "review", "debugging",
        "structured_output", "long_context",
    ),
    ModelClass.CODING.value: (
        "coding", "debugging", "testing", "structured_output",
    ),
    ModelClass.LONG_CONTEXT.value: (
        "long_context", "research", "documentation", "reasoning",
    ),
    ModelClass.TOOL_USE.value: ("tool_use", "structured_output"),
    ModelClass.PLANNING.value: ("planning", "reasoning"),
    ModelClass.SPECIALIST.value: (
        "review", "security", "testing", "documentation", "research",
    ),
}

#: Which content modalities each class consumes/produces. Used for
#: capability-requirement checks in tool planning; like the table above it
#: is descriptive metadata, never authority.
CLASS_MODALITIES: Dict[str, Dict[str, Tuple[str, ...]]] = {
    ModelClass.TRANSFORMER_LLM.value: {"input": ("text",), "output": ("text",)},
    ModelClass.MULTIMODAL.value: {
        "input": ("text", "image", "audio"), "output": ("text", "image")},
    ModelClass.VISION.value: {"input": ("image",), "output": ("text",)},
    ModelClass.AUDIO.value: {"input": ("audio",), "output": ("audio",)},
    ModelClass.SPEECH.value: {
        "input": ("audio", "text"), "output": ("text", "audio")},
    ModelClass.EMBEDDING.value: {"input": ("text",), "output": ("vector",)},
    ModelClass.RERANKER.value: {
        "input": ("text", "candidates"), "output": ("ranking",)},
    ModelClass.REASONING.value: {"input": ("text",), "output": ("text",)},
    ModelClass.CODING.value: {"input": ("text",), "output": ("text",)},
    ModelClass.LONG_CONTEXT.value: {"input": ("text",), "output": ("text",)},
    ModelClass.TOOL_USE.value: {
        "input": ("text", "tools"), "output": ("text", "tool_calls")},
    ModelClass.PLANNING.value: {"input": ("text",), "output": ("plan",)},
    ModelClass.SPECIALIST.value: {"input": ("text",), "output": ("text",)},
}


def is_model_class(value: object) -> bool:
    """Return True when *value* is a recognized model-class name."""
    return isinstance(value, str) and value in ALL_MODEL_CLASSES


def normalize_model_class(value: Any) -> str:
    """Coerce to a canonical class name; raise on unknown names.

    ``""``/``None`` mean "class not declared", which is distinct from a
    misdeclared class: absence is allowed, typos are not.
    """
    if value is None or value == "":
        return ""
    if isinstance(value, ModelClass):
        return value.value
    text = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    if text in ALL_MODEL_CLASSES:
        return text
    raise ValueError(
        f"unknown model class {value!r}; expected one of "
        f"{', '.join(ALL_MODEL_CLASSES)}")


def capabilities_for_class(model_class: Any) -> Tuple[str, ...]:
    """Return the capability families a model class can serve."""
    name = normalize_model_class(model_class)
    if not name:
        return ALL_CAPABILITIES
    return CLASS_CAPABILITIES.get(name, ())


def model_classes_for_capabilities(
        capabilities: Iterable[str]) -> Tuple[str, ...]:
    """Return every model class that could serve all *capabilities*."""
    wanted = {c for c in capabilities if c in ALL_CAPABILITIES}
    if not wanted:
        return ALL_MODEL_CLASSES
    matches = []
    for name, provided in CLASS_CAPABILITIES.items():
        if wanted.issubset(set(provided)):
            matches.append(name)
    return tuple(matches)


def class_is_coherent(model_class: Any, capabilities: Iterable[str]) -> bool:
    """Would the declared class plausibly serve these declared capabilities?

    Empty class means "undeclared" and is always coherent. Embedding and
    reranker classes intentionally have an empty capability tuple because the
    fabric has no text-generation route for them; they are coherent only with
    an empty capability set.
    """
    name = normalize_model_class(model_class)
    if not name:
        return True
    provided = set(CLASS_CAPABILITIES.get(name, ()))
    wanted = {c for c in capabilities if c in ALL_CAPABILITIES}
    return wanted.issubset(provided)


# -- parameter scale ---------------------------------------------------------

class ScaleBand(str, Enum):
    """Bands derived from *declared* parameter counts only."""

    UNKNOWN = "unknown"
    SMALL = "small"        # < 2B
    MEDIUM = "medium"      # 2B .. 16B
    LARGE = "large"        # 16B .. 70B
    XLARGE = "xlarge"      # 70B .. 200B
    HUGE = "huge"          # 200B .. 1T (hundreds of billions)
    MASSIVE = "massive"    # >= 1T (trillion-scale)

    @classmethod
    def values(cls) -> Tuple[str, ...]:
        return tuple(member.value for member in cls)


#: (exclusive upper bound in parameters, band) — MASSIVE is the remainder.
_BAND_EDGES: Tuple[Tuple[int, str], ...] = (
    (2 * _BILLION, ScaleBand.SMALL.value),
    (16 * _BILLION, ScaleBand.MEDIUM.value),
    (70 * _BILLION, ScaleBand.LARGE.value),
    (200 * _BILLION, ScaleBand.XLARGE.value),
    (1_000 * _BILLION, ScaleBand.HUGE.value),
)

#: Explicit architecture labels that mark a sparse / mixture-of-experts model.
MOE_ARCHITECTURES = frozenset({"moe", "mixture_of_experts", "sparse_moe",
                               "switch_transformer", "deepseek_moe"})


def band_for_count(count: Optional[int]) -> str:
    """Return the band for a declared count; ``unknown`` without a count."""
    if not isinstance(count, int) or count <= 0:
        return ScaleBand.UNKNOWN.value
    for limit, band in _BAND_EDGES:
        if count < limit:
            return band
    return ScaleBand.MASSIVE.value


def parse_declared_count(value: Any) -> Optional[int]:
    """Convert an *explicitly disclosed* count to an integer parameter count.

    Accepts positive integers and short disclosure strings with a magnitude
    suffix (``"7B"``, ``"70b"``, ``"671B"``, ``"1.2T"``, ``"124000M"``).
    Anything else — ranges, "~", vague labels like ``"large"``, garbage —
    returns ``None`` so the registry keeps the honest ``unknown`` state.
    This parser never guesses a number the provider did not disclose.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        return int(value) if value > 0 and value.is_integer() else None
    if not isinstance(value, str):
        return None
    text = value.strip().upper().replace(",", "").replace("_", "")
    if not text:
        return None
    suffix_mult = 1
    if text[-1] in ("B", "T", "M", "K"):
        suffix_mult = {"B": _BILLION, "T": 1_000 * _BILLION,
                       "M": 1_000_000, "K": 1_000}[text[-1]]
        text = text[:-1]
    try:
        number = float(text)
    except ValueError:
        return None
    if number <= 0 or number != number:  # rejects NaN too
        return None
    scaled = number * suffix_mult
    if scaled < 1:
        return None
    return int(scaled)


@dataclass(frozen=True)
class ParameterScale:
    """Declared parameter-scale metadata for one model.

    Every field is optional. ``None``/``""`` means "not disclosed" and stays
    that way; serialization always reports ``disclosed`` so consumers cannot
    mistake absence for zero.
    """

    parameter_count: Optional[int] = None
    active_parameter_count: Optional[int] = None
    architecture: str = ""
    quantization: str = ""
    context_length: Optional[int] = None
    model_version: str = ""
    #: Which disclosure source the counts came from (provider docs, GGUF
    #: header, operator config). Empty means operator-declared without a
    #: cited source; it does not make the value less unknown when missing.
    declared_by: str = ""

    # -- construction ------------------------------------------------------

    @classmethod
    def from_mapping(cls, data: Optional[Mapping[str, Any]]) -> "ParameterScale":
        """Read only explicitly declared keys; never infer anything.

        ``parameter_count: null`` and a missing key are the same honest
        outcome: unknown. Values already given as ints pass through;
        disclosure strings are parsed strictly (see
        :func:`parse_declared_count`).
        """
        data = data or {}
        raw_total = data.get("parameter_count")
        raw_active = data.get("active_parameter_count")
        context = data.get("context_length")
        if isinstance(context, bool) or not isinstance(context, (int, float)):
            context_value = None
        else:
            context_value = int(context) if context and int(context) > 0 else None
        return cls(
            parameter_count=parse_declared_count(raw_total),
            active_parameter_count=parse_declared_count(raw_active),
            architecture=str(data.get("architecture") or "").strip().lower(),
            quantization=str(data.get("quantization") or "").strip(),
            context_length=context_value,
            model_version=str(data.get("model_version") or "").strip(),
            declared_by=str(data.get("parameter_source")
                            or data.get("declared_by") or "").strip(),
        )

    # -- derived views -----------------------------------------------------

    @property
    def disclosed(self) -> bool:
        return isinstance(self.parameter_count, int) and self.parameter_count > 0

    @property
    def band(self) -> str:
        return band_for_count(self.parameter_count)

    @property
    def active_band(self) -> str:
        return band_for_count(self.active_parameter_count)

    @property
    def is_moe(self) -> bool:
        """True only when the *declared architecture* says MoE, or a smaller
        active count than total was disclosed (the sparse-expert signature).
        Never inferred from model names."""
        if self.architecture in MOE_ARCHITECTURES:
            return True
        if (self.disclosed and isinstance(self.active_parameter_count, int)
                and 0 < self.active_parameter_count < self.parameter_count):
            return True
        return False

    def is_large_for(self, band_minimum: str) -> bool:
        """Declared band at or above ``band_minimum`` — unknown is never large."""
        order = [b.value for b in ScaleBand]
        if self.band == ScaleBand.UNKNOWN.value:
            return False
        if band_minimum not in order:
            return False
        return order.index(self.band) >= order.index(band_minimum)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "parameter_count": self.parameter_count,
            "parameter_count_label": (
                f"{self.parameter_count:,}" if self.disclosed else "unknown"),
            "disclosed": self.disclosed,
            "active_parameter_count": self.active_parameter_count,
            "architecture": self.architecture or "unknown",
            "quantization": self.quantization or "unknown",
            "context_length": self.context_length,
            "model_version": self.model_version,
            "declared_by": self.declared_by,
            "band": self.band,
            "active_band": self.active_band,
            "moe": self.is_moe,
        }


#: Registry metadata key under which models carry their scale block.
SCALE_METADATA_KEY = "scale"


def scale_from_metadata(metadata: Optional[Mapping[str, Any]]) -> ParameterScale:
    """Extract the scale block from a model metadata mapping.

    Reads the nested ``scale`` mapping when present, else flat keys, so both
    ``metadata={"scale": {...}}`` and ``metadata={"parameter_count": ...}``
    work without inventing anything when neither exists.
    """
    metadata = metadata or {}
    block = metadata.get(SCALE_METADATA_KEY)
    if isinstance(block, Mapping):
        return ParameterScale.from_mapping(block)
    return ParameterScale.from_mapping(metadata)


# -- infrastructure honesty --------------------------------------------------

@dataclass(frozen=True)
class AvailabilityAxes:
    """The three separate availability axes for one massive-model claim."""

    can_route: bool
    forge_hosts: bool
    provider_offers: bool
    #: ``verified`` / ``configured`` / ``unknown`` — the provider-side state
    #: exactly as the fabric reports it, never upgraded.
    provider_state: str = "unknown"

    @property
    def honest_label(self) -> str:
        if self.forge_hosts and self.provider_offers:
            return "hosted-and-offered"
        if self.forge_hosts:
            return "forge-hosted"
        if self.can_route and self.provider_offers:
            return "routed-to-live-provider"
        if self.can_route:
            return "routing-configured-only"
        return "unavailable"


def describe_availability(*, can_route: bool, forge_hosts: bool,
                          provider_offers: bool,
                          provider_state: str = "unknown") -> str:
    """Render the never-conflated sentence for status surfaces."""
    axes = AvailabilityAxes(can_route=bool(can_route),
                            forge_hosts=bool(forge_hosts),
                            provider_offers=bool(provider_offers),
                            provider_state=provider_state or "unknown")
    scale_note = ""
    parts = [
        f"model routing: {'yes' if axes.can_route else 'no'}",
        f"weights hosted by Forge: {'yes' if axes.forge_hosts else 'no'}",
        f"provider currently offers it: {'yes' if axes.provider_offers else 'no'}"
        f" ({axes.provider_state})",
    ]
    return "; ".join(parts) + scale_note
