from forge.observability.telemetry import TelemetryRecorder
from forge.agents.router import AgentRouter, AgentCandidate
from forge.browser.provider import BrowserGateway, BrowserAction
from forge.capabilities.packs import get_pack

def test_telemetry_records():
    r=TelemetryRecorder(max_events=2)
    r.trace("task",project="p")
    assert r.snapshot()[0]["name"]=="task"

def test_agent_router_prefers_capability_match():
    a=AgentCandidate("coder",capabilities=frozenset({"python"}),quality=.9)
    b=AgentCandidate("generic",quality=1.0)
    assert AgentRouter().choose(["python"],[a,b]).agent.name=="coder"

def test_browser_gateway_fails_closed():
    try:
        BrowserGateway(enabled=True).act(BrowserAction("click"),approved=True)
        assert False
    except RuntimeError:
        assert True

def test_capability_pack():
    assert "3d" in get_pack("creative").capabilities
