from __future__ import annotations
"""Lightweight performance lab: measurement contracts, not invented results."""
from dataclasses import dataclass, field
import time
@dataclass(frozen=True)
class BenchmarkSpec:
    name:str
    metric:str
    target:float|None=None
    higher_is_better:bool=False
    min_runs:int=3
@dataclass
class BenchmarkResult:
    name:str
    values:list[float]=field(default_factory=list)
    def mean(self): return sum(self.values)/len(self.values) if self.values else None
    def passed(self,spec:BenchmarkSpec):
        if len(self.values)<spec.min_runs or spec.target is None:return False
        m=self.mean()
        return m>=spec.target if spec.higher_is_better else m<=spec.target
class PerformanceLab:
    def measure(self,fn,spec:BenchmarkSpec)->BenchmarkResult:
        vals=[]
        for _ in range(max(1,spec.min_runs)):
            start=time.perf_counter(); fn(); vals.append(time.perf_counter()-start)
        return BenchmarkResult(spec.name,vals)
    def evaluate(self,result,spec): return {"measured":bool(result.values),"runs":len(result.values),"mean":result.mean(),"passed":result.passed(spec)}
