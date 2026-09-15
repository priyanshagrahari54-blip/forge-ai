"""Model-performance evidence for self-improvement (Session 11).

The inference fabric records structured telemetry about *routing*, not about
content. This module turns that telemetry into findings the existing
self-development pipeline can reason about — wrong routing, timeouts, resource
exhaustion, fallback frequency, quality regression, high latency, model failure
rate — and it is deliberately **proposal-only**.

Hard boundary: nothing produced here may change a permission, a security rule,
a provider authorization, or a credential policy. Those live in A32/A33 and the
resource governor, and every one of them fails closed. A finding is evidence
for a human (or for the gated improvement loop), never authority.

Python floor: 3.8 (Windows 7 reference target). Stdlib only.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

__all__ = ["ROUTING_EVIDENCE_KINDS", "evidence_to_findings",
           "summarize_evidence"]

#: The evidence kinds this module knows how to turn into a finding.
ROUTING_EVIDENCE_KINDS = (
    "timeout_rate",
    "resource_denial_rate",
    "policy_denial_rate",
    "fallback_frequency",
    "stale_results",
    "model_failure_rate",
    "high_latency",
    "quality_regression",
)

_SEVERITY = {
    "timeout_rate": "high",
    "resource_denial_rate": "high",
    "model_failure_rate": "high",
    "policy_denial_rate": "medium",
    "fallback_frequency": "medium",
    "stale_results": "medium",
    "high_latency": "low",
    "quality_regression": "medium",
}

#: What a proposal may touch. Anything outside this list is refused.
_PROPOSAL_SCOPE = ("routing_order", "verification", "timeout_bound",
                   "device_profile", "residency_budget", "fallback_ladder")


def summarize_evidence(evidence: Dict[str, Any]) -> Dict[str, Any]:
    """A bounded, content-free summary suitable for a report or telemetry."""
    evidence = evidence or {}
    return {
        "requests": int(evidence.get("requests") or 0),
        "success_rate": evidence.get("success_rate"),
        "failure_rate": evidence.get("failure_rate"),
        "timeouts": int(evidence.get("timeouts") or 0),
        "resource_denials": int(evidence.get("resource_denials") or 0),
        "policy_denials": int(evidence.get("policy_denials") or 0),
        "fallbacks": int(evidence.get("fallbacks") or 0),
        "stale_results": int(evidence.get("stale_results") or 0),
        "latency_p50_ms": evidence.get("latency_p50_ms"),
        "latency_p95_ms": evidence.get("latency_p95_ms"),
        "proposals": len(evidence.get("proposals") or ()),
        "models": sorted((evidence.get("per_model") or {}).keys()),
        "authority": evidence.get("authority", ""),
    }


def evidence_to_findings(evidence: Dict[str, Any], *,
                         prefix: str = "inference",
                         limit: int = 24) -> List[Any]:
    """Convert routing evidence into self-development ``Finding`` objects.

    Returns ``[]`` when the improvement module is unavailable, so a caller can
    never crash on a missing optional dependency.
    """
    try:
        from forge.self_development.findings import (Finding,
                                                     FindingCategory,
                                                     FindingSeverity)
    except Exception:
        return []
    proposals: Sequence[Dict[str, Any]] = list(
        (evidence or {}).get("proposals") or ())[:max(1, int(limit))]
    summary = summarize_evidence(evidence)
    findings: List[Any] = []
    for index, proposal in enumerate(proposals):
        kind = str(proposal.get("kind") or "routing")
        if kind not in ROUTING_EVIDENCE_KINDS:
            continue
        severity = str(proposal.get("severity")
                       or _SEVERITY.get(kind, "low")).lower()
        if severity not in {item.value for item in FindingSeverity}:
            severity = "low"
        text = str(proposal.get("proposal") or "")
        findings.append(Finding(
            id="%s-routing-%s-%d" % (prefix, kind, index),
            category=FindingCategory.PERFORMANCE.value
            if kind in ("timeout_rate", "high_latency", "model_failure_rate")
            else FindingCategory.FAILURE_PATTERN.value,
            severity=severity,
            evidence="%s | summary=%s" % (
                str(proposal.get("evidence") or "")[:300],
                _compact(summary)),
            affected_files=["forge/models/routing.py",
                            "forge/models/engine.py"],
            affected_symbols=["RoutingEngine.route", "InferenceFabric.generate"],
            description=("Inference routing evidence: %s"
                         % str(proposal.get("evidence") or kind)[:400]),
            proposed_improvement=("%s (scope: %s; this proposal may not change "
                                  "permissions, security rules, provider "
                                  "authorization, or credential policy)"
                                  % (text, ", ".join(_PROPOSAL_SCOPE))),
            estimated_complexity="low",
            estimated_risk="low",
        ))
    return findings


def _compact(summary: Dict[str, Any]) -> str:
    parts = []
    for key in ("requests", "success_rate", "timeouts", "resource_denials",
                "policy_denials", "fallbacks", "stale_results",
                "latency_p95_ms"):
        value = summary.get(key)
        if value is None:
            continue
        parts.append("%s=%s" % (key, value))
    return ",".join(parts)[:300]
