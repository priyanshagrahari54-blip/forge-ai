"""Requirement -> evidence-backed implementation DAG helpers."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Iterable

@dataclass(frozen=True)
class DecomposedTask:
    id: str
    title: str
    stage: str
    depends_on: tuple[str,...]=()
    acceptance: str=""
    capabilities: tuple[str,...]=()

@dataclass
class TaskGraph:
    tasks: list[DecomposedTask]=field(default_factory=list)
    def validate(self)->list[str]:
        ids={t.id for t in self.tasks}; problems=[]
        for t in self.tasks:
            for d in t.depends_on:
                if d not in ids: problems.append(f"{t.id}: unknown dependency {d}")
        state={}
        graph={t.id:t.depends_on for t in self.tasks}
        def visit(n):
            if state.get(n)==1:return True
            if state.get(n)==2:return False
            state[n]=1
            if any(visit(d) for d in graph.get(n,())): return True
            state[n]=2; return False
        if any(visit(n) for n in graph): problems.append("task graph contains a cycle")
        return problems
    def frontier(self,done:Iterable[str]=())->list[DecomposedTask]:
        done=set(done); return [t for t in self.tasks if t.id not in done and all(d in done for d in t.depends_on)]

def build_graph(capabilities:list[str], quality:bool=True)->TaskGraph:
    tasks=[DecomposedTask("requirements","Lock requirements and acceptance criteria","requirements",
                          acceptance="contract is executable"),
           DecomposedTask("architecture","Define architecture and components","architecture",("requirements",),
                          acceptance="dependencies are explicit"),
           DecomposedTask("implementation","Implement selected capabilities","implementation",("architecture",),
                          tuple(capabilities))]
    if quality:
        tasks += [DecomposedTask("tests","Run universal tests","verification",("implementation",),
                                 acceptance="required tests pass"),
                  DecomposedTask("acceptance","Verify contract and quality bar","acceptance",("tests",),
                                 acceptance="all required gates pass")]
    return TaskGraph(tasks)
