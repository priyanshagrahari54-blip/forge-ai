from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from forge.models.provider import ModelProvider
from forge.models.policy import RoutingPolicy
from forge.models.registry import Model, ModelRegistry
from forge.models.request import ModelRequest
from forge.models.telemetry import Telemetry


@dataclass
class ModelInfo:
    """Legacy router entry. Kept for backward compatibility with pre-fabric
    integrations; the fabric's canonical entry is ``forge.models.registry.Model``."""

    name: str
    capability: str
    available: bool = False
    context_size: int = 4096
    historical_success_rate: float = 1.0
    latency: float = 0.0
    failure_rate: float = 0.0
    task_complexity: float = 1.0
    cost_per_token: float = 0.0
    free: bool = True
    provider: ModelProvider | None = None
    capabilities: tuple[str, ...] = ()


@dataclass
class RoutingDecision:
    model: ModelInfo
    score: float
    factors: dict[str, float] = field(default_factory=dict)


class ModelRouter:
    """Legacy capability/complexity/cost router over ``ModelInfo`` entries.

    Preserved exactly for backward compatibility. The Model Fabric layers the
    richer ``FabricRouter`` (registry + policy + telemetry + feedback) on top.
    """

    def __init__(self, models: list[ModelInfo] | None = None) -> None:
        self.models = list(models or [])
        self.history: list[dict[str, Any]] = []

    def register(self, model: ModelInfo) -> None:
        self.models.append(model)

    def decide(self, capability: str, *, min_context_size: int = 0,
               max_cost: float | None = None, max_latency: float | None = None,
               task_complexity: float | None = None, context_size: int = 0) -> RoutingDecision | None:
        candidates = [model for model in self.models
                      if model.available and (model.capability == capability or capability in model.capabilities)]
        candidates = [model for model in candidates
                      if model.context_size >= max(min_context_size, context_size)]
        if max_cost is not None:
            candidates = [model for model in candidates if model.cost_per_token <= max_cost]
        if max_latency is not None:
            candidates = [model for model in candidates if model.latency <= max_latency]
        if task_complexity is not None:
            candidates = [model for model in candidates if model.task_complexity >= task_complexity]
        if not candidates:
            return None

        def score(model: ModelInfo) -> float:
            reliability = max(0.0, model.historical_success_rate - model.failure_rate)
            latency = 1.0 / (1.0 + max(0.0, model.latency))
            cost = 1.0 / (1.0 + max(0.0, model.cost_per_token) * 1000)
            complexity = min(1.0, model.task_complexity / max(task_complexity or 1.0, 1.0))
            context = min(1.0, model.context_size / max(context_size or 1, 1))
            return reliability * .4 + latency * .15 + cost * .15 + complexity * .2 + context * .1

        chosen = max(candidates, key=lambda model: (score(model), model.free, model.name))
        return RoutingDecision(chosen, score(chosen), {
            "reliability": chosen.historical_success_rate - chosen.failure_rate,
            "latency": chosen.latency,
            "cost": chosen.cost_per_token,
            "context": chosen.context_size,
            "complexity": chosen.task_complexity,
        })

    def select(self, capability: str, **kwargs) -> ModelInfo | None:
        decision = self.decide(capability, **kwargs)
        return decision.model if decision else None

    def record(self, model_name: str, success: bool, latency: float | None = None,
               *, capability: str = "", task_complexity: float | None = None) -> None:
        for model in self.models:
            if model.name != model_name:
                continue
            # Exponential smoothing makes recent real outcomes influence the
            # next routing decision while preserving initial model metadata.
            model.historical_success_rate = (model.historical_success_rate + (1.0 if success else 0.0)) / 2
            model.failure_rate = 1.0 - model.historical_success_rate
            if latency is not None:
                model.latency = (model.latency + latency) / 2
        self.history.append({
            "model": model_name,
            "capability": capability,
            "success": success,
            "failure": not success,
            "latency": latency,
            "task_complexity": task_complexity,
        })


@dataclass
class RouteDecision:
    """Result of fabric routing: the chosen model and why it was chosen."""

    model: Model | None = None
    score: float = 0.0
    factors: dict[str, Any] = field(default_factory=dict)
    fallback: bool = False
    fallback_reason: str = ""
    candidates: tuple[str, ...] = ()
    error: str = ""

    @property
    def chosen(self) -> bool:
        return self.model is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model.name if self.model else None,
            "provider": self.model.provider if self.model else None,
            "score": round(self.score, 4),
            "factors": dict(self.factors),
            "fallback": self.fallback,
            "fallback_reason": self.fallback_reason,
            "candidates": list(self.candidates),
            "error": self.error,
        }


