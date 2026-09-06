from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from forge.models.provider import ModelProvider


@dataclass
class ModelInfo:
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
