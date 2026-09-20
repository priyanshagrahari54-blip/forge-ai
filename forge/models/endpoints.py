"""Endpoint description for a self-hosted, OpenAI-compatible model server.

Forge can drive several of an operator's own servers at once. An endpoint is
one base URL serving one model id; endpoints that serve the *same* model id are
pooled, so a request that hits a quota or rate limit on one server is retried on
the next. Endpoints serving different models are registered as different models,
which is what lets routing prefer the more powerful one.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class LocalEndpoint:
    """One self-hosted OpenAI-compatible server."""

    url: str
    model: str = ""
    #: Operator-declared capabilities for this endpoint's model.
    capabilities: tuple[str, ...] = ()
    context_window: int = 8192
    timeout: float = 120.0
    api_key: str = ""
    #: Relative power of the model behind this endpoint. Routing prefers a
    #: higher tier among models that can do the job; 0 means "undeclared" and
    #: leaves ordering exactly as it was before tiers existed.
    tier: int = 0
    #: Human label used in telemetry ("gpu-box", "office-box", "cloud").
    label: str = ""
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.url).strip():
            raise ValueError("a local endpoint requires a url")

    @property
    def display(self) -> str:
        return self.label or self.url

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "model": self.model,
            "capabilities": list(self.capabilities),
            "context_window": self.context_window,
            "timeout": self.timeout,
            "tier": self.tier,
            "label": self.label,
            # Never serialize the key itself.
            "api_key_configured": bool(self.api_key),
        }
