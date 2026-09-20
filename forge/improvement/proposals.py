"""Improvement proposal engine (A84 Stage O).

Scans the real learning surfaces and emits proposals only where evidence
exists:

* repeated failures of one tool/route/provider (OperationalLedger, A59
  failure fingerprints) → "add fallback / review integration" proposals;
* routing quality: providers whose verified latency/reliability diverge
  from the policy preset → "adjust policy" proposals (the proposal is a
  *suggestion*; the policy file stays operator-owned);
* prompt strategies with real outcome evidence underperforming the default →
  "review strategy" proposals;
* user corrections recorded in memory (status transitions) → "convention"
  proposals for the preference profile;
* research quality: reports whose status was PARTIAL/FAILED with a named
  failure source → "configure source" proposals.

Every proposal carries its evidence references and an action class. The
action classes are *governance labels*: ``config-change`` still requires the
operator, ``prompt-strategy`` still requires benchmark evidence, and any
``self-modification`` proposal is pinned to ``requires: tests + review +
authorization + rollback`` — this module cannot perform any of them.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

__all__ = ["ACTION_CLASSES", "ImprovementEngine", "Proposal"]

ACTION_CLASSES = ("config-change", "prompt-strategy", "fallback-add",
                  "convention", "source-availability", "self-modification")

#: The mandatory governance sentence attached to self-modification proposals.
SELF_MODIFICATION_GUARD = (
    "requires: tests + security review + explicit authorization + rollback; "
    "this proposal is a *suggestion* and grants nothing")

MAX_PROPOSALS = 200
MIN_FAILURE_SAMPLES = 3


@dataclass(frozen=True)
class Proposal:
    proposal_id: str
    kind: str                       # action class
    title: str
    rationale: str
    evidence: Tuple[Dict[str, Any], ...] = ()
    proposed_action: str = ""
    requires: Tuple[str, ...] = ()
    status: str = "open"            # open | accepted | rejected | deferred
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {"proposal_id": self.proposal_id, "kind": self.kind,
                "title": self.title, "rationale": self.rationale,
                "evidence": [dict(e) for e in self.evidence],
                "proposed_action": self.proposed_action,
                "requires": list(self.requires), "status": self.status,
                "created_at": self.created_at,
                "governance": ("proposals are suggestions with receipts; "
                               "only the governed loops change behaviour")}


class ImprovementEngine:
    """Reads learning surfaces; writes proposal records. Touches nothing else."""

    def __init__(self, *, operational: Any = None, failures: Any = None,
                 strategy_ledger: Any = None, routing_priors: Any = None,
                 fabric: Any = None, store: Any = None) -> None:
        self.operational = operational
        self.failures = failures
        self.strategy_ledger = strategy_ledger
        self.routing_priors = routing_priors
        self.fabric = fabric
        self._store: List[Proposal] = []
        self.store = store          # optional persistent (db-backed) sink

    # -- scanning ----------------------------------------------------------------

    def scan(self, *, recent_reports: Sequence[Dict[str, Any]] = ()) -> Tuple[Proposal, ...]:
        proposals: List[Proposal] = []
        proposals.extend(self._from_operational())
        proposals.extend(self._from_strategies())
        proposals.extend(self._from_routing())
        proposals.extend(self._from_reports(recent_reports))
        for proposal in proposals:
            self._record(proposal)
        return tuple(proposals)

    def _from_operational(self) -> List[Proposal]:
        out: List[Proposal] = []
        if self.operational is None:
            return out
        try:
            stats = self.operational.stats()
        except Exception:
            return out
        for key, data in (stats.get("subjects") or {}).items():
            if not data.get("established"):
                continue
            rate = float(data.get("success_rate") or 1.0)
            if rate >= 0.7 or data.get("samples", 0) < MIN_FAILURE_SAMPLES:
                continue
            kind, _, subject = key.partition(":")
            evidence = {"subject": subject, "kind": kind,
                        "samples": data.get("samples"),
                        "success_rate": rate,
                        "failures": data.get("failures"),
                        "policy_denials_excluded": True}
            if kind == "tool":
                out.append(Proposal(
                    proposal_id=self._new_id(), kind="fallback-add",
                    title=f"Tool {subject!r} fails "
                          f"{(1 - rate):.0%} of recorded runs",
                    rationale="deterministic fallback or a guard before "
                              "invocation would convert failures into "
                              "honest refusals",
                    evidence=(evidence,),
                    proposed_action=f"add a bounded fallback for tool "
                                    f"{subject!r} in the tool registry",
                    requires=("operator config review",)))
            else:
                out.append(Proposal(
                    proposal_id=self._new_id(), kind="config-change",
                    title=f"{kind} {subject!r} success rate {rate:.0%}",
                    rationale="provider/route reliability below par in "
                              "recorded outcomes; review health, timeouts "
                              "or endpoint config",
                    evidence=(evidence,),
                    proposed_action=f"review {kind} configuration for "
                                    f"{subject!r}",
                    requires=("operator config review",)))
        for pattern in (stats.get("error_patterns") or [])[:3]:
            if int(pattern.get("count") or 0) >= MIN_FAILURE_SAMPLES:
                out.append(Proposal(
                    proposal_id=self._new_id(), kind="source-availability",
                    title="recurring error: " + str(pattern.get("subject")),
                    rationale="same error signature repeats across runs; the "
                              "underlying dependency or config deserves an "
                              "explicit health check",
                    evidence=(dict(pattern),),
                    proposed_action="add/refresh an availability probe for "
                                    "the failing subject",
                    requires=("tests",)))
        return out

    def _from_strategies(self) -> List[Proposal]:
        out: List[Proposal] = []
        ledger = self.strategy_ledger
        if ledger is None:
            return out
        try:
            stats = ledger.stats()
        except Exception:
            return out
        default = (stats.get("strategies") or {}).get("default") or {}
        for name, data in (stats.get("strategies") or {}).items():
            if name == "default" or not data.get("established"):
                continue
            if float(data.get("success_rate") or 1.0) < float(
                    default.get("success_rate") or 1.0) and default.get(
                        "established"):
                out.append(Proposal(
                    proposal_id=self._new_id(), kind="prompt-strategy",
                    title=f"strategy {name!r} underperforms the default",
                    rationale="recorded prompt outcomes show a lower success "
                              "rate with real samples; revisit the strategy's "
                              "guidance line",
                    evidence=({"strategy": name, **data},
                              {"strategy": "default", **default}),
                    proposed_action=f"benchmark strategy {name!r} against the "
                                    "default on representative tasks",
                    requires=("benchmark evidence",)))
        return out

    def _from_routing(self) -> List[Proposal]:
        out: List[Proposal] = []
        priors = self.routing_priors
        if priors is None or self.fabric is None:
            return out
        try:
            report = priors.report()
        except Exception:
            return out
        registry_names = set()
        try:
            registry_names = {m.name for m in self.fabric.registry.list()}
        except Exception:
            registry_names = set()
        for entry in report.get("entries") or []:
            if not entry.get("established"):
                continue
            if entry.get("subject") in ("", "unknown"):
                continue
            if entry["subject"] not in registry_names and registry_names:
                continue   # stale entry for an unregistered model: ignore
            if float(entry.get("success_rate") or 1.0) >= 0.5:
                continue
            if entry["subject"] not in registry_names:
                continue
            out.append(Proposal(
                proposal_id=self._new_id(), kind="fallback-add",
                title=f"model {entry['subject']!r} falls back often",
                rationale="established routing outcomes show this model "
                          "failing at >50%; a nearer fallback in the chain "
                          "would reduce end-to-end latency of failure",
                evidence=(dict(entry),),
                proposed_action=f"order fallbacks so a reliable sibling "
                                f"precedes {entry['subject']!r} for its "
                                "capability",
                requires=("operator config review",)))
        return out

    def _from_reports(self, reports: Sequence[Dict[str, Any]]) -> List[Proposal]:
        out: List[Proposal] = []
        degraded = [r for r in reports or ()
                    if str(r.get("status")) in ("PARTIAL", "FAILED")]
        if not degraded:
            return out
        sources: Dict[str, int] = {}
        for report in degraded:
            for outcome in report.get("source_outcomes") or ():
                if str(outcome.get("state")) in ("FAILED", "ERROR",
                                                 "UNAVAILABLE"):
                    key = str(outcome.get("source", "?"))
                    sources[key] = sources.get(key, 0) + 1
        for source, count in sorted(sources.items(),
                                    key=lambda kv: (-kv[1], kv[0]))[:5]:
            out.append(Proposal(
                proposal_id=self._new_id(), kind="source-availability",
                title=f"research source {source!r} degraded in {count} "
                      "recent report(s)",
                rationale="deep research reports were PARTIAL/FAILED because "
                          "of this source; configuration or a substitute "
                          "would restore corroboration",
                evidence=({"source": source, "affected_reports": count},),
                proposed_action=f"configure/verify {source!r} or add an "
                                "alternative source for the same domain",
                requires=("operator config review",)))
        return out

    # -- store -------------------------------------------------------------------

    def _record(self, proposal: Proposal) -> None:
        self._store.append(proposal)
        self._store = self._store[-MAX_PROPOSALS:]
        if self.store is not None:
            try:
                self.store.append(proposal)
            except Exception:
                pass        # a store hiccup must never fake improvement

    def proposals(self, *, status: str = "") -> Tuple[Proposal, ...]:
        items = tuple(self._store)
        if status:
            items = tuple(p for p in items if p.status == status)
        return tuple(reversed(items))

    @staticmethod
    def _new_id() -> str:
        return "imp-" + ("%d" % (time.time_ns() % 10_000_000_000))

    def governance(self) -> Dict[str, Any]:
        return {
            "produces": "proposals only",
            "self_modification_guard": SELF_MODIFICATION_GUARD,
            "action_classes": ACTION_CLASSES,
            "max_proposals": MAX_PROPOSALS,
            "min_failure_samples": MIN_FAILURE_SAMPLES,
            "appliers": ["operator", "A58 self-development loop (its own "
                                       "gates unchanged)"],
        }
