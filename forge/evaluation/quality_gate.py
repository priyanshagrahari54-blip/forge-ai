"""Fail-closed aggregation of objective delivery evidence."""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
class GateStatus(str,Enum):
    PASS="PASS"
    FAIL="FAIL"
    UNKNOWN="UNKNOWN"
@dataclass(frozen=True)
class QualityGateResult:
    status:GateStatus
    reasons:tuple[str,...]
class QualityGateAggregator:
    def evaluate(self,outcomes):
        vals=list(outcomes or [])
        if not vals:return QualityGateResult(GateStatus.UNKNOWN,("no evidence",))
        norm=[getattr(v,"value",v) for v in vals]
        if any(v is False or str(v).upper()=="FAIL" for v in norm):return QualityGateResult(GateStatus.FAIL,("required gate failed",))
        if any(v is None or str(v).upper()=="UNKNOWN" for v in norm):return QualityGateResult(GateStatus.UNKNOWN,("required evidence is unknown",))
        return QualityGateResult(GateStatus.PASS,())
