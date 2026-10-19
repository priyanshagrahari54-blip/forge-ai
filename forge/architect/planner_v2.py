"""Evidence-first planner pipeline built on the existing ProjectArchitect."""
from dataclasses import dataclass,field
from typing import Any
@dataclass
class PlanningGraph:
    stages:list[str]=field(default_factory=lambda:["requirements","architecture","components","dependencies","dag","tests","benchmarks","release"])
    evidence:dict[str,Any]=field(default_factory=dict)
    def missing_evidence(self): return [s for s in self.stages if s not in self.evidence]
    def ready(self): return not self.missing_evidence()
class PlannerV2:
    def build(self,plan)->PlanningGraph:
        g=PlanningGraph()
        g.evidence.update({
          "requirements":plan.requirement.to_dict(),
          "architecture":[x.to_dict() for x in plan.components],
          "components":[x.name for x in plan.components],
          "dependencies":[x.to_dict() for x in plan.dependencies],
          "dag":[x.to_dict() for x in plan.tasks],
          "tests":plan.test_plan.to_dict(),
          "benchmarks":plan.benchmark_plan.to_dict(),
          "release":plan.release_plan.to_dict()})
        return g
