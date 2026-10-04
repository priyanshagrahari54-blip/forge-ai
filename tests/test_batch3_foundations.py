from forge.intelligence.context_assembler import ContextAssembler, ContextItem
from forge.capabilities.adoption_pipeline import CapabilityAdoptionPipeline
from forge.capabilities.registry import CapabilityCandidate
from forge.tools.contracts import ToolRegistry, ToolSpec, ToolInvoker
from forge.evaluation.evidence import Evidence, EvidenceLedger, EvidenceStatus
from forge.workspace.isolation import WorkspaceManager

def test_context_is_bounded_and_sorted():
    c=ContextAssembler(max_items=2,max_chars=100)
    x=c.assemble("p",
        [ContextItem("low","a","memory",.8,1)],
        [ContextItem("high","b","contract",1,10),
         ContextItem("other","c","repo",1,5)])
    assert [i.kind for i in x.items] == ["high","other"]
    assert x.truncated

def test_adoption_pipeline_blocks_unverified():
    c=CapabilityCandidate("x","bad","web","x",license="MIT",quality_score=.99)
    r=CapabilityAdoptionPipeline().evaluate(c)
    assert not r.approved
    assert r.decision.value == "block"

def test_tool_boundary_requires_permissions():
    reg=ToolRegistry()
    reg.register(ToolSpec("t","test",frozenset({"read"}),verified=True))
    inv=ToolInvoker(reg)
    assert inv.invoke("t",lambda:42,approved_permissions=frozenset({"read"})) == 42

def test_evidence_unknown_is_not_pass():
    l=EvidenceLedger.from_entries([Evidence("test",EvidenceStatus.UNKNOWN,"not-run")])
    assert l.status() is EvidenceStatus.UNKNOWN

def test_workspace_isolation():
    ws=WorkspaceManager("/tmp/forge-test").open("project-a","session-1")
    assert ws.session_root.name == "session-1"
    assert ws.project_root.name == "project-a"
    assert ws.project_root.parent.name == "projects"
