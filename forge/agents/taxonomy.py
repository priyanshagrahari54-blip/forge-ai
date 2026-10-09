"""Capability-oriented agent taxonomy; roles are descriptors, not fake running agents."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable
@dataclass(frozen=True)
class AgentRole:
    name:str
    domain:str
    capabilities:tuple[str,...]
    risk_tier:str="standard"
    def to_dict(self): return {"name":self.name,"domain":self.domain,"capabilities":list(self.capabilities),"risk_tier":self.risk_tier}
CORE_ROLES=(
 AgentRole("planner","control",("requirements","planning","dag")),
 AgentRole("researcher","knowledge",("search","rag","provenance")),
 AgentRole("engineer","software",("coding","debugging","build")),
 AgentRole("tester","verification",("testing","benchmarking","acceptance")),
 AgentRole("security","governance",("security","secrets","permissions"),"high"),
 AgentRole("creative","creative",("image","video","audio","3d")),
 AgentRole("systems","systems",("os","kernel","drivers")),
)
def roles_for(capabilities:Iterable[str])->list[AgentRole]:
    wanted=set(capabilities)
    return [r for r in CORE_ROLES if wanted.intersection(r.capabilities)]
