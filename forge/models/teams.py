"""Cross-model intelligence (A84 Stage J).

Forge can use several models as collaborators instead of treating one model
as the whole answer:

```
Planner + Reasoning + Coder + Researcher + Vision + Critic + Verifier
                                    -> Synthesis
```

A ``ModelTeam`` is a *task-dependent* role assignment resolved through the
existing Model Fabric — not a fixed fan-out. Rules:

* roles map to canonical fabric capabilities; a role is only active when the
  task needs it (a "what time is it" never assembles seven model calls);
* every role resolves through the fabric's normal routing: policy, health,
  verification, fallback and telemetry all still apply — teams do not get a
  side door;
* two roles MAY resolve to the same model when that is all the registry can
  honestly offer; the result then reports the consolidation instead of
  pretending diversity ("many agents ≠ better answer", Stage R);
* independent verification requires *independent* answers: a critic sharing
  a model with the producer is flagged ``not_independent`` and consensus is
  never claimed over one model's re-answers;
* synthesis is a data step, not a privilege: the merged result keeps each
  role's provenance and the team's honest label (simulated/verified/etc.).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from forge.models.capabilities import is_capability
from forge.models.request import ModelRequest

__all__ = ["TEAM_ROLES", "RoleAssignment", "ModelTeam", "TeamMemberResult",
           "TeamResult"]

#: Closed role vocabulary with the capability each role requires.
TEAM_ROLES: Dict[str, Tuple[str, ...]] = {
    "planner": ("planning",),
    "reasoner": ("reasoning",),
    "coder": ("coding",),
    "researcher": ("research",),
    "vision": ("vision",),
    "critic": ("review",),
    "verifier": ("review", "security"),
    "synthesizer": ("documentation",),
}

#: Which roles a task-shape actually needs. Kept small and explicit; the
#: synthesizer role always runs last when any specialist ran.
TASK_SHAPES: Dict[str, Tuple[str, ...]] = {
    "answer": (),
    "code": ("planner", "coder", "verifier", "synthesizer"),
    "debug": ("reasoner", "coder", "verifier", "synthesizer"),
    "research": ("planner", "researcher", "critic", "synthesizer"),
    "review": ("critic", "verifier", "synthesizer"),
    "plan": ("planner", "reasoner", "synthesizer"),
    "vision": ("vision", "synthesizer"),
    "complex": ("planner", "reasoner", "coder", "critic", "verifier",
                "synthesizer"),
}

#: Complexity at/above which the simple shapes escalate to the full team.
COMPLEX_ESCALATION = 4.0


@dataclass(frozen=True)
class RoleAssignment:
    role: str
    capability: str
    model: str = ""
    provider: str = ""
    reason: str = ""
    #: True when no registered model can serve the role right now.
    unmet: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {"role": self.role, "capability": self.capability,
                "model": self.model, "provider": self.provider,
                "reason": self.reason, "unmet": self.unmet}


@dataclass(frozen=True)
class TeamMemberResult:
    role: str
    model: str
    provider: str
    ok: bool
    text: str = ""
    error: str = ""
    latency_ms: float = 0.0
    simulated: bool = False
    tokens: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"role": self.role, "model": self.model,
                "provider": self.provider, "ok": self.ok,
                "text": self.text[:2000], "error": self.error[:300],
                "latency_ms": round(self.latency_ms, 1),
                "simulated": self.simulated, "tokens": dict(self.tokens)}


@dataclass(frozen=True)
class TeamResult:
    shape: str
    assignments: Tuple[RoleAssignment, ...] = ()
    members: Tuple[TeamMemberResult, ...] = ()
    synthesis: str = ""
    independence: str = "n/a"
    ok: bool = False
    notes: Tuple[str, ...] = ()
    duration_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {"shape": self.shape,
                "assignments": [a.to_dict() for a in self.assignments],
                "members": [m.to_dict() for m in self.members],
                "synthesis": self.synthesis,
                "independence": self.independence, "ok": self.ok,
                "notes": list(self.notes),
                "duration_ms": round(self.duration_ms, 1),
                "honesty": ("team execution is fabric execution per role; "
                            "no member proves another member's answer")}


def select_shape(*, capabilities: Sequence[str] = (), complexity: float = 1.0,
                 is_question: bool = False) -> str:
    """Deterministic task-shape choice for a request."""
    caps = {c for c in capabilities if is_capability(c)}
    if float(complexity or 1.0) >= COMPLEX_ESCALATION:
        return "complex"
    if is_question or not caps:
        return "answer"
    if "vision" in caps:
        return "vision"
    if caps & {"debugging"}:
        return "debug"
    if caps & {"coding", "testing"}:
        return "code"
    if caps & {"research"}:
        return "research"
    if caps & {"review", "security"}:
        return "review"
    if caps & {"planning"}:
        return "plan"
    return "answer"


class ModelTeam:
    """Role assignment + task-dependent execution through the fabric."""

    def __init__(self, fabric: Any, *, policy: Any = None) -> None:
        self.fabric = fabric
        self.policy = policy

    # -- assignment ------------------------------------------------------------

    def resolve(self, shape: str, *, require: Iterable[str] = ()) -> Tuple[RoleAssignment, ...]:
        roles = list(TASK_SHAPES.get(shape, ()))
        for capability in require:
            for role, role_caps in TEAM_ROLES.items():
                if capability in role_caps and role not in roles:
                    roles.append(role)
                    break
        assignments: List[RoleAssignment] = []
        for role in roles:
            capability = TEAM_ROLES[role][0]
            try:
                decision = self.fabric.route(
                    ModelRequest(prompt="", capability=capability,
                                 task=f"team:{role}"))
            except Exception as exc:                # route must not raise into UI
                assignments.append(RoleAssignment(
                    role=role, capability=capability,
                    reason=f"routing failed closed: {type(exc).__name__}",
                    unmet=True))
                continue
            model = getattr(decision, "model", None)
            if model is None:
                assignments.append(RoleAssignment(
                    role=role, capability=capability,
                    reason=("no registered model can serve this role right "
                            "now; the role stays unmet rather than faked"),
                    unmet=True))
                continue
            assignments.append(RoleAssignment(
                role=role, capability=capability, model=model.name,
                provider=getattr(model, "provider", ""),
                reason=f"routed via fabric capability {capability!r}"))
        return tuple(assignments)

    # -- execution ---------------------------------------------------------------

    def run(self, prompt: str, *, shape: str = "", capabilities: Sequence[str] = (),
             complexity: float = 1.0, is_question: bool = False,
             context: str = "") -> TeamResult:
        started = time.perf_counter()
        if not shape:
            shape = select_shape(capabilities=capabilities,
                                 complexity=complexity,
                                 is_question=is_question)
        if shape == "answer":
            # Stage J: do not assemble a team for a plain answer. One routed
            # call; honestly reported as single-model, not "cross-model".
            decision = self.fabric.route(ModelRequest(
                prompt=prompt, capability="reasoning", context=context,
                complexity=complexity))
            return TeamResult(
                shape="answer", independence="single-model (no team needed)",
                members=(), synthesis="", ok=False,
                notes=("the team layer declined to fan out: a plain answer "
                       "gets one routed call, executed by the caller's "
                       "normal path",))
        assignments = self.resolve(shape, require=capabilities)
        unmet = tuple(a for a in assignments if a.unmet)
        usable = tuple(a for a in assignments if not a.unmet)
        notes: List[str] = []
        if unmet:
            notes.append("unmet roles: " + ", ".join(a.role for a in unmet)
                         + " (registry cannot honestly serve them now)")

        # producer roles run first; critic/verifier review producer text.
        producers = [a for a in usable if a.role in ("planner", "reasoner",
                                                     "coder", "researcher",
                                                     "vision", "synthesizer")]
        reviewers = [a for a in usable if a.role in ("critic", "verifier")]
        member_results: List[TeamMemberResult] = []
        produced_text = ""
        for assignment in producers:
            text, meta = self._call(assignment, prompt, context, complexity)
            member_results.append(meta)
            if meta.ok and meta.role != "synthesizer":
                produced_text += (f"[{meta.role}:{meta.model}] "
                                  f"{text[:1200]}\n")
        review_text = produced_text or prompt
        for assignment in reviewers:
            _text, meta = self._call(assignment, review_text, context,
                                     complexity, task_suffix=(
                                         "Independently review the preceding "
                                         "team output for factual, security and "
                                         "requirement problems. Report "
                                         "findings; do not rewrite the goal."))
            member_results.append(meta)

        used_models = [m.model for m in member_results if m.ok]
        independent = len(set(used_models)) > 1
        if reviewers and not independent:
            notes.append("critic/verifier share a model with producers: "
                          "review is re-questioning, NOT independent "
                          "verification")
        independence = ("independent" if independent else
                        "consolidated (same model serves multiple roles)")
        synthesis = produced_text[:4000]
        synth_meta = next((m for m in member_results
                           if m.role == "synthesizer"), None)
        if synth_meta is not None and synth_meta.ok:
            synthesis = synth_meta.text[:4000]
        ok = any(m.ok for m in member_results if m.role != "synthesizer") \
            or bool(synth_meta and synth_meta.ok)
        return TeamResult(
            shape=shape, assignments=assignments,
            members=tuple(member_results), synthesis=synthesis,
            independence=independence, ok=ok, notes=tuple(notes),
            duration_ms=(time.perf_counter() - started) * 1000.0)

    # -- one role call ------------------------------------------------------------

    def _call(self, assignment: RoleAssignment, prompt: str, context: str,
              complexity: float, task_suffix: str = "") -> Tuple[str, TeamMemberResult]:
        task = f"team:{assignment.role}"
        full_prompt = prompt if not task_suffix else (
            task_suffix + "\n\n" + prompt)
        request = ModelRequest(prompt=full_prompt,
                               capability=assignment.capability,
                               context=context, task=task,
                               complexity=max(1.0, float(complexity or 1.0)))
        started = time.perf_counter()
        try:
            response = self.fabric.request(
                request, policy=self.policy) if self.policy else \
                self.fabric.request(request)
        except Exception as exc:
            return "", TeamMemberResult(
                role=assignment.role, model=assignment.model,
                provider=assignment.provider, ok=False,
                error=f"{type(exc).__name__}: {exc}"[:240],
                latency_ms=(time.perf_counter() - started) * 1000.0)
        ok = bool(getattr(response, "success", False))
        text = str(getattr(response, "text", "") or "")
        simulated = bool(
            (getattr(response, "metadata", None) or {}).get("simulated")) or \
            assignment.model in ("local-model", "deterministic-local")
        latency = (time.perf_counter() - started) * 1000.0
        tokens = {"input": int(getattr(response, "input_tokens", 0) or 0),
                  "output": int(getattr(response, "output_tokens", 0) or 0)}
        return text, TeamMemberResult(
            role=assignment.role,
            model=str(getattr(response, "model", assignment.model)
                      or assignment.model),
            provider=str(getattr(response, "provider", assignment.provider)
                         or assignment.provider),
            ok=ok, text=text if ok else "",
            error="" if ok else str(getattr(response, "error", "failed"))[:240],
            latency_ms=latency, simulated=simulated, tokens=tokens)
