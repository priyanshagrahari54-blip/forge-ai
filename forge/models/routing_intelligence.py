"""Advisory routing score; policy/availability remain authoritative."""
from __future__ import annotations
from dataclasses import dataclass

@dataclass(frozen=True)
class RoutingContext:
    task_type: str=""
    complexity: float=.5
    quality: float=.5
    latency_sensitive: bool=False
    cost_sensitive: bool=True
    privacy_sensitive: bool=False

@dataclass(frozen=True)
class RoutingCandidate:
    model: str
    capability: str
    quality_score: float=.5
    latency_score: float=.5
    cost_score: float=.5
    privacy_score: float=.5
    benchmark_score: float=.5
    available: bool=True

def score(c: RoutingCandidate, x: RoutingContext)->float:
    if not c.available:return -1.0
    wq=.30+.25*x.quality; wb=.15+.15*x.complexity
    wl=.20 if x.latency_sensitive else .08
    wc=.18 if x.cost_sensitive else .05
    wp=.20 if x.privacy_sensitive else .04
    total=wq+wb+wl+wc+wp
    return (c.quality_score*wq+c.benchmark_score*wb+c.latency_score*wl+
            c.cost_score*wc+c.privacy_score*wp)/total

def rank(candidates, context: RoutingContext):
    return sorted(((score(c,context),c) for c in candidates),
                  key=lambda x:(x[0],x[1].model),reverse=True)
