from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ModelInfo:
    name: str
    capability: str
    available: bool = True
    is_local: bool = True
    provider: str = "mock"
    max_context_tokens: int = 8192
    quality_score: float = 1.0  # Range: 0.0 to 1.0
    reliability_score: float = 1.0  # Range: 0.0 to 1.0
    latency_ms: float = 100.0  # Expected latency in ms
    cost_per_1k_tokens: float = 0.0  # Cost in USD per 1k tokens
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RoutingDecision:
    selected_model: ModelInfo | None
    score: float
    capability: str
    reasoning: str
    fallback_model: ModelInfo | None = None


@dataclass
class ModelHistoricalStats:
    calls: int = 0
    successes: int = 0
    total_quality: float = 0.0
    total_latency_ms: float = 0.0

    @property
    def success_rate(self) -> float:
        if self.calls == 0:
            return 1.0
        return self.successes / self.calls

    @property
    def average_quality(self) -> float:
        if self.calls == 0:
            return 1.0
        return self.total_quality / self.calls

    @property
    def average_latency(self) -> float:
        if self.calls == 0:
            return 100.0
        return self.total_latency_ms / self.calls


class ModelRouter:
    """Deterministic scoring-based model router."""

    def __init__(self, prefer_local: bool = True) -> None:
        self.models: list[ModelInfo] = []
        self.prefer_local = prefer_local
        self.history: dict[str, ModelHistoricalStats] = {}
        self.decisions: list[RoutingDecision] = []

    def register(self, model: ModelInfo) -> None:
        self.models.append(model)
        if model.name not in self.history:
            self.history[model.name] = ModelHistoricalStats()

    def record_result(
        self,
        model_name: str,
        success: bool,
        quality: float = 1.0,
        latency_ms: float = 100.0,
    ) -> None:
        stats = self.history.setdefault(model_name, ModelHistoricalStats())
        stats.calls += 1
        if success:
            stats.successes += 1
        stats.total_quality += quality
        stats.total_latency_ms += latency_ms

    def score_model(
        self,
        model: ModelInfo,
        capability: str,
        context_tokens: int = 0,
        prefer_local: bool | None = None,
    ) -> float:
        if not model.available:
            return -1.0

        alt_caps = model.metadata.get("capabilities", ())
        if model.capability != capability and capability not in alt_caps:
            return -1.0

        if context_tokens > model.max_context_tokens:
            return -1.0

        p_local = self.prefer_local if prefer_local is None else prefer_local
        stats = self.history.get(model.name, ModelHistoricalStats())

        reliability = (
            stats.success_rate if stats.calls > 0 else model.reliability_score
        )
        quality = (
            stats.average_quality if stats.calls > 0 else model.quality_score
        )

        context_fit = 1.0 if context_tokens <= model.max_context_tokens else 0.0
        local_multiplier = 1.2 if (p_local and model.is_local) else 1.0

        avg_lat = stats.average_latency if stats.calls > 0 else model.latency_ms
        latency_penalty = min(0.3, avg_lat / 10000.0)
        cost_penalty = min(0.3, model.cost_per_1k_tokens * 10.0)

        base_score = (quality * 0.4) + (reliability * 0.4) + (context_fit * 0.2)
        score = (base_score * local_multiplier) - latency_penalty - cost_penalty
        return max(0.0, score)

    def route(
        self,
        capability: str,
        context_tokens: int = 0,
        prefer_local: bool | None = None,
    ) -> RoutingDecision:
        candidates = [
            m
            for m in self.models
            if m.available
            and (
                m.capability == capability
                or capability in m.metadata.get("capabilities", ())
            )
        ]

        if not candidates:
            decision = RoutingDecision(
                selected_model=None,
                score=0.0,
                capability=capability,
                reasoning=f"No available candidates for capability '{capability}'.",
            )
            self.decisions.append(decision)
            return decision

        scored = [
            (m, self.score_model(m, capability, context_tokens, prefer_local))
            for m in candidates
        ]
        scored.sort(key=lambda item: item[1], reverse=True)

        selected, best_score = scored[0]
        fallback = (
            scored[1][0] if len(scored) > 1 and scored[1][1] > 0 else None
        )

        reasoning = (
            f"Selected model '{selected.name}' (provider={selected.provider}) "
            f"with score {best_score:.3f} for capability '{capability}'."
        )

        decision = RoutingDecision(
            selected_model=selected,
            score=best_score,
            capability=capability,
            reasoning=reasoning,
            fallback_model=fallback,
        )
        self.decisions.append(decision)
        return decision

    def select(self, capability: str) -> ModelInfo | None:
        decision = self.route(capability)
        return decision.selected_model
