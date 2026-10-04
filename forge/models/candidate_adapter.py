"""Normalize model metadata into routing candidates."""
from forge.models.routing_intelligence import RoutingCandidate
def to_candidate(item):
    if isinstance(item,RoutingCandidate):return item
    d=item if isinstance(item,dict) else {}
    return RoutingCandidate(model=str(d.get("model","")),capability=str(d.get("capability","text")),quality_score=float(d.get("quality_score",.5)),latency_score=float(d.get("latency_score",.5)),cost_score=float(d.get("cost_score",.5)),privacy_score=float(d.get("privacy_score",.5)),benchmark_score=float(d.get("benchmark_score",.5)),available=bool(d.get("available",False)))
