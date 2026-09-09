"""Computer-use engine (A40): screen → understand → element tree →
propose → PolicyGate → execute, with bounded state.

Security model (all real, all enforced here):

* **Observation only under SAFE/LOCKED modes** — every actuation is
  refused before it reaches the desktop agent.
* **Per-action permission checks** — the A35 DesktopAgent pipeline
  (A33 policy → hard invariants → risk → profile → approvals) is the
  single execution authority; the engine never bypasses it.
* **Risk-based approval** — HIGH/CRITICAL risk actions are escalated
  to operator approval even when a profile would auto-run them.
* **Confirm-dialog fail-closed** — if the current screen shows a
  confirmation dialog (detected from the screen's own text), actuation
  is refused until a fresh, dialog-free screen is observed.
* **Action budget** — a hard cap on executed actions per task.
* **Typed-text redaction** — history/logs/memory only ever see
  redacted parameters; the real payload goes only to the provider at
  execution time.
* **Proposals never execute** — `propose` only authorizes dry runs;
  execution happens exclusively through `act`.
"""
from __future__ import annotations

from typing import Any, Callable

from forge.computer.elements import build_element_tree
from forge.computer.state import (ComputerStateStore, redact_params)
from forge.desktop.actions import DesktopActionKind, DesktopRequest
from forge.desktop.agent import DesktopAgent
from forge.desktop.risk import HIGH, CRITICAL
from forge.security.permissions import OperationMode
from forge.vision.base import VisionResult
from forge.vision.pipeline import propose_actions

OBSERVATION_KINDS = {"screenshot", "read_screen", "window_list",
                     "process_list", "system_info"}

#: Action budget per task (hard cap on executed actions).
DEFAULT_MAX_ACTIONS = 20


class ComputerBudgetExceeded(Exception):
    """The per-task action budget is exhausted (fail closed)."""


