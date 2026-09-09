"""Agent self-development (A58): honest learning from real failures.

An agent can only change itself through validated, bounded edits
derived from its own recorded failed runs. Nothing here invents
capabilities: a proposal either writes a bounded journal note into
the definition's description/metrics or is refused as not
self-fixable (for example, an unbound executor). Every application
is validated by the factory, counted against caps, audited, and
never touches executors, bindings, policy, or other agents.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

MAX_APPLIED_PER_SESSION = 8
MAX_APPLIED_PER_AGENT = 3
MAX_NOTE = 200
NOT_FIXABLE = ("without a bound executor", "no executor is available")


@dataclass
class SelfDevProposal:
    proposal_id: str
    agent: str
    kind: str
    note: str = ""
    reason: str = ""
    created_at: float = field(default_factory=time.time)
    applied: bool = False
    applied_at: float = 0.0
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"proposal_id": self.proposal_id, "agent": self.agent,
                "kind": self.kind, "note": self.note,
                "reason": self.reason, "created_at": self.created_at,
                "applied": self.applied, "applied_at": self.applied_at,
                "metrics": dict(self.metrics)}


class SelfDevLedger:
    """Session-bounded proposals and decisions."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self._proposals: dict[str, SelfDevProposal] = {}
        self._applied: dict[str, int] = {}

    @property
    def applied_per_session(self) -> int:
        return sum(self._applied.values())

    def analyze(self, agent: str, failures: list[dict[str, Any]]
                ) -> SelfDevProposal:
        recent = [entry for entry in failures
                  if entry.get("agent") == agent
                  and not entry.get("success", True)]
        if not recent:
            proposal = SelfDevProposal(
                uuid.uuid4().hex[:12], agent, "none",
                reason="no failed runs on record for this agent")
            self._proposals[proposal.proposal_id] = proposal
            return proposal
        last_error = (recent[-1].get("error") or "").strip()
        if any(marker in last_error.lower() for marker in NOT_FIXABLE):
            proposal = SelfDevProposal(
                uuid.uuid4().hex[:12], agent, "none",
                reason="latest failure is not self-fixable: "
                       + last_error[:160])
            self._proposals[proposal.proposal_id] = proposal
            return proposal
        if "limit" in last_error.lower():
            note = "quota refusal recorded: " + last_error[:120]
        else:
            note = "failure journal: " + last_error[:150]
        proposal = SelfDevProposal(
            uuid.uuid4().hex[:12], agent, "failure-note",
            note=note[:MAX_NOTE],
            metrics={"failures": len(recent),
                     "last_failure_at": round(time.time(), 3)})
        self._proposals[proposal.proposal_id] = proposal
        return proposal

    def get(self, proposal_id: str) -> SelfDevProposal | None:
        return self._proposals.get(proposal_id)

    def applied_count(self, agent: str) -> int:
        return self._applied.get(agent, 0)

    def mark_applied(self, proposal: SelfDevProposal) -> None:
        proposal.applied = True
        proposal.applied_at = time.time()
        self._applied[proposal.agent] = \
            self._applied.get(proposal.agent, 0) + 1

    def entries(self, agent: str | None = None
                ) -> list[dict[str, Any]]:
        entries = [proposal.to_dict()
                   for proposal in self._proposals.values()
                   if agent is None or proposal.agent == agent]
        return sorted(entries, key=lambda item: item["created_at"])
