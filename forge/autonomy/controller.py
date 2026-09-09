"""Autonomy levels (A69): policy-derived, never policy-bypassing.

The autonomy controller is consultative: it computes, from the
actual permission policy, what each resource allows without an
operator, what needs approval, and what is blocked. Level
transitions are validated and stepwise; the level can only select
an existing run mode — it can never grant anything the policy
denies.
"""
from __future__ import annotations

import time
from typing import Any

from forge.security.policy import (
    PermissionPolicy,
    PermissionRequest,
    PolicyDecision,
    RESOURCE_OPERATIONS,
    Resource,
)

LEVELS = ("safe", "assisted", "autonomous")
_RANK = {level: index for index, level in enumerate(LEVELS)}

PROFILE_LEVELS = {
    "safe": "safe",
    "locked": "safe",
    "assisted": "assisted",
    "autonomous": "autonomous",
}


def _resource_value(resource: Resource | str) -> str:
    return resource.value if isinstance(resource, Resource) \
        else str(resource)


def probe_decision(policy: PermissionPolicy, resource: Resource,
                   operation: str, risk: str = "LOW",
                   agent: str = "forge-autonomy") -> str:
    request = PermissionRequest(
        agent=agent, resource=resource, operation=operation, scope="",
        reason="autonomy probe")
    evaluation = policy.evaluate(request)
    decision = str(evaluation.decision)
    if decision.endswith("ALLOW"):
        return "autonomous"
    if decision.endswith("REQUIRE_APPROVAL"):
        return "approval"
    return "blocked"


def resource_autonomy(policy: PermissionPolicy,
                      ) -> dict[str, dict[str, str]]:
    """Per-operation verdicts evaluated against the actual rules.

    For each resource/operation pair, every matching rule is
    evaluated with a request aligned to that rule's constrained
    dimensions (scope/agent/args); the most permissive outcome is
    reported. No matching rule = fail closed = blocked.
    """
    report: dict[str, dict[str, str]] = {}
    for resource, operations in sorted(
            RESOURCE_OPERATIONS.items(),
            key=lambda pair: str(pair[0].value)):
        ops: dict[str, str] = {}
        for operation in sorted(operations):
            verdict = "blocked"
            for rule in (policy.rules or []):
                if _resource_value(rule.resource) != resource.value:
                    continue
                if str(getattr(rule, "operation", "")) != operation:
                    continue
                request = PermissionRequest(
                    agent=str(getattr(rule, "agent", "") or
                              "forge-autonomy"),
                    resource=resource, operation=operation,
                    scope=str(getattr(rule, "scope", "") or ""),
                    reason="autonomy probe",
                    details=(("args",
                              tuple(getattr(rule, "args", None) or ())),))
                evaluation = policy.evaluate(request)
                decision = str(evaluation.decision)
                if decision.endswith("ALLOW"):
                    verdict = "autonomous"
                    break
                if decision.endswith("REQUIRE_APPROVAL"):
                    verdict = "approval"
            ops[operation] = verdict
        report[str(resource.value)] = ops
    return report


def summarize(report: dict[str, dict[str, str]]) -> dict[str, int]:
    autonomous = approval = blocked = 0
    for ops in report.values():
        for verdict in ops.values():
            if verdict == "autonomous":
                autonomous += 1
            elif verdict == "approval":
                approval += 1
            else:
                blocked += 1
    return {"autonomous": autonomous, "approval": approval,
            "blocked": blocked}


def effective_level(profile: str, override: str = "") -> str:
    if override:
        return override if override in LEVELS else profile
    return PROFILE_LEVELS.get((profile or "assisted").lower(),
                              "assisted")


def validate_transition(current_level: str, target: str) -> None:
    if target not in LEVELS:
        raise ValueError(
            f"unknown autonomy level {target!r}; expected one of "
            f"{LEVELS}")
    if current_level not in LEVELS:
        raise ValueError(
            f"the current profile ({current_level!r}) manages its own "
            "autonomy; the level cannot be changed here")
    if _RANK[target] > _RANK[current_level] + 1:
        raise ValueError(
            "autonomy must be increased stepwise "
            "(safe → assisted → autonomous)")
    if target == current_level:
        raise ValueError(f"already at autonomy level {target!r}")


def autonomy_report(policy: PermissionPolicy, profile: str,
                    override: str = "") -> dict[str, Any]:
    level = effective_level(profile, override)
    resource_report = resource_autonomy(policy)
    return {
        "level": level,
        "profile": profile,
        "override": bool(override),
        "summary": summarize(resource_report),
        "resources": resource_report,
        "checked_at": time.time(),
        "note": ("Derived from the live permission policy; the level "
                 "selects a run mode and can never grant what the "
                 "policy denies."),
    }
