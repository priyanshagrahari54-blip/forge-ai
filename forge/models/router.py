from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol


@dataclass(frozen=True)
class ModelInfo:
    """A registered model descriptor."""

    name: str
    provider: str
    capability: str
    available: bool = False


@dataclass(frozen=True)
class ModelRequest:
    """Provider-agnostic model request."""

    prompt: str
    capability: str
    max_tokens: int = 1000
    temperature: float = 0.0
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelResponse:
    """Provider-agnostic model response."""

    text: str
    model: str
    provider: str
    tokens_used: int = 0
    success: bool = True
    error: str = ""


class ModelProvider(Protocol):
    """Protocol for model providers.

    Any object implementing ``name``, ``capabilities``, and ``generate``
    satisfies this protocol.
    """

    @property
    def name(self) -> str:
        """Human-readable provider name."""

    @property
    def capabilities(self) -> frozenset[str]:
        """Set of capability identifiers this provider supports."""

    def generate(self, request: ModelRequest) -> ModelResponse:
        """Generate a response for the given request."""


class ModelRouter:
    """Capability-based model router with fallback chain.

    Routes requests to registered providers based on the requested
    capability. If the primary provider fails, falls back to the next
    available provider that supports the capability.
    """

    def __init__(
        self,
        providers: list[ModelProvider] | None = None,
        availability: Callable[[ModelProvider], bool] | None = None,
    ) -> None:
        self._providers: list[ModelProvider] = list(providers or [])
        self._availability = availability or (lambda p: True)

    @property
    def providers(self) -> tuple[ModelProvider, ...]:
        return tuple(self._providers)

    def register(self, provider: ModelProvider) -> None:
        """Register a model provider."""
        self._providers.append(provider)

    def add_provider(self, provider: ModelProvider) -> None:
        """Alias for register."""
        self._providers.append(provider)

    def providers_for(self, capability: str) -> list[ModelProvider]:
        """Return providers supporting the given capability."""
        return [
            provider
            for provider in self._providers
            if capability in provider.capabilities
            and self._availability(provider)
        ]

    def route(self, request: ModelRequest) -> ModelResponse:
        """Route a request to the best available provider.

        Tries each provider in registration order. Returns the first
        successful response, or a failure response if none succeed.
        """
        candidates = self.providers_for(request.capability)

        if not candidates:
            return ModelResponse(
                text="",
                model="",
                provider="",
                success=False,
                error=(
                    f"No provider available for capability: "
                    f"{request.capability}"
                ),
            )

        errors: list[str] = []
        for provider in candidates:
            try:
                response = provider.generate(request)
                if response.success:
                    return response
                if response.error:
                    errors.append(
                        f"{provider.name}: {response.error}"
                    )
            except Exception as exc:
                errors.append(f"{provider.name}: {exc}")

        return ModelResponse(
            text="",
            model="",
            provider="",
            success=False,
            error="All providers failed: " + "; ".join(errors),
        )

    def select(self, capability: str) -> ModelInfo | None:
        """Return the first available model info for a capability."""
        providers = self.providers_for(capability)
        if not providers:
            return None
        provider = providers[0]
        return ModelInfo(
            name=provider.name,
            provider=provider.name,
            capability=capability,
            available=True,
        )
