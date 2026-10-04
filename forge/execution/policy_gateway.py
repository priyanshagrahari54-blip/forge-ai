"""Single fail-closed authorization seam for executable actions."""
from __future__ import annotations
from dataclasses import dataclass

@dataclass(frozen=True)
class ActionRequest:
    kind:str
    project_id:str
    session_id:str
    risk:str="low"
    approved:bool=False

@dataclass(frozen=True)
class ActionDecision:
    allowed:bool
    reason:str

class ExecutionPolicyGateway:
    def authorize(self, request:ActionRequest)->ActionDecision:
        if not request.project_id or not request.session_id:return ActionDecision(False,"missing execution scope")
        if request.risk in {"high","critical"} and not request.approved:return ActionDecision(False,"approval required")
        return ActionDecision(True,"authorized")
