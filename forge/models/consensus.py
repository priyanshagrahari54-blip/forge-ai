"""Deterministic multi-model consensus over real responses (A18).

Consensus aggregates independent ``ModelResponse`` objects for the same
request. It is honest aggregation — quorum counting and deterministic
tie-breaking — not machine learning and not a substitute for real independent
model calls.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from forge.models.request import ModelResponse


class ConsensusStrategy(str, Enum):
    MAJORITY = "majority"     # most common normalized answer (>= quorum)
    UNANIMOUS = "unanimous"   # all answers identical
    WEIGHTED = "weighted"     # highest total weight (weights required)
    BEST = "best"             # lowest-latency successful response


@dataclass
class ConsensusResult:
    agreed: bool = False
    strategy: str = ""
    selected: str = ""
    model: str = ""
    provider: str = ""
    confidence: float = 0.0
    total: int = 0
    agreeing: int = 0
    quorum: float = 0.0
    reason: str = ""
    distribution: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agreed": self.agreed,
            "strategy": self.strategy,
            "selected": self.selected,
            "model": self.model,
            "provider": self.provider,
            "confidence": round(self.confidence, 4),
            "total": self.total,
            "agreeing": self.agreeing,
            "quorum": self.quorum,
            "reason": self.reason,
            "distribution": dict(self.distribution),
        }


def _normalize(text: str) -> str:
    return " ".join((text or "").strip().split())


def _distribution(responses: list[ModelResponse]) -> dict[str, int]:
    counts = Counter(_normalize(response.text) for response in responses)
    return {key: counts[key] for key in sorted(counts)}


def consensus(
    responses: list[ModelResponse],
    strategy: str | ConsensusStrategy = ConsensusStrategy.MAJORITY,
    *,
    weights: list[float] | None = None,
    quorum: float = 0.5,
) -> ConsensusResult:
    """Aggregate independent responses into a deterministic consensus result.

    ``quorum`` is the fraction of responses that must agree. Responses are
    grouped by normalized text; only successful responses participate. The
    result always records agreement, confidence, distribution, and a reason on
    disagreement.
    """
    strategy = ConsensusStrategy(strategy)
    successful = [response for response in responses if response.success]
    total = len(successful)

    if not successful:
        return ConsensusResult(
            strategy=strategy.value,
            reason="no successful responses to aggregate",
        )

    groups: dict[str, list[ModelResponse]] = {}
    for response in successful:
        groups.setdefault(_normalize(response.text), []).append(response)

    distribution = _distribution(successful)

    if strategy == ConsensusStrategy.UNANIMOUS:
        if len(groups) == 1:
            key = next(iter(groups))
            return _agreed(strategy, key, groups[key][0], total, len(groups[key]), quorum, distribution)
        return ConsensusResult(
            agreed=False,
            strategy=strategy.value,
            total=total,
            quorum=quorum,
            reason=f"not unanimous: {len(groups)} distinct answers",
            distribution=distribution,
        )

    if strategy == ConsensusStrategy.WEIGHTED:
        if weights is None or len(weights) != total:
            return ConsensusResult(
                strategy=strategy.value,
                total=total,
                quorum=quorum,
                reason="weighted consensus requires one weight per successful response",
                distribution=distribution,
            )
        scores: dict[str, float] = {}
        for response, weight in zip(successful, weights):
            key = _normalize(response.text)
            scores[key] = scores.get(key, 0.0) + weight
        top_key = max(sorted(scores), key=lambda k: (scores[k], k))
        return _agreed(strategy, top_key, groups[top_key][0], total, len(groups[top_key]), quorum, distribution)

    if strategy == ConsensusStrategy.BEST:
        best = min(successful, key=lambda r: ((r.latency_ms or 0.0), r.model))
        key = _normalize(best.text)
        return _agreed(strategy, key, best, total, len(groups[key]), quorum, distribution)

    # Majority (default)
    top_key, top_count = max(distribution.items(), key=lambda item: (item[1], item[0]))
    needed = quorum * total
    if top_count >= needed:
        return _agreed(strategy, top_key, groups[top_key][0], total, top_count, quorum, distribution)
    return ConsensusResult(
        agreed=False,
        strategy=strategy.value,
        selected=top_key,
        model=groups[top_key][0].model,
        provider=groups[top_key][0].provider,
        confidence=(top_count / total) if total else 0.0,
        total=total,
        agreeing=top_count,
        quorum=quorum,
        reason=f"no answer reached quorum {quorum:.0%} (top={top_count}/{total})",
        distribution=distribution,
    )


def _agreed(
    strategy: ConsensusStrategy,
    key: str,
    response: ModelResponse,
    total: int,
    agreeing: int,
    quorum: float,
    distribution: dict[str, int],
) -> ConsensusResult:
    return ConsensusResult(
        agreed=agreeing >= max(1, quorum * total),
        strategy=strategy.value,
        selected=key,
        model=response.model,
        provider=response.provider,
        confidence=(agreeing / total) if total else 0.0,
        total=total,
        agreeing=agreeing,
        quorum=quorum,
        distribution=distribution,
    )
