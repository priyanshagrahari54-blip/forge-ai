"""Official provider/model resource links used by Forge UIs and diagnostics.

Links are public metadata only. They never contain API keys or imply that a
provider is configured, reachable, or available for inference.
"""
from __future__ import annotations

from typing import Any


_PROVIDER_LINKS: dict[str, dict[str, str]] = {
    "openai": {"website": "https://openai.com/", "api": "https://platform.openai.com/", "docs": "https://platform.openai.com/docs/"},
    "anthropic": {"website": "https://www.anthropic.com/", "api": "https://console.anthropic.com/", "docs": "https://docs.anthropic.com/"},
    "google": {"website": "https://ai.google.dev/", "api": "https://aistudio.google.com/", "docs": "https://ai.google.dev/gemini-api/docs"},
    "gemini": {"website": "https://ai.google.dev/", "api": "https://aistudio.google.com/", "docs": "https://ai.google.dev/gemini-api/docs"},
    "mistral": {"website": "https://mistral.ai/", "api": "https://console.mistral.ai/", "docs": "https://docs.mistral.ai/"},
    "cohere": {"website": "https://cohere.com/", "api": "https://dashboard.cohere.com/", "docs": "https://docs.cohere.com/"},
    "xai": {"website": "https://x.ai/", "api": "https://console.x.ai/", "docs": "https://docs.x.ai/"},
    "groq": {"website": "https://groq.com/", "api": "https://console.groq.com/", "docs": "https://console.groq.com/docs"},
    "deepseek": {"website": "https://www.deepseek.com/", "api": "https://platform.deepseek.com/", "docs": "https://api-docs.deepseek.com/"},
    "openrouter": {"website": "https://openrouter.ai/", "api": "https://openrouter.ai/", "docs": "https://openrouter.ai/docs"},
    "together": {"website": "https://www.together.ai/", "api": "https://api.together.ai/", "docs": "https://docs.together.ai/"},
    "fireworks": {"website": "https://fireworks.ai/", "api": "https://fireworks.ai/", "docs": "https://docs.fireworks.ai/"},
    "replicate": {"website": "https://replicate.com/", "api": "https://replicate.com/", "docs": "https://replicate.com/docs"},
    "huggingface": {"website": "https://huggingface.co/", "api": "https://huggingface.co/", "docs": "https://huggingface.co/docs"},
    "ollama": {"website": "https://ollama.com/", "api": "https://ollama.com/", "docs": "https://docs.ollama.com/"},
    "lmstudio": {"website": "https://lmstudio.ai/", "api": "https://lmstudio.ai/", "docs": "https://lmstudio.ai/docs"},
    "perplexity": {"website": "https://www.perplexity.ai/", "api": "https://www.perplexity.ai/", "docs": "https://docs.perplexity.ai/"},
    "ai21": {"website": "https://www.ai21.com/", "api": "https://studio.ai21.com/", "docs": "https://docs.ai21.com/"},
    "nvidia": {"website": "https://www.nvidia.com/en-us/ai/", "api": "https://build.nvidia.com/", "docs": "https://docs.nvidia.com/"},
    "azure_openai": {"website": "https://azure.microsoft.com/products/ai-services/openai-service", "api": "https://portal.azure.com/", "docs": "https://learn.microsoft.com/azure/ai-services/openai/"},
    "aws_bedrock": {"website": "https://aws.amazon.com/bedrock/", "api": "https://aws.amazon.com/console/", "docs": "https://docs.aws.amazon.com/bedrock/"},
    "cloudflare": {"website": "https://www.cloudflare.com/developer-platform/products/workers-ai/", "api": "https://dash.cloudflare.com/", "docs": "https://developers.cloudflare.com/workers-ai/"},
    "baseten": {"website": "https://www.baseten.co/", "api": "https://app.baseten.co/", "docs": "https://docs.baseten.co/"},
    "modal": {"website": "https://modal.com/", "api": "https://modal.com/", "docs": "https://modal.com/docs/"},
}


def provider_links(name: str) -> dict[str, str]:
    """Return public official links for a provider, without inventing status."""
    key = name.strip().lower().replace("-", "_").replace(" ", "_")
    links = _PROVIDER_LINKS.get(key)
    if links:
        return dict(links)
    return {}


def enrich_provider_info(info: dict[str, Any]) -> dict[str, Any]:
    """Add official links to an existing provider metadata record."""
    result = dict(info)
    links = provider_links(str(result.get("name", "")))
    if links:
        result["links"] = links
    return result


def provider_link_catalog() -> list[dict[str, Any]]:
    """Return the complete public provider-link catalogue for UI discovery."""
    return [
        {"name": name, "links": dict(links)}
        for name, links in sorted(_PROVIDER_LINKS.items())
    ]
