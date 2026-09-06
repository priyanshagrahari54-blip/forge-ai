"""Model Registry for the Model Fabric.

A ``Model`` is a declarative registry entry describing one provider/model
combination: its capabilities, context window, cost posture, and live health
and reliability state. The registry is the single source of truth the router
consults; providers are resolved separately by name.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

from forge.models.capabilities import is_capability
from forge.models.health import ModelHealth


@dataclass
class Model:
    """A model known to the fabric and the routing signals it exposes."""

    name: str
    provider: str
    capabilities: tuple[str, ...] = ()
    context_window: int = 4096
    max_output_tokens: int = 2048
    free: bool = True
    local: bool = True
    cost_per_token: float = 0.0
    latency_ms: float = 0.0
    reliability: float = 1.0
    available: bool = True
    #: Fallback models (e.g. the deterministic local no-op) are only selected
    #: when no regular model can serve the request.
    fallback: bool = False
    health: ModelHealth = field(default_factory=ModelHealth)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for capability in self.capabilities:
            if not is_capability(capability):
                raise ValueError(
                    f"Model {self.name!r} advertises unknown capability {capability!r}"
                )

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities

    def supports_all(self, capabilities: Iterable[str]) -> bool:
        return all(capability in self.capabilities for capability in capabilities)

    # Derived, declarative capability checks. These never hard-code provider
    # assumptions: they read from the model's declared capability tuple.

    @property
    def supports_tools(self) -> bool:
        return self.supports("tool_use")

    @property
    def supports_structured_output(self) -> bool:
        return self.supports("structured_output")

    @property
    def supports_vision(self) -> bool:
        return self.supports("vision")

    @property
    def supports_image_generation(self) -> bool:
        return self.supports("image_generation")

    @property
    def supports_audio(self) -> bool:
        return self.supports("audio") or self.supports("speech_to_text") or self.supports("text_to_speech")

    @property
    def supports_code(self) -> bool:
        return self.supports("coding")

    @property
    def supports_reasoning(self) -> bool:
        return self.supports("reasoning")

    @property
    def supports_browser(self) -> bool:
        return self.supports("browser")

    @property
    def supports_computer_use(self) -> bool:
        return self.supports("computer_use")

    @property
    def supports_streaming(self) -> bool:
        # Providers, not models, implement streaming; a model advertises it
        # explicitly through metadata so future adapters can be declarative.
        return bool(self.metadata.get("streaming", False))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "provider": self.provider,
            "capabilities": list(self.capabilities),
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "free": self.free,
            "local": self.local,
            "cost_per_token": self.cost_per_token,
            "latency_ms": self.latency_ms,
            "reliability": self.reliability,
            "available": self.available,
            "fallback": self.fallback,
            "health": self.health.to_dict(),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Model":
        health = ModelHealth(**data.get("health", {})) if isinstance(data.get("health"), dict) else ModelHealth()
        return cls(
            name=data["name"],
            provider=data["provider"],
            capabilities=tuple(data.get("capabilities", ())),
            context_window=int(data.get("context_window", 4096)),
            max_output_tokens=int(data.get("max_output_tokens", 2048)),
            free=bool(data.get("free", True)),
            local=bool(data.get("local", True)),
            cost_per_token=float(data.get("cost_per_token", 0.0)),
            latency_ms=float(data.get("latency_ms", 0.0)),
            reliability=float(data.get("reliability", 1.0)),
            available=bool(data.get("available", True)),
            fallback=bool(data.get("fallback", False)),
            health=health,
            metadata=dict(data.get("metadata", {})),
        )


class ModelRegistry:
    """Central, name-keyed registry of models for the fabric."""

    def __init__(self, models: Iterable[Model] | None = None) -> None:
        self._models: dict[str, Model] = {}
        for model in models or ():
            self.register(model)

    def register(self, model: Model) -> None:
        if not model.name:
            raise ValueError("Model name cannot be empty")
        if not model.provider:
            raise ValueError("Model provider cannot be empty")
        if model.context_window <= 0:
            raise ValueError("Model context_window must be positive")
        if model.name in self._models:
            raise ValueError(f"Model already registered: {model.name}")
        self._models[model.name] = model

    def replace(self, model: Model) -> None:
        """Register or overwrite a model by name."""
        if not model.name or not model.provider:
            raise ValueError("Model name and provider cannot be empty")
        self._models[model.name] = model

    def get(self, name: str) -> Model:
        try:
            return self._models[name]
        except KeyError:
            raise KeyError(f"Unknown model: {name}") from None

    def remove(self, name: str) -> None:
        if name not in self._models:
            raise KeyError(f"Unknown model: {name}")
        del self._models[name]

    def has(self, name: str) -> bool:
        return name in self._models

    def names(self) -> list[str]:
        return sorted(self._models)

    def list(self) -> list[Model]:
        return sorted(self._models.values(), key=lambda model: model.name)

    def by_capability(self, capability: str) -> list[Model]:
        return sorted(
            (model for model in self._models.values() if model.supports(capability)),
            key=lambda model: model.name,
        )

    def models_for_capabilities(self, capabilities: Iterable[str]) -> list[Model]:
        required = tuple(capabilities)
        return sorted(
            (model for model in self._models.values() if model.supports_all(required)),
            key=lambda model: model.name,
        )

    def available(self) -> list[Model]:
        return sorted(
            (model for model in self._models.values() if model.available),
            key=lambda model: model.name,
        )

    def capabilities(self) -> list[str]:
        return sorted(
            {
                capability
                for model in self._models.values()
                for capability in model.capabilities
            }
        )

    def snapshot(self) -> list[dict[str, Any]]:
        return [model.to_dict() for model in self.list()]

    def __len__(self) -> int:
        return len(self._models)

    def __iter__(self) -> Iterator[Model]:
        return iter(self.list())

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._models
