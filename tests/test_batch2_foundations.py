from forge.evaluation.performance_lab import PerformanceLab,BenchmarkSpec
from forge.execution.hardware_profile import detect_hardware
from forge.agents.taxonomy import roles_for
from forge.plugins.adapter import PluginManifest,can_load
from forge.architect.planner_v2 import PlannerV2

def test_performance_lab_measures():
    s=BenchmarkSpec("x","latency",target=1.0)
    r=PerformanceLab().measure(lambda: None,s)
    assert len(r.values)==3 and r.mean() is not None

def test_hardware_profile_is_observed():
    p=detect_hardware()
    assert p.cpu_count>=1 and "git" in p.tools

def test_agent_taxonomy_selects_roles():
    assert any(r.name=="tester" for r in roles_for(["testing"]))

def test_plugin_policy_fails_closed():
    m=PluginManifest("x","1",("x",),"pkg:run",("filesystem",),verified=False)
    assert not can_load(m,{"filesystem"})[0]

def test_planner_v2_is_evidence_complete():
    class P:
      class R:
        def to_dict(self): return {}
      requirement=R(); components=[]; dependencies=[]; tasks=[]
      class T:
        def to_dict(self): return {}
      test_plan=T(); benchmark_plan=T(); release_plan=T()
    assert PlannerV2().build(P()).ready()
