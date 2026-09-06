"""The Model Fabric: Forge's centralized model infrastructure.

Data flow::

    Agent -> ModelFabric -> FabricRouter -> ModelRegistry -> Provider
        -> Model -> ModelResponse -> Telemetry -> Router feedback

The fabric owns the registry of models and providers, the routing policy,
telemetry, and credential resolution. It performs no filesystem writes to the
repository and treats every provider result as untrusted data returned to the
caller.
"""
from __future__ import annotations

from time import perf_counter
from typing import Any

from forge.models.capabilities import Capability, TEXT_CAPABILITIES
from forge.models.config import FabricConfig
from forge.models.credentials import CredentialStore
from forge.models.feedback import RouterFeedback
from forge.models.policy import RoutingPolicy
from forge.models.provider import (
    LocalModelProvider,
    OllamaProvider,
    OpenAIProvider,
    Provider,
    ProviderInfo,
    ProviderRegistry,
)
from forge.models.registry import Model, ModelRegistry
from forge.models.request import ModelRequest, ModelResponse
from forge.models.router import FabricRouter, ModelInfo, ModelRouter, RouteDecision
from forge.models.telemetry import Telemetry


class ModelFabric:
    """Central entry point for routing and calling models."""

    def __init__(
        self,
        registry: ModelRegistry | None = None,
        providers: ProviderRegistry | None = None,
        router: FabricRouter | None = None,
        policy: RoutingPolicy | None = None,
        telemetry: Telemetry | None = None,
        credentials: CredentialStore | None = None,
        config: FabricConfig | None = None,
    ) -> None:
        self.config = config
        self.registry = registry if registry is not None else ModelRegistry()
        self.providers = providers if providers is not None else ProviderRegistry()
        self.policy = policy if policy is not None else (config.policy if config else RoutingPolicy())
        self.telemetry = telemetry if telemetry is not None else Telemetry(
            enabled=config.telemetry_enabled if config else True,
            sink_path=config.telemetry_path if config else None,
        )
        self.credentials = credentials if credentials is not None else CredentialStore()
        self.router = router if router is not None else FabricRouter(self.registry, self.policy, self.telemetry)

    # -- construction ----------------------------------------------------

    @classmethod
    def from_defaults(cls, config: FabricConfig | None = None) -> "ModelFabric":
        """Build the standard fabric: local fallback + Ollama + optional OpenAI.

        Nothing touches the network at construction time; Ollama availability
        is learned from actual calls and reflected through health feedback.
        """
        config = config or FabricConfig.from_dict({})
        config.validate()
        registry = ModelRegistry()
        providers = ProviderRegistry()
        credentials = CredentialStore()

        if config.local_enabled:
            providers.register(
                "local",
                LocalModelProvider(),
                ProviderInfo(name="local", kind="fallback", local=True, free=True, capabilities=TEXT_CAPABILITIES),
            )
            registry.register(Model(
                name="local-fallback",
                provider="local",
                capabilities=TEXT_CAPABILITIES,
                context_window=4096,
                free=True,
                local=True,
                fallback=True,
                metadata={"description": "Deterministic offline fallback; refuses arbitrary synthesis."},
            ))

        if config.ollama_enabled:
            ollama = OllamaProvider(model=config.ollama_model, url=config.ollama_url)
            capabilities = _ollama_capabilities(config.ollama_model, config.ollama_context_window)
            providers.register(
                "ollama",
                ollama,
                ProviderInfo(
                    name="ollama",
                    kind="local",
                    local=True,
                    free=True,
                    capabilities=capabilities,
                    endpoint=config.ollama_url,
                    model=config.ollama_model,
                ),
            )
            registry.register(Model(
                name=f"ollama/{config.ollama_model}",
                provider="ollama",
                capabilities=capabilities,
                context_window=config.ollama_context_window,
                free=True,
                local=True,
                metadata={"description": "Local Ollama model."},
            ))

        if config.openai_enabled and credentials.configured("openai"):
            openai = OpenAIProvider(model=config.openai_model, api_key=credentials.get("openai"))
            providers.register(
                "openai",
                openai,
                ProviderInfo(name="openai", kind="remote", local=False, free=False, capabilities=TEXT_CAPABILITIES),
            )
            registry.register(Model(
                name=f"openai/{config.openai_model}",
                provider="openai",
                capabilities=TEXT_CAPABILITIES,
                context_window=128000,
                free=False,
                local=False,
                metadata={"description": "Optional remote provider; enabled only with a configured key."},
            ))

        for extra in config.extra_models:
            registry.register(Model.from_dict(extra))

        return cls(
            registry=registry,
            providers=providers,
            policy=config.policy,
            telemetry=Telemetry(enabled=config.telemetry_enabled, sink_path=config.telemetry_path),
            credentials=credentials,
            config=config,
        )

    # -- registration ----------------------------------------------------

    def register_model(self, model: Model) -> None:
        if not self.providers.has(model.provider):
            raise ValueError(f"Provider {model.provider!r} must be registered before model {model.name!r}")
        self.registry.register(model)

    def register_provider(self, name: str, provider: Provider, info: ProviderInfo | None = None) -> None:
        self.providers.register(name, provider, info)

    # -- routing and generation ------------------------------------------

    def route(self, request: ModelRequest | str, *, policy: RoutingPolicy | None = None) -> RouteDecision:
        if isinstance(request, str):
            request = ModelRequest(prompt=request, capability=self._default_capability())
        return self.router.route(request, policy=policy)

    def generate(self, request: ModelRequest | str) -> ModelResponse:
        """Route and call a provider, returning a structured ``ModelResponse``.

        Never raises for routing/provider failures: failures are recorded as
        feedback and telemetry, and deterministic failover moves down the
        candidate chain (ending with the deterministic local fallback). The
        caller decides how to handle a ``success=False`` result.
        """
        if isinstance(request, str):
            request = ModelRequest(prompt=request, capability=self._default_capability())
        started = perf_counter()
        decision = self.route(request)
        if not decision.chosen:
            error = decision.error or "no model available for this request"
            self.telemetry.record("error", trace_id=request.trace_id, capability=request.capability, error=error)
            return ModelResponse.failure(error, request_id=request.trace_id)

        chain: list[str] = []
        for name in decision.candidates:
            if name not in chain:
                chain.append(name)
        if decision.model and decision.model.name not in chain:
            chain.insert(0, decision.model.name)

        last_error = ""
        for model_name in chain:
            try:
                model = self.registry.get(model_name)
            except KeyError:
                continue
            provider = self.providers.get(model.provider) if self.providers.has(model.provider) else None
            if provider is None:
                # A model whose provider is not registered can never succeed.
                model.available = False
                last_error = f"provider {model.provider!r} for model {model.name!r} is not registered"
                self.record_feedback(
                    model=model.name, provider=model.provider, capability=request.capability,
                    success=False, error=last_error,
                )
                continue

            try:
                result = provider.generate(request.prompt, context=request.context, task=request.task)
            except Exception as exc:  # provider failures are feedback, not crashes
                last_error = str(exc)
                self.record_feedback(
                    model=model.name, provider=model.provider, capability=request.capability,
                    success=False, latency_ms=(perf_counter() - started) * 1000.0,
                    complexity=request.complexity, error=last_error,
                )
                continue

            response = ModelResponse(
                text=result.text,
                model=model.name,
                provider=model.provider,
                request_id=request.trace_id,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                latency_ms=(perf_counter() - started) * 1000.0,
                finish_reason="stop",
            )
            self.record_feedback(
                model=model.name, provider=model.provider, capability=request.capability,
                success=True, latency_ms=response.latency_ms,
                input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                complexity=request.complexity,
            )
            self.telemetry.record(
                "response",
                trace_id=request.trace_id,
                model=model.name,
                provider=model.provider,
                success=True,
                latency_ms=response.latency_ms,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
            )
            return response

        self.telemetry.record("error", trace_id=request.trace_id, capability=request.capability, error=last_error)
        return ModelResponse.failure(last_error or "no model available", request_id=request.trace_id)

    def record_feedback(self, model: str | None = None, *, provider: str = "", capability: str = "",
                        success: bool = True, latency_ms: float | None = None, input_tokens: int = 0,
                        output_tokens: int = 0, complexity: float | None = None, error: str = "",
                        feedback: RouterFeedback | None = None) -> None:
        """Apply router feedback to health/reliability/latency and telemetry."""
        if feedback is None:
            feedback = RouterFeedback(
                model=model or "",
                provider=provider,
                capability=capability,
                success=success,
                latency_ms=latency_ms,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                complexity=complexity,
                error=error,
            )
        self.router.record(
            feedback.model,
            feedback.success,
            feedback.latency_ms,
            capability=feedback.capability,
            task_complexity=feedback.complexity,
            input_tokens=feedback.input_tokens,
            output_tokens=feedback.output_tokens,
            error=feedback.error,
        )

    # -- introspection ---------------------------------------------------

    def _default_capability(self) -> str:
        return self.config.default_capability if self.config else Capability.CODING.value

    def models(self) -> list[Model]:
        return self.registry.list()

    def capabilities(self) -> list[str]:
        return self.registry.capabilities()

    def health(self) -> dict[str, dict[str, Any]]:
        return {
            model.name: {
                "health": model.health.status,
                "reliability": model.reliability,
                "latency_ms": model.latency_ms,
                "available": model.available,
                "free": model.free,
                "local": model.local,
            }
            for model in self.registry.list()
        }

    def legacy_router(self) -> ModelRouter:
        """Return a backward-compatible ``ModelRouter`` view of this fabric.

        Useful for integrations that still consume the legacy router API.
        """
        infos: list[ModelInfo] = []
        for model in self.registry.list():
            provider = self.providers.get(model.provider) if self.providers.has(model.provider) else None
            infos.append(ModelInfo(
                name=model.name,
                capability=model.capabilities[0] if model.capabilities else "",
                available=model.available,
                context_size=model.context_window,
                historical_success_rate=model.reliability,
                latency=model.latency_ms / 1000.0,
                failure_rate=1.0 - model.reliability,
                cost_per_token=model.cost_per_token,
                free=model.free,
                provider=provider,
                capabilities=model.capabilities,
            ))
        return ModelRouter(infos)

    def snapshot(self) -> dict[str, Any]:
        return {
            "models": self.registry.snapshot(),
            "providers": self.providers.snapshot(),
            "policy": self.policy.to_dict(),
            "capabilities": self.registry.capabilities(),
            "telemetry_events": len(self.telemetry),
            "router_history": list(self.router.history[-50:]),
        }

    def to_dict(self) -> dict[str, Any]:
        return self.snapshot()


def _ollama_capabilities(model_name: str, context_window: int) -> tuple[str, ...]:
    capabilities = list(TEXT_CAPABILITIES)
    if OllamaProvider.supports_vision(model_name):
        capabilities.append(Capability.VISION.value)
    if context_window >= 32768:
        capabilities.append(Capability.LONG_CONTEXT.value)
    return tuple(capabilities)
