"""Self-improvement engine (A81): bounded, ledgered, never self-merging.

One iteration:

    analyze -> propose -> validate proposal (guardrails) -> create isolated
    candidate -> apply change in the candidate -> test candidate + baseline
    -> compare -> acceptance decision (tests, security, architecture,
    regression, improvement, guardrails, policy approval) -> ledger

The engine **never** writes to the live checkout during an iteration. An
accepted candidate is only *applied* by :meth:`apply_accepted`, which is a
separate operator action requiring the same :class:`PolicyApproval`, and
which snapshots originals for :class:`RollbackManager`. Nothing is ever
committed or merged by the engine.

Iterations are hard-bounded (``HARD_MAX_ITERATIONS``) regardless of caller
input, and each run also stops on the first accepted candidate, on empty
analysis, or when every proposal has been tried.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from forge.security.permissions import OperationMode
from forge.self_improvement.acceptance import (
    AcceptanceDecision,
    PolicyApproval,
    SelfImprovementAcceptance,
)
from forge.self_improvement.analysis import SelfAnalysisReport, SelfAnalyzer
from forge.self_improvement.candidate import (
    Candidate,
    CandidateRunner,
    ChangeProducer,
    model_change_producer,
)
from forge.self_improvement.guardrails import Guardrails, GuardrailViolation
from forge.self_improvement.ledger import ImprovementLedger
from forge.self_improvement.proposals import (
    ImprovementProposal,
    ProposalGenerator,
    validate_proposal,
)
from forge.self_improvement.rollback import RollbackManager

HARD_MAX_ITERATIONS = 10
DEFAULT_MAX_ITERATIONS = 3


@dataclass
class IterationResult:
    iteration: int
    proposal: ImprovementProposal | None
    candidate: Candidate | None
    decision: AcceptanceDecision | None
    stopped_reason: str = ""
    duration_seconds: float = 0.0

    @property
    def accepted(self) -> bool:
        return bool(self.decision and self.decision.accepted)

    def to_dict(self) -> dict[str, Any]:
        return {
            "iteration": self.iteration,
            "proposal": self.proposal.to_dict() if self.proposal else None,
            "candidate": self.candidate.to_dict() if self.candidate else None,
            "decision": self.decision.to_dict() if self.decision else None,
            "accepted": self.accepted,
            "stopped_reason": self.stopped_reason,
            "duration_seconds": self.duration_seconds,
        }


@dataclass
class EngineStatus:
    root: str
    iterations_run: int
    max_iterations: int
    hard_max_iterations: int
    proposals: int
    candidates: int
    accepted: int
    rejected: int
    applied: int
    rollbacks: int
    pending_approval: list[str]
    ledger: dict[str, Any]
    awaiting: int = 0
    guardrails: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SelfImprovementEngine:
    def __init__(self, root: str | Path = ".", *,
                 guardrails: Guardrails | None = None,
                 producer: ChangeProducer | None = None,
                 fabric: Any = None, router: Any = None,
                 mode: OperationMode | str = OperationMode.ASSISTED,
                 max_iterations: int = DEFAULT_MAX_ITERATIONS,
                 approval_resolver: Callable[[Candidate, ImprovementProposal], PolicyApproval | None] | None = None,
                 runner: CandidateRunner | None = None,
                 analyzer: SelfAnalyzer | None = None,
                 ledger: ImprovementLedger | None = None,
                 rollback: RollbackManager | None = None,
                 keep_candidates: bool = False) -> None:
        self.root = Path(root).resolve()
        self.guardrails = guardrails or Guardrails()
        self.mode = OperationMode(mode)
        self.max_iterations = max(1, min(int(max_iterations), HARD_MAX_ITERATIONS))
        self.analyzer = analyzer or SelfAnalyzer(self.root)
        self.runner = runner or CandidateRunner(self.root, guardrails=self.guardrails)
        self.ledger = ledger or ImprovementLedger(self.root)
        self.rollback_manager = rollback or RollbackManager(self.root)
        self.acceptance = SelfImprovementAcceptance(mode=self.mode)
        self.approval_resolver = approval_resolver
        self.keep_candidates = keep_candidates
        self._fabric = fabric
        self._router = router
        self._producer = producer
        self.iterations_run = 0
        self.results: list[IterationResult] = []
        self.last_report: SelfAnalysisReport | None = None
        # Candidates that passed every technical gate and now wait for an
        # explicit operator approval (persisted so a later CLI/API process
        # can approve, apply, or discard them).
        self.pending_dir = self.root / ".forge" / "self_improvement" / "pending"

    # -- pending store --------------------------------------------------------------

    def _pending_path(self, candidate_id: str) -> Path:
        safe = "".join(ch for ch in candidate_id if ch.isalnum() or ch in "-_")
        if not safe:
            raise ValueError("invalid candidate id")
        return self.pending_dir / f"{safe}.json"

    def _save_pending(self, candidate: Candidate, proposal: ImprovementProposal,
                      decision: AcceptanceDecision) -> None:
        import json

        self.pending_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "candidate": {**candidate.to_dict(), "changes": candidate.changes,
                          "originals": candidate.originals},
            "proposal": proposal.to_dict(),
            "decision": decision.to_dict(),
        }
        self._pending_path(candidate.id).write_text(
            json.dumps(payload, indent=2, default=str), encoding="utf-8")

    def _load_pending(self, candidate_id: str
                      ) -> tuple[Candidate, ImprovementProposal, AcceptanceDecision] | None:
        import json

        path = self._pending_path(candidate_id)
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        cand = data["candidate"]
        candidate = Candidate(
            id=cand["id"], proposal_id=cand["proposal_id"], root=cand.get("root", ""),
            baseline_root=cand.get("baseline_root", ""), created_at=cand.get("created_at", 0.0),
            changes=dict(cand.get("changes", {})), originals=dict(cand.get("originals", {})),
            change_fingerprint=cand.get("change_fingerprint", ""), status=cand.get("status", ""),
            error=cand.get("error", ""), build_ok=cand.get("build_ok"),
            security=dict(cand.get("security", {})), architecture=dict(cand.get("architecture", {})),
            guardrail_violations=list(cand.get("guardrail_violations", [])))
        from forge.self_improvement.candidate import CandidateComparison, TestOutcome

        if cand.get("tests"):
            t = dict(cand["tests"]); t.pop("ok", None)
            candidate.tests = TestOutcome(**t)
        if cand.get("baseline_tests"):
            t = dict(cand["baseline_tests"]); t.pop("ok", None)
            candidate.baseline_tests = TestOutcome(**t)
        if cand.get("comparison"):
            c = dict(cand["comparison"]); c.pop("regressed", None)
            candidate.comparison = CandidateComparison(**c)
        proposal = ImprovementProposal.from_dict(data["proposal"])
        d = data["decision"]
        decision = AcceptanceDecision(
            accepted=bool(d.get("accepted")), gates=dict(d.get("gates", {})),
            reasons=dict(d.get("reasons", {})), failed_gates=list(d.get("failed_gates", [])),
            metrics=dict(d.get("metrics", {})), decided_at=float(d.get("decided_at", 0.0) or 0.0),
            awaiting_approval=bool(d.get("awaiting_approval")))
        return candidate, proposal, decision

    @property
    def pending(self) -> dict[str, tuple[Candidate, ImprovementProposal, AcceptanceDecision]]:
        rows: dict[str, tuple[Candidate, ImprovementProposal, AcceptanceDecision]] = {}
        if not self.pending_dir.is_dir():
            return rows
        for path in sorted(self.pending_dir.glob("*.json")):
            try:
                loaded = self._load_pending(path.stem)
            except (ValueError, KeyError, TypeError):
                continue
            if loaded is not None:
                rows[path.stem] = loaded
        return rows

    def discard_pending(self, candidate_id: str, *, actor: str = "") -> bool:
        path = self._pending_path(candidate_id)
        if not path.is_file():
            return False
        path.unlink()
        self.ledger.record("rejected", {"reason": "discarded by operator", "actor": actor},
                           candidate_id=candidate_id)
        return True

    # -- change producer ------------------------------------------------------------

    def producer(self) -> ChangeProducer:
        if self._producer is None:
            self._producer = model_change_producer(fabric=self._fabric, router=self._router)
        return self._producer

    # -- analysis / proposals -------------------------------------------------------

    def analyze(self, **collect_kwargs: Any) -> SelfAnalysisReport:
        report = self.analyzer.analyze(**collect_kwargs)
        self.last_report = report
        self.ledger.record("analysis", {"summary": report.summary(), "metrics": report.metrics,
                                        "sources": report.sources})
        return report

    def propose(self, report: SelfAnalysisReport | None = None,
                *, limit: int = 10) -> list[ImprovementProposal]:
        report = report or self.last_report or self.analyze()
        generator = ProposalGenerator(self.guardrails, history=self.ledger.outcomes())
        proposals = generator.generate(report, limit=limit)
        for proposal in proposals:
            self.ledger.record("proposal", proposal.to_dict(), proposal_id=proposal.id,
                               weakness_id=proposal.weakness_id)
        return proposals

    # -- one candidate --------------------------------------------------------------

    def evaluate_proposal(self, proposal: ImprovementProposal,
                          *, report: SelfAnalysisReport | None = None,
                          targets: Iterable[str] | None = None,
                          metric_probe: Callable[[Path], float | None] | None = None,
                          approval: PolicyApproval | None = None) -> tuple[Candidate, AcceptanceDecision]:
        known = [e.id for e in (report or self.last_report).evidence] if (report or self.last_report) else None
        problems = validate_proposal(proposal, known, self.guardrails)
        if problems:
            violations = [{"rule": "inadmissible proposal", "detail": p} for p in problems]
            self.ledger.record("rejected", {"reasons": problems, "failed_gates": ["admissibility"],
                                            "weakness_id": proposal.weakness_id,
                                            "changed_files": []},
                               proposal_id=proposal.id)
            raise GuardrailViolation(violations)
        candidate = self.runner.create(proposal)
        self.ledger.record("candidate", {"status": "created", "root": candidate.root},
                           candidate_id=candidate.id, proposal_id=proposal.id)
        try:
            try:
                self.runner.apply(candidate, proposal, self.producer())
            except GuardrailViolation as exc:
                candidate.status = "rejected"
                candidate.guardrail_violations = list(getattr(exc, "violations", []) or
                                                      [{"rule": "guardrail", "detail": str(exc)}])
            if candidate.status == "applied":
                self.runner.evaluate(candidate, proposal, targets=targets, metric_probe=metric_probe)
            if approval is None and self.approval_resolver is not None and candidate.status in ("evaluated", "regressed"):
                approval = self.approval_resolver(candidate, proposal)
            decision = self.acceptance.decide(candidate, proposal, approval)
        finally:
            if not self.keep_candidates:
                self.runner.cleanup(candidate)
        self.ledger.record("decision", {**decision.to_dict(), "candidate": candidate.to_dict()},
                           candidate_id=candidate.id, proposal_id=proposal.id)
        if decision.accepted or decision.awaiting_approval:
            self._save_pending(candidate, proposal, decision)
            self.ledger.record("pending", {"awaiting_approval": decision.awaiting_approval,
                                           "accepted": decision.accepted,
                                           "change_fingerprint": candidate.change_fingerprint,
                                           "changed_files": sorted(candidate.changes)},
                               candidate_id=candidate.id, proposal_id=proposal.id)
        else:
            self.ledger.record("rejected", {"failed_gates": decision.failed_gates,
                                            "reasons": decision.reasons,
                                            "weakness_id": proposal.weakness_id,
                                            "changed_files": sorted(candidate.changes)},
                               candidate_id=candidate.id, proposal_id=proposal.id)
        return candidate, decision

    # -- bounded loop ---------------------------------------------------------------

    def run(self, *, max_iterations: int | None = None,
            targets: Iterable[str] | None = None,
            metric_probe: Callable[[Path], float | None] | None = None,
            collect_kwargs: dict[str, Any] | None = None,
            stop_on_accept: bool = True) -> list[IterationResult]:
        bound = self.max_iterations if max_iterations is None else max(
            1, min(int(max_iterations), HARD_MAX_ITERATIONS))
        results: list[IterationResult] = []
        report = self.analyze(**(collect_kwargs or {}))
        proposals = self.propose(report, limit=bound + 1)
        if not proposals:
            result = IterationResult(0, None, None, None, "no admissible proposals")
            self.ledger.record("iteration", result.to_dict())
            self.results.append(result)
            return [result]
        for index, proposal in enumerate(proposals, 1):
            if index > bound:
                break
            started = time.perf_counter()
            self.iterations_run += 1
            try:
                candidate, decision = self.evaluate_proposal(
                    proposal, report=report, targets=targets, metric_probe=metric_probe)
                result = IterationResult(index, proposal, candidate, decision,
                                         duration_seconds=round(time.perf_counter() - started, 3))
            except GuardrailViolation as exc:
                result = IterationResult(index, proposal, None, None,
                                         stopped_reason=str(exc),
                                         duration_seconds=round(time.perf_counter() - started, 3))
            self.ledger.record("iteration", {"iteration": index, "accepted": result.accepted,
                                             "stopped_reason": result.stopped_reason,
                                             "duration_seconds": result.duration_seconds},
                               proposal_id=proposal.id,
                               candidate_id=result.candidate.id if result.candidate else "")
            results.append(result)
            self.results.append(result)
            parked = bool(result.decision and (result.decision.accepted
                                               or result.decision.awaiting_approval))
            if parked and stop_on_accept:
                if not result.accepted:
                    result.stopped_reason = "candidate passed technical gates; awaiting policy approval"
                break
        if len(results) == bound and len(proposals) > bound:
            results[-1].stopped_reason = results[-1].stopped_reason or f"iteration bound {bound} reached"
        return results

    # -- operator actions -----------------------------------------------------------

    def approve(self, candidate_id: str, *, approved_by: str, reason: str = "",
                change_fingerprint: str = "") -> tuple[PolicyApproval, AcceptanceDecision]:
        """Record an operator's policy approval for a pending candidate.

        The approval is bound to the candidate's change fingerprint (the
        caller may pass the fingerprint it reviewed; a mismatch is refused).
        Re-runs the acceptance decision with the approval attached; the
        technical gates are re-evaluated from the stored evidence, so an
        approval can never rescue a failed test/security/architecture gate.
        """
        entry = self._load_pending(candidate_id)
        if entry is None:
            raise LookupError(f"no pending candidate {candidate_id!r}")
        candidate, proposal, _previous = entry
        if change_fingerprint and change_fingerprint != candidate.change_fingerprint:
            raise PermissionError("approval fingerprint does not match the pending change")
        approval = PolicyApproval(candidate_id=candidate.id,
                                  change_fingerprint=candidate.change_fingerprint,
                                  approved_by=approved_by, reason=reason)
        decision = self.acceptance.decide(candidate, proposal, approval)
        self.ledger.record("decision", {**decision.to_dict(), "candidate": candidate.to_dict(),
                                        "approval": approval.to_dict()},
                           candidate_id=candidate.id, proposal_id=proposal.id)
        if decision.accepted:
            self._save_pending(candidate, proposal, decision)
        else:
            self._pending_path(candidate_id).unlink()
            self.ledger.record("rejected", {"failed_gates": decision.failed_gates,
                                            "reasons": decision.reasons,
                                            "weakness_id": proposal.weakness_id},
                               candidate_id=candidate.id, proposal_id=proposal.id)
        return approval, decision

    def apply_accepted(self, candidate_id: str, approval: PolicyApproval) -> dict[str, Any]:
        """Write an accepted candidate's changes to the live checkout.

        Requires the pending *accepted* decision **and** a matching
        approval; snapshots originals for rollback; never commits or merges.
        """
        entry = self._load_pending(candidate_id)
        if entry is None:
            raise LookupError(f"no pending candidate {candidate_id!r}")
        candidate, proposal, decision = entry
        if not decision.accepted:
            raise PermissionError("apply refused: candidate is not accepted "
                                  f"(failed gates: {', '.join(decision.failed_gates)})")
        ok, why = approval.matches(candidate)
        if not ok:
            raise PermissionError(f"apply refused: {why}")
        if self.mode in (OperationMode.SAFE, OperationMode.LOCKED):
            raise PermissionError(f"apply refused: mode {self.mode.value} forbids modifications")
        # The live files must still be what the candidate was built from;
        # otherwise the tested change is not the change being applied.
        for path, original in candidate.originals.items():
            target = self.root / path
            current = target.read_text(encoding="utf-8") if target.is_file() else None
            if current != original:
                raise PermissionError(
                    f"apply refused: {path} changed since the candidate was evaluated")
        result = self.rollback_manager.apply(candidate.id, candidate.changes, guardrails=self.guardrails)
        self._pending_path(candidate_id).unlink()
        self.ledger.record("applied", {**result, "weakness_id": proposal.weakness_id,
                                       "approved_by": approval.approved_by,
                                       "metrics": decision.metrics,
                                       "committed": False},
                           candidate_id=candidate.id, proposal_id=proposal.id)
        return {**result, "committed": False,
                "note": "changes written to the working tree only; review and commit manually"}

    def rollback(self, candidate_id: str, *, actor: str = "") -> dict[str, Any]:
        result = self.rollback_manager.rollback(candidate_id)
        self.ledger.record("rollback", {**result, "actor": actor}, candidate_id=candidate_id)
        return result

    # -- status ---------------------------------------------------------------------

    def status(self) -> EngineStatus:
        entries = self.ledger.entries()
        by_kind: dict[str, int] = {}
        for entry in entries:
            by_kind[entry["kind"]] = by_kind.get(entry["kind"], 0) + 1
        accepted = sum(1 for e in entries if e["kind"] == "decision" and e["payload"].get("accepted"))
        rejected = sum(1 for e in entries if e["kind"] == "decision"
                       and not e["payload"].get("accepted")
                       and not e["payload"].get("awaiting_approval"))
        return EngineStatus(
            root=str(self.root), iterations_run=self.iterations_run,
            max_iterations=self.max_iterations, hard_max_iterations=HARD_MAX_ITERATIONS,
            proposals=by_kind.get("proposal", 0), candidates=by_kind.get("candidate", 0),
            accepted=accepted, rejected=rejected, applied=by_kind.get("applied", 0),
            rollbacks=by_kind.get("rollback", 0), pending_approval=sorted(self.pending),
            awaiting=by_kind.get("pending", 0),
            ledger=self.ledger.summary(), guardrails=Guardrails.describe())

    def dashboard(self) -> dict[str, Any]:
        """Everything the dashboard shows, derived from the ledger."""
        entries = self.ledger.entries()
        proposals = [e["payload"] for e in entries if e["kind"] == "proposal"]
        decisions = [e for e in entries if e["kind"] == "decision"]
        candidates = []
        for entry in decisions:
            payload = entry["payload"]
            cand = payload.get("candidate", {})
            candidates.append({
                "candidate_id": entry["refs"].get("candidate_id"),
                "proposal_id": entry["refs"].get("proposal_id"),
                "status": cand.get("status"), "accepted": payload.get("accepted"),
                "awaiting_approval": payload.get("awaiting_approval", False),
                "failed_gates": payload.get("failed_gates", []),
                "gates": payload.get("gates", {}), "reasons": payload.get("reasons", {}),
                "changed_files": cand.get("changed_files", []),
                "comparison": cand.get("comparison"),
                "at": entry["at"],
            })
        applied = [{"candidate_id": e["refs"].get("candidate_id"), **e["payload"], "at": e["at"]}
                   for e in entries if e["kind"] == "applied"]
        rejected = [{"candidate_id": e["refs"].get("candidate_id"),
                     "proposal_id": e["refs"].get("proposal_id"), **e["payload"], "at": e["at"]}
                    for e in entries if e["kind"] == "rejected"]
        rollbacks = [{"candidate_id": e["refs"].get("candidate_id"), **e["payload"], "at": e["at"]}
                     for e in entries if e["kind"] == "rollback"]
        evidence = [e.to_dict() for e in self.last_report.evidence] if self.last_report else []
        weaknesses = [w.to_dict() for w in self.last_report.weaknesses] if self.last_report else []
        if not evidence:
            analysis_file = self.analyzer.output_dir / "analysis.json"
            if analysis_file.is_file():
                try:
                    import json
                    data = json.loads(analysis_file.read_text(encoding="utf-8"))
                    evidence = list(data.get("evidence", []))
                    weaknesses = list(data.get("weaknesses", []))
                except (OSError, ValueError):
                    pass
        return {
            "status": self.status().to_dict(),
            "proposals": proposals[-50:],
            "candidates": candidates[-50:],
            "accepted": [c for c in candidates if c["accepted"]][-50:],
            "applied": applied[-50:],
            "rejected": rejected[-50:],
            "rollbacks": rollbacks[-50:],
            "pending_approval": [
                {"candidate_id": cid, "proposal_id": p.id, "title": p.title,
                 "change_fingerprint": c.change_fingerprint, "changed_files": sorted(c.changes),
                 "metrics": d.metrics, "accepted": d.accepted,
                 "awaiting_approval": d.awaiting_approval, "gates": d.gates}
                for cid, (c, p, d) in self.pending.items()],
            "evidence": evidence[-200:],
            "weaknesses": weaknesses,
            "guardrails": Guardrails.describe(),
        }
