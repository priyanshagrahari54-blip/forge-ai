from forge.architect.decomposition import build_graph
from forge.capabilities.lazy import LazyCapabilityLoader
from forge.capabilities.mcp_policy import review_server
from forge.capabilities.broker import CapabilityBroker
from forge.capabilities.registry import CapabilityRegistry
from forge.intelligence.engineering_knowledge import EngineeringKnowledgeStore, KnowledgeObject
from forge.models.routing_intelligence import RoutingCandidate, RoutingContext, rank

def test_task_graph_frontier_and_validation():
    g=build_graph(["web"], quality=True)
    assert not g.validate()
    assert [t.id for t in g.frontier()] == ["requirements"]

def test_knowledge_is_project_scoped(tmp_path):
    s=EngineeringKnowledgeStore(tmp_path/"k.db")
    s.upsert(KnowledgeObject("p1","architecture","api",{"x":1},"test"))
    s.upsert(KnowledgeObject("p2","architecture","api",{"x":2},"test"))
    assert s.query("p1")[0].data["x"] == 1
    assert s.query("p2")[0].data["x"] == 2
    s.close()

def test_routing_prefers_quality_when_quality_is_high():
    ctx=RoutingContext(quality=1.0,cost_sensitive=False)
    items=[RoutingCandidate("a","text",quality_score=.9),RoutingCandidate("b","text",quality_score=.5)]
    assert rank(items,ctx)[0][1].model=="a"

def test_lazy_loader_does_not_execute():
    loader=LazyCapabilityLoader(CapabilityBroker(CapabilityRegistry()))
    d=loader.prepare("video-editing")
    assert d.action=="discover_then_verify" and d.requires_approval

def test_mcp_policy_fail_closed():
    assert not review_server("x").allowed
    assert review_server("x",verified=True,license_ok=True,security_ok=True,
                          permissions=["read"]).allowed