class ComputerUseEngine:
    """Vision-driven, policy-gated computer control."""

    def __init__(self, desktop_agent: DesktopAgent, *,
                 max_actions_per_task: int = DEFAULT_MAX_ACTIONS,
                 state_store: ComputerStateStore | None = None,
                 audit: Any = None) -> None:
        if not isinstance(desktop_agent, DesktopAgent):
            raise ValueError("ComputerUseEngine requires a DesktopAgent")
        self.desktop = desktop_agent
        self.max_actions_per_task = max_actions_per_task
        self.state_store = state_store or ComputerStateStore()
        self.audit = audit

    # -- observation & understanding -------------------------------------------

    def observe(self, session_id: str, task_id: str, image: bytes, *,
                goal: str, understanding: dict[str, Any]) -> dict[str, Any]:
        """Record a versioned screen snapshot and return understanding."""
        snapshot = self.state_store.record_snapshot(
            task_id, image, goal=goal, understanding=understanding)
        tree = build_element_tree(understanding)
        return {
            "snapshot_version": snapshot.version,
            "understanding": understanding,
            "element_tree": tree.to_dict(),
            "confirm_dialog": self.state_store.state(
                task_id).confirm_dialog,
            "session_id": session_id,
            "task_id": task_id,
        }

    def propose(self, session_id: str, task_id: str, *,
                goal: str,
                understanding: dict[str, Any],
                profile: Any = None) -> dict[str, Any]:
        """Dry-run proposals from one screen understanding.

        Nothing is executed. Each proposal is authorized through the
        desktop pipeline; approval-required proposals are reported but
        approval requests are only filed when an `act` is attempted.
        """
        result = VisionResult(
            format=understanding.get("format", ""),
            width=understanding.get("width"),
            height=understanding.get("height"),
            summary=understanding.get("summary", ""),
            provider=understanding.get("provider", ""),
            model=understanding.get("model", ""),
            simulation=understanding.get("simulation", True))
        proposals = []
        for proposal in propose_actions(result):
            if proposal["action"] == "blocked_untrusted_instruction":
                proposals.append(proposal)
                continue
            if proposal["action"] == "observe":
                proposal["reason"] = "No actionable regions detected."
                proposals.append(proposal)
                continue
            request = self._request_for(task_id, "mouse_click",
                                        proposal["target"],
                                        {"region": proposal.get("region")},
                                        goal)
            authorization = self.desktop.authorize(request, profile=profile)
            if authorization.allowed:
                proposal["status"] = "proposed"
                proposal["risk"] = authorization.risk
            elif authorization.approval_required:
                proposal["status"] = "approval_required"
                proposal["risk"] = authorization.risk
            else:
                proposal["status"] = "blocked"
                proposal["risk"] = authorization.risk
                proposal["reason"] = authorization.reason
            proposals.append(proposal)
        return {
            "allowed": True,
            "session_id": session_id,
            "task_id": task_id,
            "goal": goal,
            "proposals": proposals[:40],
            "executed": False,
        }

    # -- execution ---------------------------------------------------------------

    def act(self, session_id: str, task_id: str,
            request: DesktopRequest, *, mode: OperationMode,
            approval_token_id: str = "", profile: Any = None,
            approve_callback: Callable | None = None) -> dict[str, Any]:
        """Authorize and execute one computer action, fully guarded."""
        kind = request.action.value if isinstance(
            request.action, DesktopActionKind) else str(request.action)
        state = self.state_store.state(task_id)
        base = {
            "action": kind,
            "target": request.target,
            "params_redacted": redact_params(kind, dict(request.params)),
            "session_id": session_id,
            "task_id": task_id,
            "executed": False,
            "decision": "DENY",
        }
        if state.executed_actions >= self.max_actions_per_task:
            base.update({
                "allowed": False,
                "reason": ("computer-use action budget exhausted "
                           f"({self.max_actions_per_task} executed actions "
                           "per task)"),
                "risk": "MEDIUM",
            })
            return base
        if mode in (OperationMode.SAFE, OperationMode.LOCKED) \
                and kind not in OBSERVATION_KINDS:
            base.update({
                "allowed": False,
                "reason": (f"{mode.value} mode permits observation only; "
                           "no real actions run under SAFE/LOCKED"),
                "risk": "MEDIUM",
            })
            return base
        if state.confirm_dialog and kind not in OBSERVATION_KINDS:
            base.update({
                "allowed": False,
                "reason": ("a confirmation dialog is on screen: "
                           "fail closed until a fresh, dialog-free screen "
                           "is observed"),
                "risk": "HIGH",
            })
            return base
        # Risk escalation: HIGH/CRITICAL never auto-runs, even when the
        # profile would allow it — risk-based approval is a hard rule.
        authorization = self.desktop.authorize(request, profile=profile)
        if authorization.allowed and authorization.risk in (HIGH, CRITICAL):
            from forge.security.approvals import ApprovalRequest
            if self.desktop.store is None:
                base.update({"allowed": False,
                             "reason": ("HIGH-risk action and no approval "
                                        "store configured"),
                             "risk": authorization.risk})
                return base
            permission = authorization.permission_request
            filed = self.desktop.store.submit(ApprovalRequest(
                agent=request.agent, resource=permission.resource,
                operation=permission.operation,
                scopes=tuple([permission.scope]) if permission.scope
                else (),
                task_id=request.task_id,
                reason=request.reason or "computer-use high-risk action",
                consequences=("A HIGH/CRITICAL-risk computer action was "
                              "escalated to operator approval.")))
            base.update({
                "allowed": False,
                "approval_required": True,
                "approval_request_id": filed.id,
                "reason": ("HIGH/CRITICAL-risk computer action escalated "
                           "to operator approval"),
                "risk": authorization.risk,
                "risk_reasons": list(authorization.risk_reasons),
            })
            return base
        result = self.desktop.act(
            request, approval_token_id=approval_token_id,
            approve_callback=approve_callback, profile=profile)
        payload = dict(base)
        payload.update(result.to_dict() if hasattr(result, "to_dict")
                       else {
                           "allowed": result.allowed,
                           "executed": result.executed,
                           "decision": getattr(result, "decision", ""),
                           "reason": getattr(result, "reason", ""),
                           "risk": getattr(result, "risk", "NONE"),
                       })
        payload["params_redacted"] = redact_params(
            kind, dict(request.params))
        self.state_store.record_action(task_id, payload)
        if self.audit is not None and getattr(
                self.audit, "record_event", None):
            self.audit.record_event(
                actor=request.agent, category="computer",
                action=kind,
                outcome="executed" if payload.get("executed") else "denied",
                detail=payload.get("reason", "")[:200])
        return payload

    def cycle(self, session_id: str, task_id: str, image: bytes, *,
              goal: str, understanding: dict[str, Any],
              mode: OperationMode, profile: Any = None,
              max_cycles: int = 5) -> dict[str, Any]:
        """One bounded observe→propose→act round.

        At most one safe proposal is executed per cycle: with a
        simulated screen nothing changes between cycles, so looping
        would repeat the same action — the engine refuses to do that
        and reports honestly. max_cycles bounds the observe/verify
        re-scans.
        """
        if max_cycles < 1:
            max_cycles = 1
        observed = self.observe(session_id, task_id, image, goal=goal,
                                understanding=understanding)
        executed: dict[str, Any] | None = None
        for _round in range(max_cycles):
            proposal_payload = self.propose(
                session_id, task_id, goal=goal,
                understanding=understanding, profile=profile)
            candidate = next(
                (p for p in proposal_payload["proposals"]
                 if p.get("status") == "proposed"
                 and p.get("risk", "MEDIUM") in ("NONE", "LOW")),
                None)
            if candidate is None:
                break
            request = self._request_for(
                task_id, "mouse_click", candidate["target"],
                {"region": candidate.get("region")}, goal)
            executed = self.act(
                session_id, task_id, request, mode=mode, profile=profile)
            break  # one real action per cycle — bounded and honest
        note = ("One safe low-risk proposal executed per cycle; "
                "screen updates come from the provider's next "
                "observation (the simulated provider never changes "
                "the screen, so the loop does not repeat itself).")
        if executed is None:
            note = ("No safe low-risk proposal could auto-run this "
                    "cycle: proposals require operator approval or the "
                    "screen shows no actionable region. The loop never "
                    "repeats an action against an unchanged screen.")
        return {
            "allowed": True,
            "observed": observed,
            "executed_action": executed,
            "note": note,
            "task_id": task_id,
        }

    def history(self, task_id: str) -> dict[str, Any]:
        return self.state_store.history(task_id)

    # -- internals ----------------------------------------------------------------

    def _request_for(self, task_id: str, action: str, target: str,
                     params: dict[str, Any], goal: str) -> DesktopRequest:
        return DesktopRequest(
            DesktopActionKind(action), target=target, params=params,
            agent="forge-computer", task_id=task_id, reason=goal)
