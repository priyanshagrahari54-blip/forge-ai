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

import inspect
from time import perf_counter
from typing import Any, Iterator

from forge.models.capabilities import Capability, TEXT_CAPABILITIES
from forge.models.config import FabricConfig
from forge.models.credentials import CredentialStore
from forge.models.errors import ModelUnavailableError
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
from forge.security.classification import DataClassification, classify_text
from forge.security.policy_gate import PolicyDecision


def _forwardable_kwargs(callable_obj: Any, **kwargs: Any) -> dict[str, Any]:
    """Forward only the keyword arguments a provider callable accepts.

    Providers may implement a subset of the full keyword contract
    (``context``/``task``/``instructions``/``max_output_tokens``/``temperature``).
    This keeps the fabric honest — it never passes a keyword a provider did not
    declare, and providers with ``**kwargs`` receive everything.
    """
    try:
        signature = inspect.signature(callable_obj)
    except (TypeError, ValueError):
        return {}
    parameters = list(signature.parameters.values())
    if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters):
        return dict(kwargs)
    accepted = {
        parameter.name
        for parameter in parameters
        if parameter.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }
    return {key: value for key, value in kwargs.items() if key in accepted}


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
        model_policy=None,
    ) -> None:
        #: Optional model data policy (A33): classified content is filtered
        #: per candidate model before any provider call. ``None`` (default)
        #: preserves exact legacy behavior.
        self.config = config
        self.registry = registry if registry is not None else ModelRegistry()
        self.providers = providers if providers is not None else ProviderRegistry()
        self.policy = policy if policy is not None else _policy_from_config(config)
        self.telemetry = telemetry if telemetry is not None else Telemetry(
            enabled=config.telemetry_enabled if config else True,
            sink_path=config.telemetry_path if config else None,
        )
        self.credentials = credentials if credentials is not None else CredentialStore()
        self.router = router if router is not None else FabricRouter(self.registry, self.policy, self.telemetry)
        self.default_model = config.default_model if config else None
        self.preferred_provider = config.preferred_provider if config else None
        self.model_policy = model_policy

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
            ollama = OllamaProvider(model=config.ollama_model, url=config.ollama_url, timeout=config.timeout_seconds)
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
                capability_status=_ollama_capability_status(config.ollama_model, capabilities),
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
            policy=_policy_from_config(config),
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
        decision = self.router.route(request, policy=policy)
        return self._apply_preferences(decision, request)

    def request(self, request: ModelRequest | str, *, policy: RoutingPolicy | None = None) -> ModelResponse:
        """Main entry point: route and execute a model request.

        This is the canonical ``request()`` API agents use. It is an alias of
        :meth:`generate` (which already routes, fails over, records telemetry,
        and returns a structured ``ModelResponse``).
        """
        return self.generate(request, policy=policy)

    def select(self, request: ModelRequest | str | None = None, capability: str | None = None,
               **kwargs: Any) -> Model | None:
        """Select (but do not call) the model the router would choose."""
        if request is None:
            request = ModelRequest(prompt=kwargs.pop("prompt", ""), capability=capability or self._default_capability(), **kwargs)
        decision = self.route(request)
        return decision.model

    def generate(self, request: ModelRequest | str, *, policy: RoutingPolicy | None = None) -> ModelResponse:
        """Route and call a provider, returning a structured ``ModelResponse``.

        Never raises for routing/provider failures: failures are recorded as
        feedback and telemetry, and deterministic failover moves down the
        candidate chain (ending with the deterministic local fallback). The
        caller decides how to handle a ``success=False`` result.
        """
        if isinstance(request, str):
            request = ModelRequest(prompt=request, capability=self._default_capability())
        started = perf_counter()
        decision = self.route(request, policy=policy)
        if not decision.chosen:
            error = decision.error or "no model available for this request"
            self.telemetry.record("error", trace_id=request.trace_id, capability=request.capability, error=error)
            return ModelResponse.failure(error, request_id=request.trace_id)
        data_policy, classification, data_authorized = self._data_policy_for(request)

        chain = self._failover_chain(decision)
        last_error = ""
        for model_name in chain:
            try:
                model = self.registry.get(model_name)
            except KeyError:
                continue
            if data_policy is not None and classification is not None:
                verdict = data_policy.evaluate(
                    classification, local=model.local,
                    authorized=data_authorized)
                if verdict != PolicyDecision.ALLOW:
                    last_error = (
                        f"model {model.name!r} is not authorized for "
                        f"{classification.value} data ({verdict.value})")
                    self.record_feedback(
                        model=model.name, provider=model.provider,
                        capability=request.capability, success=False,
                        error=last_error,
                    )
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
                result = provider.generate(
                    request.prompt,
                    **_forwardable_kwargs(
                        provider.generate,
                        context=request.context,
                        task=request.task,
                        instructions=request.constraints_text(),
                        max_output_tokens=request.max_output_tokens,
                        temperature=request.temperature,
                    ),
                )
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
            if classification is not None:
                response.metadata["classification"] = classification.value
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

    def stream(self, request: ModelRequest | str, *, policy: RoutingPolicy | None = None) -> Iterator[str]:
        """Stream response text with the same guarantees as :meth:`generate`.

        Routing, capability validation, availability checking, and policy
        filtering are identical to ``generate``. The selected provider's
        ``stream`` is used when available; otherwise its ``generate`` result is
        yielded as a single chunk (an explicit, tested fallback). Provider
        failures fall through the same failover chain as ``generate`` — chunks
        are buffered and only emitted after the provider completes, so a failed
        stream never emits partial or duplicate output. If every candidate
        fails, a ``ModelUnavailableError`` is raised (never silently swallowed);
        health, reliability, latency, feedback, and telemetry are recorded for
        both success and failure.
        """
        if isinstance(request, str):
            request = ModelRequest(prompt=request, capability=self._default_capability())
        started = perf_counter()
        decision = self.route(request, policy=policy)
        if not decision.chosen:
            error = decision.error or "no model available for this request"
            self.telemetry.record("error", trace_id=request.trace_id, capability=request.capability, error=error)
            raise ModelUnavailableError(error)
        data_policy, classification, data_authorized = self._data_policy_for(request)

        chain = self._failover_chain(decision)
        last_error = ""
        for model_name in chain:
            try:
                model = self.registry.get(model_name)
            except KeyError:
                continue
            if data_policy is not None and classification is not None:
                verdict = data_policy.evaluate(
                    classification, local=model.local,
                    authorized=data_authorized)
                if verdict != PolicyDecision.ALLOW:
                    last_error = (
                        f"model {model.name!r} is not authorized for "
                        f"{classification.value} data ({verdict.value})")
                    self.record_feedback(
                        model=model.name, provider=model.provider,
                        capability=request.capability, success=False,
                        error=last_error,
                    )
                    continue
            provider = self.providers.get(model.provider) if self.providers.has(model.provider) else None
            if provider is None:
                model.available = False
                last_error = f"provider {model.provider!r} for model {model.name!r} is not registered"
                self.record_feedback(
                    model=model.name, provider=model.provider, capability=request.capability,
                    success=False, error=last_error,
                )
                continue

            stream_fn = getattr(provider, "stream", None)
            call_kwargs = _forwardable_kwargs(
                stream_fn if callable(stream_fn) else provider.generate,
                context=request.context,
                task=request.task,
                instructions=request.constraints_text(),
                max_output_tokens=request.max_output_tokens,
                temperature=request.temperature,
            )

            if callable(stream_fn):
                chunks, ok, error = self._collect_stream(
                    stream_fn, request, call_kwargs
                )
                if not ok:
                    last_error = error
                    self.record_feedback(
                        model=model.name, provider=model.provider, capability=request.capability,
                        success=False, latency_ms=(perf_counter() - started) * 1000.0,
                        complexity=request.complexity, error=error,
                    )
                    continue
                latency_ms = (perf_counter() - started) * 1000.0
                self.record_feedback(
                    model=model.name, provider=model.provider, capability=request.capability,
                    success=True, latency_ms=latency_ms, complexity=request.complexity,
                )
                self.telemetry.record(
                    "response", trace_id=request.trace_id, model=model.name,
                    provider=model.provider, success=True, latency_ms=latency_ms,
                )
                yield from chunks
                return

            # Explicit, tested fallback: provider without streaming is served
            # a single complete response from generate().
            try:
                result = provider.generate(request.prompt, **call_kwargs)
            except Exception as exc:
                last_error = str(exc)
                self.record_feedback(
                    model=model.name, provider=model.provider, capability=request.capability,
                    success=False, latency_ms=(perf_counter() - started) * 1000.0,
                    complexity=request.complexity, error=last_error,
                )
                continue
            latency_ms = (perf_counter() - started) * 1000.0
            self.record_feedback(
                model=model.name, provider=model.provider, capability=request.capability,
                success=True, latency_ms=latency_ms, complexity=request.complexity,
                input_tokens=result.input_tokens, output_tokens=result.output_tokens,
            )
            self.telemetry.record(
                "response", trace_id=request.trace_id, model=model.name,
                provider=model.provider, success=True, latency_ms=latency_ms,
                input_tokens=result.input_tokens, output_tokens=result.output_tokens,
            )
            yield result.text
            return

        self.telemetry.record("error", trace_id=request.trace_id, capability=request.capability, error=last_error)
        raise ModelUnavailableError(last_error or "no model available")

    @staticmethod
    def _collect_stream(stream_fn: Any, request: ModelRequest, call_kwargs: dict[str, Any]) -> tuple[list[str], bool, str]:
        """Buffer a provider stream and report success/failure.

        Chunks are buffered so a provider that fails partway through never
        emits partial output (which would corrupt the result or duplicate
        output if the caller falls back to the next candidate).
        """
        chunks: list[str] = []
        try:
            for chunk in stream_fn(request.prompt, **call_kwargs):
                if isinstance(chunk, str):
                    chunks.append(chunk)
                else:
                    chunks.append(str(chunk))
        except Exception as exc:
            return [], False, str(exc)
        return chunks, True, ""

    def _data_policy_for(self, request: ModelRequest):
        """Resolve the effective model data policy for one request.

        A request-level policy (``metadata["model_data_policy"]``) wins over
        the fabric-level one; with neither configured, content flows exactly
        as before (``(None, None, False)``).
        """
        metadata = request.metadata or {}
        data_policy = metadata.get("model_data_policy") or self.model_policy
        if data_policy is None:
            return None, None, False
        classification: DataClassification = classify_text(
            f"{request.prompt}\n{request.context}\n{request.task}")
        return data_policy, classification, metadata.get("data_authorized") is True

    def _failover_chain(self, decision: RouteDecision) -> list[str]:
        chain: list[str] = []
        if decision.model is not None:
            chain.append(decision.model.name)
        for name in decision.candidates:
            if name not in chain:
                chain.append(name)
        return chain

    def _apply_preferences(self, decision: RouteDecision, request: ModelRequest) -> RouteDecision:
        """Apply ``default_model`` / ``preferred_provider`` preferences.

        These only reorder the router's candidate chain; they never add a model
        that failed capability/context/policy filtering. The explicit
        ``default_model`` wins over the softer ``preferred_provider``.
        """
        if not decision.chosen:
            return decision
        required = request.effective_capabilities()

        if self.default_model and self.default_model in decision.candidates:
            model = self.registry.get(self.default_model)
            if model.available and model.supports_all(required):
                return self._promote(decision, model)

        if self.preferred_provider:
            for name in decision.candidates:
                model = self.registry.get(name)
                if (
                    model.provider == self.preferred_provider
                    and not model.fallback
                    and model.available
                    and model.supports_all(required)
                ):
                    return self._promote(decision, model)
        return decision

    @staticmethod
    def _promote(decision: RouteDecision, model: Model) -> RouteDecision:
        decision.model = model
        candidates = [name for name in decision.candidates if name != model.name]
        decision.candidates = (model.name,) + tuple(candidates)
        return decision

    # -- feedback --------------------------------------------------------

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

    def record_result(self, model: str, success: bool, **kwargs: Any) -> None:
        """Alias for :meth:`record_feedback` (master-spec naming)."""
        self.record_feedback(model=model, success=success, **kwargs)

    # -- introspection ---------------------------------------------------

    def _default_capability(self) -> str:
        return self.config.default_capability if self.config else Capability.CODING.value

    def models(self) -> list[Model]:
        return self.registry.list()

    def available_models(self) -> list[Model]:
        return self.registry.available()

    def models_for_capability(self, capability: str) -> list[Model]:
        return self.registry.by_capability(capability)

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

    def provider_health(self) -> dict[str, dict[str, Any]]:
        """Aggregate per-provider health from the models they serve."""
        result: dict[str, dict[str, Any]] = {}
        for model in self.registry.list():
            entry = result.setdefault(model.provider, {
                "provider": model.provider,
                "models": 0,
                "healthy": 0,
                "degraded": 0,
                "unhealthy": 0,
                "unknown": 0,
                "available": 0,
            })
            entry["models"] += 1
            entry[model.health.status] += 1
            if model.available:
                entry["available"] += 1
        return result

    def discover_models(self) -> dict[str, Any]:
        """Discover models exposed by providers that support discovery.

        Only Ollama's ``/api/tags`` discovery is implemented today. This is an
        explicit, opt-in network call — never performed automatically at
        construction — and never downloads models. Returns per-provider results.
        """
        results: dict[str, Any] = {}
        for name, provider in self.providers.items():
            discover = getattr(provider, "list_models", None)
            if not callable(discover):
                results[name] = {"discovered": [], "error": "provider does not support discovery"}
                continue
            try:
                found = discover()
            except Exception as exc:
                results[name] = {"discovered": [], "error": str(exc)}
                continue
            registered: list[str] = []
            for model_name in found:
                registry_name = f"{name}/{model_name}"
                if self.registry.has(registry_name):
                    continue
                capabilities = _ollama_capabilities(model_name, self.config.ollama_context_window if self.config else 8192)
                self.registry.register(Model(
                    name=registry_name,
                    provider=name,
                    capabilities=capabilities,
                    capability_status=_ollama_capability_status(model_name, capabilities),
                    context_window=self.config.ollama_context_window if self.config else 8192,
                    free=True,
                    local=True,
                    metadata={"description": f"Discovered via {name} model discovery."},
                ))
                registered.append(registry_name)
            results[name] = {"discovered": registered}
        return results

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


