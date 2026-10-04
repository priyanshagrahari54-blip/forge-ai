"""Unified advisory model routing over Model Fabric metadata."""
from __future__ import annotations
from dataclasses import dataclass
from forge.models.routing_intelligence import RoutingContext, RoutingCandidate, rank

@dataclass(frozen=True)
class ModelRoute:
    model: str
    score: float
    reason: str

class UnifiedModelRouter:
    def choose(self, candidates, context: RoutingContext):
        ranked=rank(candidates,context)
        if not ranked or ranked[0][0] < 0:
            return None
        score_value,candidate=ranked[0]
        return ModelRoute(candidate.model,score_value,"highest verified advisory score")
