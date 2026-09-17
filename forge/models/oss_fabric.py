"""Bridge real OSS/Hugging Face model IDs into Model Fabric safely.

Catalogued models are registered as *known but unavailable* until a real
runtime/provider adapter proves that the exact model can be invoked. This
keeps model discovery separate from model availability and prevents routing
to an imaginary or unconfigured model.
"""
from __future__ import annotations

from typing import Iterable

from forge.models.oss_registry import OSSModel
from forge.models.registry import Model, ModelRegistry


_TAG_CAPABILITIES = {
    "reasoning": "reasoning",
    "coding": "coding",
    "vision": "vision",
    "audio": "audio",
    "speech-to-text": "speech_to_text",
    "text-to-speech": "text_to_speech",
    "embeddings": "structured_output",
    "retrieval": "research",
    "research": "research",
}


def capabilities_for_oss_model(model: OSSModel) -> tuple[str, ...]:
    """Map only known tags to canonical Model Fabric capabilities."""
    values: list[str] = []
    for tag in model.tags:
        capability = _TAG_CAPABILITIES.get(tag.lower())
        if capability and capability not in values:
            values.append(capability)
    return tuple(values)


def model_from_oss(
    model: OSSModel,
    *,
    runtime_provider: str = "huggingface",
    available: bool = False,
    local: bool = False,
) -> Model:
    """Convert a real catalog entry to a Fabric model without claiming liveness."""
    return Model(
        name=model.model_id,
        provider=runtime_provider,
        capabilities=capabilities_for_oss_model(model),
        context_window=4096,
        max_output_tokens=2048,
        free=True,
        local=local,
        available=available,
        capability_status={capability: "declared" for capability in capabilities_for_oss_model(model)},
        metadata={
            "source": model.source,
            "catalog_status": model.status,
            "catalog_provider": model.provider,
            "model_id": model.model_id,
            "license": model.license,
            "tags": list(model.tags),
            "runtime_verified": bool(available),
            "availability_policy": "runtime-verified-only",
        },
    )


def register_oss_catalog(
    registry: ModelRegistry,
    models: Iterable[OSSModel],
    *,
    runtime_provider: str = "huggingface",
) -> int:
    """Register concrete OSS IDs as non-live inventory entries.

    Returns the number of newly registered IDs. Existing names are preserved
    so runtime health/availability state is never overwritten by discovery.
    """
    added = 0
    for item in models:
        if not item.model_id or registry.has(item.model_id):
            continue
        registry.register(model_from_oss(item, runtime_provider=runtime_provider))
        added += 1
    return added


def activate_oss_model(
    registry: ModelRegistry,
    model_id: str,
    *,
    context_window: int | None = None,
    max_output_tokens: int | None = None,
    capabilities: Iterable[str] | None = None,
    local: bool | None = None,
) -> Model:
    """Mark an exact catalogued model usable only after a real runtime probe.

    Provider adapters should call this after they have verified the exact model
    ID. It never creates a missing model entry.
    """
    model = registry.get(model_id)
    model.available = True
    model.metadata["runtime_verified"] = True
    model.metadata["activation_reason"] = "explicit-runtime-verification"
    if context_window is not None:
        model.context_window = max(1, int(context_window))
    if max_output_tokens is not None:
        model.max_output_tokens = max(1, int(max_output_tokens))
    if capabilities is not None:
        model.capabilities = tuple(dict.fromkeys(capabilities))
        model.capability_status = {capability: "verified" for capability in model.capabilities}
    if local is not None:
        model.local = bool(local)
    return model
