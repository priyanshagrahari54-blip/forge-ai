from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ModelDescriptor:
    """Describes a model target without claiming that it is installed or live.

    The catalog is an inventory schema. Runtime availability must come from
    provider configuration/health checks in Model Fabric.
    """

    provider: str
    model: str
    family: str
    capabilities: tuple[str, ...]
    source: str = "provider"
    status: str = "unverified"


# Provider ecosystems intentionally use family-level entries here. Concrete
# model IDs are discovered/configured at runtime so Forge does not fabricate
# availability or silently depend on stale model names.
PROVIDER_ECOSYSTEMS: dict[str, tuple[str, ...]] = {
    "openai": ("reasoning", "general", "coding", "vision", "audio", "embeddings"),
    "anthropic": ("reasoning", "general", "coding", "vision"),
    "google": ("reasoning", "general", "coding", "vision", "audio", "embeddings"),
    "mistral": ("general", "coding", "vision", "embeddings"),
    "cohere": ("general", "retrieval", "embeddings", "reranking"),
    "xai": ("reasoning", "general", "coding", "vision"),
    "groq": ("fast-inference", "general", "coding", "vision"),
    "deepseek": ("reasoning", "coding", "general"),
    "openrouter": ("multi-provider-routing", "general", "reasoning", "coding", "vision"),
    "together": ("open-models", "general", "coding", "embeddings"),
    "fireworks": ("open-models", "general", "coding", "vision"),
    "replicate": ("multimodal", "image", "video", "audio", "general"),
    "huggingface": ("open-models", "general", "coding", "vision", "audio", "embeddings"),
    "ollama": ("local", "general", "coding", "vision", "embeddings"),
    "lmstudio": ("local", "general", "coding", "vision"),
    "perplexity": ("research", "search", "general"),
    "ai21": ("general", "long-context"),
    "nvidia": ("open-models", "inference", "vision", "embeddings"),
    "azure_openai": ("enterprise", "reasoning", "general", "coding", "vision"),
    "aws_bedrock": ("multi-provider-routing", "enterprise", "general", "reasoning", "coding"),
    "cloudflare": ("edge-inference", "open-models", "embeddings"),
    "baseten": ("inference", "custom-deployment"),
    "modal": ("custom-inference", "serverless", "open-models"),
}


def catalog() -> list[ModelDescriptor]:
    """Return the provider-family catalog; runtime must verify concrete models."""
    return [
        ModelDescriptor(
            provider=provider,
            model=f"{provider}:runtime-discovery",
            family=capability,
            capabilities=(capability,),
            source="provider-family",
            status="unverified",
        )
        for provider, capabilities in sorted(PROVIDER_ECOSYSTEMS.items())
        for capability in capabilities
    ]


def catalog_snapshot() -> dict[str, object]:
    entries = catalog()
    return {
        "schema_version": 1,
        "provider_count": len(PROVIDER_ECOSYSTEMS),
        "family_entry_count": len(entries),
        "providers": sorted(PROVIDER_ECOSYSTEMS),
        "entries": [asdict(item) for item in entries],
        "availability_policy": "runtime-verified-only",
    }
