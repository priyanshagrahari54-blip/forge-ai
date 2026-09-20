"""One hundred multimodal specialists, registered only where they can work.

Forge already has a 1,000+ text-specialist fleet. Multimodal work is different:
a specialist that reads images, transcribes speech, speaks, or draws needs a
model that actually advertises ``vision``, ``speech_to_text``,
``text_to_speech`` or ``image_generation``. A text-only model cannot answer
about a photograph, and letting one try would produce a confident wrong answer.

So this module defines the full set of 100 multimodal specialists — 25 per
modality — and registers exactly the ones whose capability is backed by a real
model in the fabric (see :mod:`forge.models.multimodal_bridge`, which bridges
self-hosted endpoints with no cloud credential). Specialists whose modality has
no endpoint are reported as ``MISSING`` with the exact environment variable
that enables them: defined, visible, and honestly not claimed as working.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from forge.agents.frontier_fleet import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    DEFAULT_TEMPERATURE,
    FrontierModelAgentExecutor,
)
from forge.agents.registry import AgentRegistration, AgentRegistry


@dataclass(frozen=True)
class Modality:
    """One multimodal capability and the fleet family that serves it."""

    family: str
    #: Bridge kind in :mod:`forge.models.multimodal_bridge`.
    kind: str
    capability: str
    #: The specialist's role, which the bridge reads to pick the endpoint.
    role: str
    labels: tuple[str, ...]
    specialties: tuple[str, ...]

    @property
    def size(self) -> int:
        return len(self.specialties)


MODALITIES: tuple[Modality, ...] = (
    Modality(
        family="vision", kind="vision", capability="vision", role="vision",
        labels=("vision", "multimodal", "engineering"),
        specialties=(
            "ocr-extraction", "ui-inspection", "screenshot-diff", "chart-reading",
            "document-layout", "diagram-parsing", "defect-detection",
            "accessibility-audit", "visual-qa", "image-captioning", "handwriting",
            "table-extraction", "form-fields", "signature-check", "logo-detection",
            "scene-description", "object-counting", "color-analysis",
            "receipt-parsing", "whiteboard-notes", "map-reading", "gauge-reading",
            "packaging-check", "safety-inspection", "photo-metadata-review",
        ),
    ),
    Modality(
        family="image-generation", kind="imagine", capability="image_generation",
        role="image-generation",
        labels=("image_generation", "multimodal", "design"),
        specialties=(
            "prompt-craft", "brand-assets", "illustration", "icon-set", "texture",
            "mockup", "infographic", "poster", "style-transfer", "upscaling",
            "background-removal", "sprite-sheet", "concept-art", "ui-mock",
            "product-shot", "avatar", "banner", "pattern", "photo-restore",
            "layout-render", "diagram-render", "chart-render", "thumbnail",
            "album-art", "ad-creative",
        ),
    ),
    Modality(
        family="speech-to-text", kind="transcribe", capability="speech_to_text",
        role="speech-to-text",
        labels=("speech_to_text", "audio", "multimodal"),
        specialties=(
            "hindi-asr", "english-asr", "hinglish-asr", "code-switch-asr",
            "domain-terms", "punctuation-restore", "timestamp-align",
            "speaker-labels", "low-resource", "accent-robust", "noise-robust",
            "long-form", "meeting-notes", "medical-terms", "legal-terms",
            "numerals", "named-entities", "diarization-merge",
            "transcript-verification", "translation-ready", "interview-transcript",
            "lecture-transcript", "call-transcript", "voicemail-transcript",
            "subtitle-source",
        ),
    ),
    Modality(
        family="text-to-speech", kind="speak", capability="text_to_speech",
        role="text-to-speech",
        labels=("text_to_speech", "audio", "multimodal"),
        specialties=(
            "hindi-tts", "english-tts", "hinglish-tts", "prosody-shaping",
            "narration", "dubbing", "accessibility-readout", "latency-tuning",
            "voice-consistency", "ssml-review", "number-reading", "acronym-reading",
            "emotion-tone", "pacing", "chunking", "long-form-narration",
            "alert-announcements", "ivr-prompts", "audiobook", "pronunciation-fix",
            "language-switch", "code-switch-tts", "emphasis-control",
            "pause-control", "audio-branding",
        ),
    ),
)

#: Every multimodal specialist name, in a stable order (100 by default).
SPECIALIST_NAMES: tuple[str, ...] = tuple(
    f"{modality.family}-{specialty}"
    for modality in MODALITIES
    for specialty in modality.specialties
)

#: The capability each specialist requires from a model.
SPECIALIST_CAPABILITY: dict[str, str] = {
    f"{modality.family}-{specialty}": modality.capability
    for modality in MODALITIES
    for specialty in modality.specialties
}


@dataclass(frozen=True)
class ModalityReport:
    """What the fabric can really serve for one modality."""

    family: str
    capability: str
    defined: int
    registered: int
    models: tuple[str, ...] = ()
    requirement: str = ""
    reason: str = ""
    what_is_implemented: str = ""

    @property
    def status(self) -> str:
        if self.registered:
            return "READY"
        return "MISSING"

    def to_dict(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "capability": self.capability,
            "status": self.status,
            "defined": self.defined,
            "registered": self.registered,
            "models": list(self.models),
            "reason": self.reason,
            "requirement": self.requirement,
            "what_is_implemented": self.what_is_implemented,
        }


def _gap_index(env: Mapping[str, str] | None) -> dict[str, Any]:
    from forge.models.multimodal_bridge import multimodal_gaps

    return {gap.capability: gap for gap in multimodal_gaps(env)}


def modality_models(fabric: Any, capability: str) -> tuple[str, ...]:
    """Registered, available models that advertise *capability*.

    Fallbacks are excluded: the deterministic no-op model must never be counted
    as proof that a modality is served.
    """
    registry = getattr(fabric, "registry", None)
    if registry is None:
        return ()
    models = registry.by_capability(capability)
    return tuple(sorted(model.name for model in models
                        if model.available and not model.fallback))


def multimodal_readiness(fabric: Any, *,
                         env: Mapping[str, str] | None = None
                         ) -> tuple[ModalityReport, ...]:
    """Per-modality availability, with the exact requirement when missing."""
    gaps = _gap_index(env)
    reports: list[ModalityReport] = []
    for modality in MODALITIES:
        models = modality_models(fabric, modality.capability)
        gap = gaps.get(modality.capability)
        reports.append(ModalityReport(
            family=modality.family, capability=modality.capability,
            defined=modality.size,
            registered=modality.size if models else 0,
            models=models,
            requirement=getattr(gap, "requirement", ""),
            reason=getattr(gap, "reason", "") if not models else "",
            what_is_implemented=getattr(gap, "what_is_implemented", ""),
        ))
    return tuple(reports)


def build_multimodal_fleet(fabric: Any, *, env: Mapping[str, str] | None = None,
                           max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
                           temperature: float | None = DEFAULT_TEMPERATURE,
                           ) -> tuple[AgentRegistry, dict[str, Any]]:
    """Register the multimodal specialists that have a real model behind them.

    Returns the registry plus a report that names, per modality, how many
    specialists exist, how many are registered, and — when none are — the exact
    external requirement that would enable them.
    """
    reports = multimodal_readiness(fabric, env=env)
    available = {report.capability: report for report in reports}
    registrations: list[AgentRegistration] = []
    for modality in MODALITIES:
        report = available[modality.capability]
        if not report.registered:
            continue
        preferred = report.models[0]
        for specialty in modality.specialties:
            name = f"{modality.family}-{specialty}"
            registrations.append(AgentRegistration(
                name, modality.role,
                FrontierModelAgentExecutor(
                    name, modality.role, preferred,
                    (modality.capability, *modality.labels[1:]), fabric,
                    declared_capabilities=modality.labels,
                    max_output_tokens=max_output_tokens,
                    temperature=temperature),
                modality.labels,
            ))
    registry = AgentRegistry(registrations)
    report = {
        "schema_version": 1,
        "defined": len(SPECIALIST_NAMES),
        "registered": len(registrations),
        "ready_modalities": [r.family for r in reports if r.registered],
        "missing_modalities": [r.family for r in reports if not r.registered],
        "modalities": [r.to_dict() for r in reports],
        "note": ("specialists are registered only for modalities a real model "
                 "advertises; a missing modality lists the exact endpoint that "
                 "enables it and is never counted as working"),
    }
    return registry, report


def extend_registry_with_multimodal_fleet(
        registry: AgentRegistry, fabric: Any, *,
        env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Add the multimodal specialists to an existing registry."""
    fleet, report = build_multimodal_fleet(fabric, env=env)
    for name in fleet.names():
        registry.register(fleet.get(name))
    return report
