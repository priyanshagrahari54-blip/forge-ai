"""General conversation engine (A43): classification, routing, real
answers, memory, honesty."""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from helpers_a34 import login, make_client, make_plane, make_repo  # noqa: E402

from forge.conversation.engine import GeneralConversationEngine  # noqa: E402
from forge.security.policy import (  # noqa: E402
    PermissionPolicy,
    PermissionRule,
    Resource,
)

WEB = Path(__file__).parent.parent / "forge" / "cockpit" / "web"

ALLOW_POLICY = PermissionPolicy(rules=[
    PermissionRule(id="m-ok", resource=Resource.MEMORY,
                   operation="write", scope="", effect="ALLOW"),
    PermissionRule(id="m-read", resource=Resource.MEMORY,
                   operation="read", scope="", effect="ALLOW"),
    PermissionRule(id="v-ok", resource=Resource.VOICE,
                   operation="command", scope="", effect="ALLOW"),
    PermissionRule(id="vi-ok", resource=Resource.VISION,
                   operation="analyze", scope="", effect="ALLOW"),
])


def test_classification_is_deterministic():
    engine = GeneralConversationEngine(
        submit_task=lambda message: {}, answer_question=lambda q: "",
        remember=lambda m: True, remember_history=lambda t: None)
    assert engine.classify("add CSV export to the project") == "task_request"
    assert engine.classify("what does this project do?") == "question"
    assert engine.classify("explain the repository structure") == "question"
    assert engine.classify("how do you build X?") == "question"
    assert engine.classify("hello") == "greeting"
    assert engine.classify("remember that I prefer terse answers") \
        == "preference"
    assert engine.classify("thanks for the help") == "chat"


def test_task_requests_route_to_submit():
    submitted = []
    engine = GeneralConversationEngine(
        submit_task=lambda message: submitted.append(message)
        or {"task_id": "t-1", "requirement": message},
        answer_question=lambda q: "", remember=lambda m: True,
        remember_history=lambda t: None)
    reply = engine.converse("add CSV export to the project")
    assert reply["kind"] == "task_request"
    assert submitted == ["add CSV export to the project"]
    assert "t-1" in reply["reply"]


def test_preferences_are_remembered_or_honestly_refused():
    remembered = []
    engine = GeneralConversationEngine(
        submit_task=lambda message: {}, answer_question=lambda q: "",
        remember=lambda m: remembered.append(m) or True,
        remember_history=lambda t: None)
    reply = engine.converse("remember that I prefer terse answers")
    assert reply["kind"] == "preference"
    assert reply["remembered"] is True
    assert remembered
    failing = GeneralConversationEngine(
        submit_task=lambda message: {}, answer_question=lambda q: "",
        remember=lambda m: False, remember_history=lambda t: None)
    denied = failing.converse("remember that I prefer terse answers")
    assert denied["remembered"] is False
    assert "couldn't remember" in denied["reply"].lower()


def test_questions_never_fabricate():
    engine = GeneralConversationEngine(
        submit_task=lambda message: {},
        answer_question=lambda q: "REAL ANSWER", remember=lambda m: True,
        remember_history=lambda t: None)
    reply = engine.converse("what does this project do?")
    assert reply["kind"] == "question"
    assert reply["reply"] == "REAL ANSWER"


def test_history_is_bounded():
    engine = GeneralConversationEngine(
        submit_task=lambda message: {}, answer_question=lambda q: "answer",
        remember=lambda m: True, remember_history=lambda t: None)
    for index in range(12):
        engine.converse(f"hello there {index}")
    assert len(engine.history) <= 16


def test_plane_answers_with_real_repository_data(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=ALLOW_POLICY)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        reply = plane.converse(session, "what does this project do?")
        assert reply["kind"] == "question"
        assert "source files" in reply["reply"]
        reply = plane.converse(session, "how many tasks do I have?")
        assert "running" in reply["reply"].lower()
        honest = plane.converse(session, "what is the meaning of life?")
        assert honest["kind"] == "question"
        assert "don't have a real answer" in honest["reply"].lower()


def test_plane_conversation_creates_real_tasks(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=ALLOW_POLICY)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        reply = plane.converse(
            session, "analyze the repository structure for me")
        assert reply["kind"] == "task_request"
        task_id = reply["task"]["task_id"]
        assert plane.runs.get(task_id) is not None
        history = plane.conversation_history(session)
        assert history["history"]


def test_plane_preferences_persist_via_memory(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=ALLOW_POLICY)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        reply = plane.converse(session, "remember that I prefer terse answers")
        assert reply["remembered"] is True
        recall = plane.converse(session, "what do you remember?")
        assert "terse answers" in recall["reply"]


def test_conversation_api(tmp_path):
    plane = make_plane(tmp_path, start=True, policy=ALLOW_POLICY)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        assert client.get("/api/v1/conversation").status_code == 401
        _session, _token, headers = login(client)
        reply = client.post("/api/v1/conversation", headers=headers,
                            json={"message": "hello"})
        assert reply.status_code == 200
        assert reply.json()["kind"] == "greeting"
        assert client.post("/api/v1/conversation", headers=headers,
                           json={"message": ""}).status_code == 400
        history = client.get("/api/v1/conversation", headers=headers)
        assert history.status_code == 200
        assert history.json()["history"]


def test_cockpit_conversation_view_contracts():
    html = (WEB / "index.html").read_text()
    js = (WEB / "app.js").read_text()
    assert '<template id="tpl-conversation">' in html
    for hook in ("conversation-log", "conversation-input",
                 "conversation-send"):
        assert f'id="{hook}"' in html, hook
    assert 'href="#/conversation"' in html
    routes_block = js.split("const ROUTES", 1)[1].split("};", 1)[0]
    assert "conversation" in routes_block
    palette = js.split("const PALETTE_COMMANDS", 1)[1]
    assert "Go to Conversation" in palette
    renderer = js.split("function renderConversationView", 1)[1].split(
        "\n/* ----------", 1)[0]
    calls = re.findall(r'api\("(/api/v1/[^"]+)"', renderer)
    assert calls and set(calls) == {"/api/v1/conversation"}, calls
    for forbidden in ("localStorage", "sessionStorage", "document.cookie",
                      "fetch(", "XMLHttpRequest", "onclick="):
        assert forbidden not in renderer, forbidden