class FabricRouter:
    """Capability/context/complexity-aware router over a ``ModelRegistry``.

    Routing is deterministic: score, then prefer free, then local, then name.
    Health, reliability, latency, and cost are all first-class signals. When no
    model satisfies the strict policy, a fixed fallback ladder relaxes exactly
    one hard constraint per step; capability requirements are never relaxed.
    """

    def __init__(
        self,
        registry: ModelRegistry | None = None,
        policy: RoutingPolicy | None = None,
        telemetry: Telemetry | None = None,
    ) -> None:
        self.registry = registry if registry is not None else ModelRegistry()
        self.policy = policy if policy is not None else RoutingPolicy()
        self.telemetry = telemetry if telemetry is not None else Telemetry()
        self.history: list[dict[str, Any]] = []

    # -- routing ---------------------------------------------------------

    def route(self, request: ModelRequest, *, policy: RoutingPolicy | None = None) -> RouteDecision:
        policy = policy or self.policy
        required = request.effective_capabilities()

        capable = [model for model in self.registry if model.available]
        if required:
            capable = [model for model in capable if model.supports_all(required)]

        if not capable:
            missing = sorted(required) if required else ["<any>"]
            error = f"no registered model supports capabilities {missing}"
            decision = RouteDecision(error=error)
            self.telemetry.record("route", **self._route_event(decision, request, required))
            return decision

        # Try the strict policy first, then relax the documented ladder.
        decision = self._try(capable, request, policy, relaxed=frozenset())
        if decision.model is None:
            for relaxed in self._ladder(policy):
                decision = self._try(capable, request, policy, relaxed=frozenset(relaxed))
                if decision.model is not None:
                    decision.fallback = True
                    decision.fallback_reason = "relaxed=" + ",".join(sorted(relaxed))
                    break
        if decision.model is None:
            decision.error = "no model satisfied the routing policy or fallback ladder"
            decision.fallback = True
            decision.fallback_reason = "exhausted"

        self.telemetry.record("route", **self._route_event(decision, request, required))
        return decision

    @staticmethod
    def _ladder(policy: RoutingPolicy) -> list[tuple[str, ...]]:
        steps: list[tuple[str, ...]] = []
        seen: set[str] = set()
        for step in policy.fallback_order:
            seen.add(step)
            steps.append(tuple(sorted(seen)))
        return steps

    def _try(
        self,
        capable: list[Model],
        request: ModelRequest,
        policy: RoutingPolicy,
        relaxed: frozenset[str],
    ) -> RouteDecision:
        pref_free = request.prefer_free if request.prefer_free is not None else policy.prefer_free
        pref_local = request.prefer_local if request.prefer_local is not None else policy.prefer_local

        candidates: list[Model] = []
        for model in capable:
            if model.context_window < request.min_context_window:
                continue
            if "paid" not in relaxed and not policy.allow_paid and not model.free:
                continue
            if "paid" not in relaxed and policy.max_cost_per_token is not None and model.cost_per_token > policy.max_cost_per_token:
                continue
            if "remote" not in relaxed and not policy.allow_remote and not model.local:
                continue
            if "latency" not in relaxed and policy.max_latency_ms is not None and model.latency_ms > policy.max_latency_ms:
                continue
            if "reliability" not in relaxed and model.reliability < policy.min_reliability:
                continue
            if "health" not in relaxed and model.health.last_resort:
                continue
            candidates.append(model)

        if not candidates:
            return RouteDecision(candidates=tuple(model.name for model in capable))

        # The deterministic no-op fallback is only considered when no regular
        # model can serve the request; otherwise it would crowd out real models
        # on otherwise-identical scores. It still appears at the end of the
        # candidate chain so callers can fail over to it deterministically.
        regular = [model for model in candidates if not model.fallback]
        fallback_models = [model for model in candidates if model.fallback]
        pool = regular or fallback_models

        def rank(pair: tuple[float, Model]) -> tuple:
            _score, model = pair
            return (-_score, 0 if model.free else 1, 0 if model.local else 1, model.name)

        scored = [(self._score(model, request, policy, pref_free, pref_local), model) for model in pool]
        scored.sort(key=rank)
        score, model = scored[0]

        chain = [entry for _, entry in scored]
        if regular and fallback_models:
            fallback_scored = [
                (self._score(entry, request, policy, pref_free, pref_local), entry)
                for entry in fallback_models
            ]
            fallback_scored.sort(key=rank)
            chain.extend(entry for _, entry in fallback_scored)

        factors = {
            "reliability": model.reliability,
            "latency_ms": model.latency_ms,
            "cost_per_token": model.cost_per_token,
            "free": model.free,
            "local": model.local,
            "context_window": model.context_window,
            "health": model.health.status,
            "capability": request.capability,
            "complexity": request.complexity,
        }
        return RouteDecision(
            model=model,
            score=score,
            factors=factors,
            candidates=tuple(entry.name for entry in chain),
        )

    def _score(
        self,
        model: Model,
        request: ModelRequest,
        policy: RoutingPolicy,
        pref_free: bool,
        pref_local: bool,
    ) -> float:
        reliability = max(0.0, min(1.0, model.reliability))
        latency = 1.0 / (1.0 + max(0.0, model.latency_ms) / 1000.0)
        cost = 1.0 / (1.0 + max(0.0, model.cost_per_token) * 1_000_000.0)
        free = (1.0 if model.free else 0.0) if pref_free else 0.5
        local = (1.0 if model.local else 0.0) if pref_local else 0.5
        context_fit = min(1.0, model.context_window / max(request.min_context_window or 1, 1))
        health = {
            "healthy": 1.0,
            "unknown": 0.8,
            "degraded": 0.5,
            "unhealthy": 0.0,
        }.get(model.health.status, 0.5)
        # Complexity-aware: complex requests favor models with proportionally
        # larger context windows (and therefore more room to reason).
        complexity = max(0.0, request.complexity or 1.0)
        complexity_fit = min(1.0, model.context_window / (4096.0 * complexity))
        return (
            reliability * 0.30
            + latency * 0.15
            + cost * 0.10
            + free * 0.10
            + local * 0.10
            + context_fit * 0.10
            + health * 0.10
            + complexity_fit * 0.05
        )

    @staticmethod
    def _route_event(decision: RouteDecision, request: ModelRequest, required: tuple[str, ...]) -> dict[str, Any]:
        return {
            "model": decision.model.name if decision.model else None,
            "capability": request.capability,
            "required_capabilities": list(required),
            "fallback": decision.fallback,
            "fallback_reason": decision.fallback_reason,
            "score": round(decision.score, 4),
            "candidates": list(decision.candidates),
            "error": decision.error,
        }

    # -- convenience API (mirrors the legacy router surface) -------------

    def decide(self, capability: str, **kwargs: Any) -> RouteDecision:
        request = ModelRequest(capability=capability, **self._request_kwargs(kwargs))
        return self.route(request)

    def select(self, capability: str, **kwargs: Any) -> Model | None:
        decision = self.decide(capability, **kwargs)
        return decision.model

    @staticmethod
    def _request_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
        mapping = {
            "min_context_size": "min_context_window",
            "min_context_window": "min_context_window",
            "task_complexity": "complexity",
            "complexity": "complexity",
            "max_cost": "max_cost_per_token",
            "max_cost_per_token": "max_cost_per_token",
            "max_latency": "max_latency_ms",
            "max_latency_ms": "max_latency_ms",
            "prompt": "prompt",
            "context": "context",
            "task": "task",
        }
        out: dict[str, Any] = {}
        for key, value in kwargs.items():
            target = mapping.get(key)
            if target is not None:
                out[target] = value
        return out

    # -- feedback --------------------------------------------------------

    def record(
        self,
        model_name: str,
        success: bool,
        latency_ms: float | None = None,
        *,
        capability: str = "",
        task_complexity: float | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        error: str = "",
    ) -> None:
        """Apply a routing outcome to model health/reliability/latency.

        This is the "router feedback" loop: success/failure outcomes move the
        model's health state and exponentially smooth its reliability and
        latency so future decisions adapt to reality.
        """
        try:
            model = self.registry.get(model_name)
        except KeyError:
            model = None
        if model is not None:
            if success:
                model.health.record_success()
                model.reliability = model.reliability + (1.0 - model.reliability) * 0.25
            else:
                model.health.record_failure(error)
                model.reliability = max(0.0, model.reliability * 0.8)
            if latency_ms is not None and latency_ms >= 0:
                model.latency_ms = (model.latency_ms + latency_ms) / 2 if model.latency_ms else latency_ms

        event = {
            "model": model_name,
            "capability": capability,
            "success": success,
            "failure": not success,
            "latency": (latency_ms or 0.0),
            "task_complexity": task_complexity,
            "tokens": {"input": input_tokens, "output": output_tokens},
            "error": error,
        }
        self.history.append(event)
        self.telemetry.record(
            "feedback",
            model=model_name,
            capability=capability,
            success=success,
            latency_ms=latency_ms or 0.0,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            error=error,
        )

    def models(self) -> list[Model]:
        return self.registry.list()
