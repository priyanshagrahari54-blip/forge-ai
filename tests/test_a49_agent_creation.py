"""Agent creation (A49): validated, honest definitions that grant
nothing."""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from helpers_a34 import login, make_client, make_plane, make_repo  # noqa: E402

from forge.agents.factory import AgentFactory  # noqa: E402

WEB = Path(__file__).parent.parent / "forge" / "cockpit" / "web"


def test_factory_validates_specs():
    factory = AgentFactory("s1")
    with pytest.raises(ValueError):
        factory.create("Bad Name!", "coding", ("coding",))
    with pytest.raises(ValueError):
        factory.create("good-name", "Bad Role", ("coding",))
    with pytest.raises(ValueError):
        factory.create("good-name", "coding", ("mind-reading",))
    with pytest.raises(ValueError):
        factory.create("good-name", "coding", ())
    definition = factory.create(
        "exporter", "coding", ("coding", "review", "coding"))
    assert definition.capabilities == ("coding", "review")  # deduped
    with pytest.raises(ValueError):
        factory.create("exporter", "coding", ("coding",))


def test_factory_real_flag_is_honest():
    factory = AgentFactory("s1")
    bound = factory.create("helper", "coding", ("coding",), bind=True)
    assert bound.real is True
    assert bound.executor == "coder"
    assert "coder executor" in bound.to_dict()["note"]
    plain = factory.create("planner-2", "planning", ("planning",),
                           bind=False)
    assert plain.real is False
    assert "no executor" in plain.to_dict()["note"]


def test_factory_update_invalidates_binding():
    factory = AgentFactory("s1")
    factory.create("helper", "coding", ("coding",), bind=True)
    updated = factory.update("helper", role="custom-role")
    assert updated.real is False
    assert updated.executor == ""
    described = factory.update("helper", description="now documented")
    assert described.description == "now documented"


def test_plane_agent_crud_and_audit(tmp_path):
    plane = make_plane(tmp_path, start=True)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        created = plane.agent_create(
            session, "exporter", "coding", ["coding", "review"])
        assert created["real"] is False
        assert created["name"] == "exporter"
        listed = plane.agent_definitions(session)
        assert [item["name"] for item in listed["agents"]] == ["exporter"]
        updated = plane.agent_update(
            session, "exporter", description="exports csv")
        assert updated["description"] == "exports csv"
        deleted = plane.agent_delete(session, "exporter")
        assert deleted["deleted"]["name"] == "exporter"
        assert plane.agent_definitions(session)["agents"] == []
        audited = plane.audit.query(resource="agents")
        assert {event.operation for event in audited} >= {
            "create", "update", "delete"}
        with pytest.raises(Exception):
            plane.agent_create(session, "Bad Name", "coding", ["coding"])


def test_created_agents_grant_no_power(tmp_path):
    plane = make_plane(tmp_path, start=True)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        before = [item["name"] for item in plane.agent_catalog()]
        plane.agent_create(session, "phantom", "coding", ["coding"])
        after = [item["name"] for item in plane.agent_catalog()]
        assert after == before  # catalog unchanged; no fake capability


def test_agent_api(tmp_path):
    plane = make_plane(tmp_path, start=True)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        assert client.get("/api/v1/agents/defined").status_code == 401
        _session, _token, headers = login(client)
        created = client.post("/api/v1/agents", headers=headers,
                              json={"name": "exporter", "role": "coding",
                                    "capabilities": ["coding"]})
        assert created.status_code == 200
        assert created.json()["real"] is False
        dup = client.post("/api/v1/agents", headers=headers,
                          json={"name": "exporter", "role": "coding",
                                "capabilities": ["coding"]})
        assert dup.status_code == 400
        bad = client.post("/api/v1/agents", headers=headers,
                          json={"name": "exporter-2", "role": "coding",
                                "capabilities": ["mind-reading"]})
        assert bad.status_code == 400
        defined = client.get("/api/v1/agents/defined", headers=headers)
        assert defined.status_code == 200
        assert [item["name"] for item in defined.json()["agents"]] == \
            ["exporter"]
        patched = client.patch("/api/v1/agents/exporter", headers=headers,
                               json={"description": "exports csv"})
        assert patched.status_code == 200
        assert patched.json()["description"] == "exports csv"
        removed = client.delete("/api/v1/agents/exporter",
                                headers=headers)
        assert removed.status_code == 200
        again = client.delete("/api/v1/agents/exporter", headers=headers)
        assert again.status_code == 400


def test_cockpit_agent_builder_contracts():
    html = (WEB / "index.html").read_text()
    js = (WEB / "app.js").read_text()
    assert '<template id="tpl-agentbuilder">' in html
    for hook in ("agent-name", "agent-role", "agent-caps",
                 "agent-create", "agent-create-result",
                 "agent-definitions"):
        assert f'id="{hook}"' in html, hook
    assert 'href="#/agentbuilder"' in html
    routes_block = js.split("const ROUTES", 1)[1].split("};", 1)[0]
    assert "agentbuilder" in routes_block
    palette = js.split("const PALETTE_COMMANDS", 1)[1]
    assert "Go to Agent Builder" in palette
    renderer = js.split("function renderAgentBuilderView", 1)[1].split(
        "\n/* ----------", 1)[0]
    calls = re.findall(r'api\("(/api/v1/[^"]+)"', renderer)
    assert calls and set(calls) == {"/api/v1/agents",
                                    "/api/v1/agents/defined"}, calls
    for forbidden in ("localStorage", "sessionStorage", "document.cookie",
                      "fetch(", "XMLHttpRequest", "onclick="):
        assert forbidden not in renderer, forbidden
