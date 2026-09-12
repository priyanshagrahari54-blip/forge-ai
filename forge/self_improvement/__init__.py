"""Controlled self-improvement (A81).

Forge analyzes its own performance, identifies weaknesses, proposes
improvements, tests candidate changes in an *isolated* workspace, and
evaluates them against mandatory gates. It is deliberately **not** an
unrestricted self-modification loop:

- every candidate runs in a throwaway copy of the repository;
- acceptance requires tests, security, architecture, regression checks and
  an explicit operator policy approval;
- applying an accepted candidate to the real working tree is a separate,
  operator-initiated step that snapshots originals for exact rollback and
  never commits or merges;
- iteration counts are hard-bounded;
- guardrails refuse any change that touches security controls, permissions,
  policy, approvals, credentials, or protected files without explicit
  authorization — and the guardrails themselves are protected.
"""
from forge.self_improvement.acceptance import (
    AcceptanceDecision,
    AcceptanceGate,
    PolicyApproval,
    SelfImprovementAcceptance,
)
from forge.self_improvement.analysis import (
    SelfAnalysisReport,
    SelfAnalyzer,
    Weakness,
)
from forge.self_improvement.candidate import (
    Candidate,
    CandidateComparison,
    CandidateRunner,
    TestOutcome,
)
from forge.self_improvement.engine import (
    HARD_MAX_ITERATIONS,
    IterationResult,
    SelfImprovementEngine,
)
from forge.self_improvement.evidence import (
    Evidence,
    EvidenceCollector,
    EvidenceKind,
)
from forge.self_improvement.guardrails import (
    GuardrailViolation,
    Guardrails,
    ProtectedFileAuthorization,
)
from forge.self_improvement.ledger import ImprovementLedger
from forge.self_improvement.proposals import (
    ImprovementProposal,
    ProposalGenerator,
    validate_proposal,
)
from forge.self_improvement.rollback import RollbackManager

__all__ = [
    "AcceptanceDecision",
    "AcceptanceGate",
    "Candidate",
    "CandidateComparison",
    "CandidateRunner",
    "Evidence",
    "EvidenceCollector",
    "EvidenceKind",
    "GuardrailViolation",
    "Guardrails",
    "HARD_MAX_ITERATIONS",
    "ImprovementLedger",
    "ImprovementProposal",
    "IterationResult",
    "PolicyApproval",
    "ProposalGenerator",
    "ProtectedFileAuthorization",
    "RollbackManager",
    "SelfAnalysisReport",
    "SelfAnalyzer",
    "SelfImprovementAcceptance",
    "SelfImprovementEngine",
    "TestOutcome",
    "Weakness",
    "validate_proposal",
]
