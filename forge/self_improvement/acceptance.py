"""Acceptance for self-improvement candidates (A81).

Acceptance is a conjunction of mandatory gates:

- ``tests``         candidate test run is green (or no worse than baseline
                    with strictly fewer failures) and has no new failures;
- ``security``      security verification on the changed files is clean;
- ``architecture``  no new dependency cycles / protected paths / low-level
                    imports;
- ``regression``    the baseline comparison reports no regression;
- ``improvement``   a measurable improvement exists (never "no-op accepted");
- ``guardrails``    no guardrail violations were recorded;
- ``policy``        an explicit :class:`PolicyApproval` issued by a human
                    actor covers this exact candidate fingerprint, and the
                    permission mode permits writes.

One passing gate never overrides a failing one, and the loop cannot mint a
``PolicyApproval`` for itself: the approval names the candidate id *and*
its change fingerprint, so it cannot be transferred to a different change.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

from forge.security.permissions import OperationMode
from forge.self_improvement.candidate import Candidate
from forge.self_improvement.proposals import ImprovementProposal

MANDATORY_GATES = ("tests", "security", "architecture", "regression",
                   "improvement", "guardrails", "policy")


@dataclass(frozen=True)
class PolicyApproval:
    """Operator approval bound to one candidate + change fingerprint."""

    candidate_id: str
    change_fingerprint: str
    approved_by: str
    reason: str = ""
    issued_at: float = field(default_factory=time.time)
    ttl_seconds: float = 3600.0

    @property
    def expired(self) -> bool:
        return time.time() > self.issued_at + self.ttl_seconds

    def matches(self, candidate: Candidate) -> tuple[bool, str]:
        if not self.approved_by.strip():
            return False, "approval has no approver"
        if self.approved_by.strip().lower() in ("forge", "self", "system", "auto", "loop"):
            return False, "approval must come from a human actor, not the loop"
        if self.candidate_id != candidate.id:
            return False, "approval is for a different candidate"
        if not candidate.change_fingerprint or self.change_fingerprint != candidate.change_fingerprint:
            return False, "approval fingerprint does not match the candidate change"
        if self.expired:
            return False, "approval expired"
        return True, f"approved by {self.approved_by}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AcceptanceDecision:
    accepted: bool
    gates: dict[str, bool]
    reasons: dict[str, str]
    failed_gates: list[str]
    metrics: dict[str, Any] = field(default_factory=dict)
    decided_at: float = field(default_factory=time.time)
    #: True when every technical gate passed and only the explicit policy
    #: approval is missing — the candidate is parked for an operator, never
    #: auto-accepted.
    awaiting_approval: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AcceptanceGate:
    """Evaluate one mandatory gate; see :class:`SelfImprovementAcceptance`."""

    name = "gate"

    def evaluate(self, candidate: Candidate, proposal: ImprovementProposal,
                 approval: PolicyApproval | None, mode: OperationMode) -> tuple[bool, str]:
        raise NotImplementedError


class SelfImprovementAcceptance:
    """Aggregate the mandatory gates into one :class:`AcceptanceDecision`."""

    def __init__(self, *, mode: OperationMode | str = OperationMode.ASSISTED) -> None:
        self.mode = OperationMode(mode)

    def decide(self, candidate: Candidate, proposal: ImprovementProposal,
               approval: PolicyApproval | None = None) -> AcceptanceDecision:
        gates: dict[str, bool] = {}
        reasons: dict[str, str] = {}

        tests = candidate.tests
        base = candidate.baseline_tests
        if tests is None or base is None:
            gates["tests"] = False
            reasons["tests"] = "candidate was not tested"
        else:
            new_failures = sorted(set(tests.failing_tests) - set(base.failing_tests))
            if tests.ok and not new_failures:
                gates["tests"] = True
                reasons["tests"] = f"{tests.passed} passed, {tests.failed} failed"
            elif not new_failures and (tests.failed + tests.errors) < (base.failed + base.errors):
                gates["tests"] = True
                reasons["tests"] = (f"strictly fewer failures than baseline "
                                    f"({base.failed + base.errors} -> {tests.failed + tests.errors})")
            else:
                gates["tests"] = False
                reasons["tests"] = ("new failures: " + ", ".join(new_failures[:5])
                                    if new_failures else
                                    f"candidate tests failed ({tests.failed} failed, {tests.errors} errors)")

        sec = candidate.security or {}
        gates["security"] = bool(sec.get("passed", False))
        reasons["security"] = ("clean" if gates["security"] else
                               "findings: " + str(sec.get("findings", sec.get("details", "not run")))[:200])

        arch = candidate.architecture or {}
        gates["architecture"] = bool(arch.get("passed", False))
        reasons["architecture"] = ("clean" if gates["architecture"] else
                                   "; ".join(arch.get("issues", ["not run"]))[:200])

        comparison = candidate.comparison
        if comparison is None:
            gates["regression"] = False
            reasons["regression"] = "no baseline comparison"
            gates["improvement"] = False
            reasons["improvement"] = "no baseline comparison"
        else:
            gates["regression"] = not comparison.regressed
            reasons["regression"] = ("none" if not comparison.regressed else
                                     "; ".join(comparison.regressions)[:200])
            improved = comparison.improvement > 0 or bool(comparison.improvements)
            gates["improvement"] = improved and candidate.build_ok is not False
            reasons["improvement"] = ("; ".join(comparison.improvements)[:200] if improved
                                      else "no measurable improvement over baseline")

        gates["guardrails"] = not candidate.guardrail_violations and candidate.status not in ("rejected", "failed")
        reasons["guardrails"] = ("clean" if gates["guardrails"] else
                                 (candidate.error or "violations recorded")[:200])

        if self.mode in (OperationMode.SAFE, OperationMode.LOCKED):
            gates["policy"] = False
            reasons["policy"] = f"mode {self.mode.value} forbids modifications"
        elif approval is None:
            gates["policy"] = False
            reasons["policy"] = "explicit policy approval required"
        else:
            ok, why = approval.matches(candidate)
            gates["policy"] = ok
            reasons["policy"] = why

        failed = [name for name in MANDATORY_GATES if not gates.get(name, False)]
        awaiting = failed == ["policy"] and approval is None and \
            self.mode not in (OperationMode.SAFE, OperationMode.LOCKED)
        metrics = {
            "baseline_passed": base.passed if base else None,
            "candidate_passed": tests.passed if tests else None,
            "baseline_failed": (base.failed + base.errors) if base else None,
            "candidate_failed": (tests.failed + tests.errors) if tests else None,
            "improvement": comparison.improvement if comparison else None,
            "improvement_pct": comparison.improvement_pct if comparison else None,
            "metric": proposal.metric,
            "mode": self.mode.value,
        }
        return AcceptanceDecision(accepted=not failed, gates=gates, reasons=reasons,
                                  failed_gates=failed, metrics=metrics,
                                  awaiting_approval=awaiting)
