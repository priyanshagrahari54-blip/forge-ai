"""Real open-model registry and Hugging Face discovery for Forge.

This module deliberately separates *real model IDs* from runtime availability.
A discovered model is catalogued only; Model Fabric decides whether it is
reachable, configured, downloaded, or live.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from urllib.parse import urlencode
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class OSSModel:
    model_id: str
    provider: str
    source: str
    status: str = "catalogued"
    license: str | None = None
    tags: tuple[str, ...] = ()


# Concrete IDs intentionally come from public model registries/provider docs.
# They are names, not claims that Forge currently has access to them.
VERIFIED_SEEDS: tuple[OSSModel, ...] = (
    OSSModel("deepseek-ai/DeepSeek-V4.1-Flash", "deepseek", "huggingface", tags=("reasoning", "coding", "vision")),
    OSSModel("deepseek-ai/DeepSeek-V4-Pro", "deepseek", "huggingface", tags=("reasoning", "coding")),
    OSSModel("Qwen/Qwen3.8-27B", "qwen", "huggingface", tags=("reasoning", "coding", "vision")),
    OSSModel("Qwen/Qwen3.8-Flash-Next", "qwen", "huggingface", tags=("reasoning", "coding", "vision")),
    OSSModel("zai-org/GLM-5.3-Flash", "zai", "huggingface", tags=("reasoning", "coding", "vision")),
    OSSModel("openbmb/MiniCPM5-2B", "openbmb", "huggingface", tags=("general",)),
    OSSModel("MiniMaxAI/MiniMax-H3", "minimax", "huggingface", tags=("reasoning", "vision", "video")),
    OSSModel("m-a-p/YuE2-3B", "map", "huggingface", tags=("audio", "music")),
    OSSModel("microsoft/VibeVoice-ASR-Streaming-7B", "microsoft", "huggingface", tags=("audio", "speech-to-text")),
    OSSModel("google-bert/bert-base-uncased", "google", "huggingface", tags=("language",)),
    OSSModel("openai/clip-vit-base-patch32", "openai", "huggingface", tags=("vision", "embeddings")),
    OSSModel("distilbert/distilbert-base-uncased", "huggingface", "huggingface", tags=("language",)),
    OSSModel("sentence-transformers/all-MiniLM-L6-v2", "sentence-transformers", "huggingface", tags=("embeddings",)),
    OSSModel("mistralai/Mistral-7B-Instruct-v0.3", "mistral", "huggingface", license="Apache-2.0", tags=("general", "coding")),
    OSSModel("mistralai/Mistral-Nemo-Instruct-2407", "mistral", "huggingface", license="Apache-2.0", tags=("general", "coding")),
    OSSModel("mistralai/Mixtral-8x7B-Instruct-v0.1", "mistral", "huggingface", license="Apache-2.0", tags=("reasoning", "coding")),
    OSSModel("mistralai/Mixtral-8x22B-Instruct-v0.1", "mistral", "huggingface", license="Apache-2.0", tags=("reasoning", "coding")),
    OSSModel("mistralai/Ministral-8B-Instruct-2410", "mistral", "huggingface", tags=("general", "coding")),
    OSSModel("mistralai/Ministral-3-8B-Instruct-2512", "mistral", "huggingface", license="Apache-2.0", tags=("general", "vision")),
    OSSModel("mistralai/Mistral-Large-3-675B-Instruct-2512", "mistral", "huggingface", license="Apache-2.0", tags=("reasoning", "coding", "vision")),
)


def _normalise(item: dict) -> OSSModel:
    model_id = str(item.get("id", "")).strip()
    tags = tuple(str(x) for x in item.get("tags", ()) if x)
    return OSSModel(
        model_id=model_id,
        provider=model_id.split("/", 1)[0] if "/" in model_id else "huggingface",
        source="huggingface-api",
        status="discovered",
        license=item.get("library_name") if False else None,
        tags=tags,
    )


def discover_huggingface_models(*, limit: int = 1000, search: str | None = None,
                                timeout: float = 20.0) -> list[OSSModel]:
    """Fetch real public Hugging Face model IDs; never invents identifiers.

    The API is queried at runtime, so the registry can grow beyond 1000 as the
    ecosystem changes. Results are still only catalogued until Model Fabric
    verifies access/runtime compatibility.
    """
    if limit < 1 or limit > 5000:
        raise ValueError("limit must be between 1 and 5000")
    params = {"limit": str(limit), "sort": "downloads", "direction": "-1"}
    if search:
        params["search"] = search
    url = "https://huggingface.co/api/models?" + urlencode(params)
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "Forge-AI/1.0"})
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    result: list[OSSModel] = []
    seen: set[str] = set()
    for raw in payload:
        item = _normalise(raw)
        if item.model_id and item.model_id not in seen:
            seen.add(item.model_id)
            result.append(item)
    return result


def oss_catalog(*, discovered: list[OSSModel] | None = None,
                minimum: int = 1000) -> list[OSSModel]:
    """Merge verified seeds with real discovered IDs, deduplicated by ID."""
    if minimum < 1:
        raise ValueError("minimum must be positive")
    values: list[OSSModel] = []
    seen: set[str] = set()
    for item in (*VERIFIED_SEEDS, *(discovered or ())):
        if item.model_id and item.model_id not in seen:
            seen.add(item.model_id)
            values.append(item)
    return values


def oss_catalog_snapshot(discovered: list[OSSModel] | None = None) -> dict[str, object]:
    entries = oss_catalog(discovered=discovered)
    return {
        "schema_version": 1,
        "real_model_count": len(entries),
        "minimum_target": 1000,
        "meets_1000_target": len(entries) >= 1000,
        "availability_policy": "catalogued-is-not-live",
        "source_policy": "public-provider-or-Hugging-Face-ID-only",
        "entries": [asdict(x) for x in entries],
    }
