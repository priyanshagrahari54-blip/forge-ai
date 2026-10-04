"""Capability-aware agent routing."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Iterable

@dataclass(frozen=True)
class AgentCandidate:
    name:str
    roles:frozenset[str]=frozenset()
    capabilities:frozenset[str]=frozenset()
    quality:float=.5
    availability:bool=True
    cost:float=0.0

@dataclass(frozen=True)
class AgentRoute:
    agent:AgentCandidate
    score:float
    reasons:tuple[str,...]=()

class AgentRouter:
    def rank(self,required:Iterable[str],candidates:Iterable[AgentCandidate])->list[AgentRoute]:
        need=set(required); out=[]
        for c in candidates:
            if not c.availability: continue
            overlap=len(need & (set(c.capabilities)|set(c.roles)))
            score=.7*overlap/max(1,len(need))+.3*max(0,min(1,c.quality))
            out.append(AgentRoute(c,score,(f"matched {overlap}/{max(1,len(need))} requirements",)))
        return sorted(out,key=lambda x:(-x.score,x.agent.cost,x.agent.name))
    def choose(self,required,candidates):
        ranked=self.rank(required,candidates)
        return ranked[0] if ranked else None