def _policy_from_config(config: FabricConfig | None) -> RoutingPolicy:
    """Derive the effective routing policy from configuration.

    Named preset (``default_policy``) wins, then ``local_only``/``free_only``
    constraints are applied on top. No secret material is involved.
    """
    if config is None:
        return RoutingPolicy()
    if config.default_policy:
        policy = RoutingPolicy.preset(config.default_policy)
    else:
        policy = config.policy
    if config.local_only:
        policy.allow_remote = False
        policy.prefer_local = True
    if config.free_only:
        policy.allow_paid = False
        policy.prefer_free = True
        if policy.max_cost_per_token is None:
            policy.max_cost_per_token = 0.0
    return policy


def _ollama_capabilities(model_name: str, context_window: int) -> tuple[str, ...]:
    capabilities = list(TEXT_CAPABILITIES)
    if OllamaProvider.supports_vision(model_name):
        capabilities.append(Capability.VISION.value)
    if context_window >= 32768:
        capabilities.append(Capability.LONG_CONTEXT.value)
    return tuple(capabilities)


def _ollama_capability_status(model_name: str, capabilities: tuple[str, ...]) -> dict[str, str]:
    """Verification levels for Ollama-derived capabilities.

    Vision is inferred from a conservative family-prefix heuristic (detected,
    not independently verified); long context comes from configured metadata.
    """
    status: dict[str, str] = {}
    if Capability.VISION.value in capabilities:
        status[Capability.VISION.value] = "detected"
    return status
