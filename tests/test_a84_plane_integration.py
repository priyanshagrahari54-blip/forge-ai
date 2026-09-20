"""A84 integration — the assistant plane on the real control plane, the
REST surface, cockpit wiring, CLI honesty output and the reality matrix.

Everything here asserts *integration*: one database, one audit stream, one
policy path, delegated execution — and honesty at every seam.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from helpers_a34 import login, make_client, make_plane, make_repo  # noqa: E402

WEB = Path(__file__).parent.parent / "forge" / "cockpit" / "web"


def _setup(tmp_path, *, profile="assisted"):
    plane = make_plane(tmp_path, start=False)
    make_repo(Path(plane.projects["demo"].root))
    return plane, make_client(plane), profile


def _headers(client, profile="assisted"):
    with client:
        _session, _token, headers = login(client, profile=profile)
    return headers


# -- plane attachment ---------------------------------------------------------------

def test_assistant_plane_is_lazily_shared_and_shares_the_plane_database(tmp_path):
    plane, _client, _profile = _setup(tmp_path)
    try:
        first = plane.assistant
        assert plane.assistant is first                 # constructed once
        # same DB → the assistant's tables live beside the plane's own
        tables = {row["name"] for row in plane._db.query(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"assistant_sessions", "assistant_session_turns",
                "assistant_session_refs", "pattern_entities",
                "pattern_relations", "pattern_conflicts",
                "operational_events", "routing_priors",
                "prompt_versions", "long_term_memory"} <= tables
    finally:
        plane.close()


def test_respond_covers_the_triage_exits(tmp_path):
    plane, _client, _profile = _setup(tmp_path)
    try:
        session, _token = plane.create_session("tester", "demo")
        ap = plane.assistant
        q = ap.respond(session, "what is the retry policy in db.py?")
        assert q["kind"] in ("direct_answer", "research")
        assert q["trace_id"] and q["session_id"]
        assert q["intent"] is not None and q["quality"]["verdict"]
        # a destructive verb halts for confirmation *before* any execution
        d = ap.respond(session, "delete all deployment configs on prod",
                       assistant_session_id=q["session_id"])
        assert d["needs_confirmation"] is True
        assert d["kind"] == "confirm"
        # an ambiguous continue asks which real thing was meant
        c = ap.respond(session, "continue the earlier research report",
                       assistant_session_id=q["session_id"])
        assert c["kind"] in ("clarify", "recover_continue")
        if c["kind"] == "clarify":
            assert c["needs_clarification"] is True and c["text"]
        # a coding request is *delegated* to the supervisor: a real task
        t = ap.respond(session, "fix the parser bug and add regression tests",
                       assistant_session_id=q["session_id"])
        assert t["kind"] in ("code", "orchestrate")
        assert t["task_id"].startswith("t-")
        run = plane.get_task(session, t["task_id"])
        assert run is not None and str(run.status)
        # the session remembered the task as its active one
        snap = ap.session_state(session, q["session_id"])
        assert snap["active_task"] == t["task_id"]
    finally:
        plane.close()


def _memory_allowed_policy():
    """Same rule shape the A37 memory tests use: memory ops ALLOW at the
    profile level so the gate proceeds instead of fail-closing to DENY."""
    from forge.security.policy import PermissionPolicy, PermissionRule, Resource
    return PermissionPolicy(rules=[
        PermissionRule(id="m-read", resource=Resource.MEMORY,
                       operation="read", scope="", effect="ALLOW"),
        PermissionRule(id="m-write", resource=Resource.MEMORY,
                       operation="write", scope="", effect="ALLOW"),
        PermissionRule(id="m-delete", resource=Resource.MEMORY,
                       operation="delete", scope="", effect="ALLOW"),
    ])


def test_destructive_memory_ops_pass_the_a33_memory_gate(tmp_path):
    from helpers_a34 import make_plane, make_repo
    plane = make_plane(tmp_path, start=False, policy=_memory_allowed_policy())
    make_repo(Path(plane.projects["demo"].root))
    try:
        session, _token = plane.create_session("tester", "demo")
        ap = plane.assistant
        # create one real long-term memory through the assistant
        out = ap.memory_forget("nonexistent-id", session=session)
        assert out.get("forgotten") is False or out.get("approval_required") \
            is True                                    # honest miss or gated
        r = ap.respond(session, "remember that the demo repo uses pytest "
                                "for the test suite",
                       assistant_session_id="gate-s")
        assert r["memory"]["decision"]["store"] is True
        view = ap.memory_inspect()
        assert view["count"] >= 1
        entry = view["entries"][0]["id"]
        # delete either proceeds (gate allowed) or demands approval — it can
        # never silently bypass; assert both shapes explicitly
        res = ap.memory_delete(entry, session=session)
        assert res.get("deleted") is True or res.get("approval_required") \
            or res.get("allowed") is False
        after = ap.memory_inspect()
        if res.get("deleted"):
            assert after["count"] == view["count"] - 1
        clear = ap.memory_clear("please", session=session)
        assert clear.get("refused")                    # exact phrase required
    finally:
        plane.close()


def test_learning_governance_and_scale_honesty_endpoints(tmp_path):
    plane, _client, _profile = _setup(tmp_path)
    try:
        ap = plane.assistant
        learn = ap.learning_stats()
        assert learn["governance"]["weights_trained"] is False
        assert learn["governance"]["priors_can_relax_filters"] is False
        assert learn["governance"]["conversations_stored_by_default"] is False
        cat = ap.model_scale_catalog()
        for model in cat["models"]:
            if not model["scale"]["disclosed"]:
                assert model["scale"]["band"] == "unknown"
                assert model["scale"]["parameter_count"] is None
        assert "never" in cat["legend"]
        net = ap.networks_status()
        assert net["gateway"]["enabled"] is False
        catalog = ap.tool_catalog()
        assert "NOT permissions" in catalog["honesty"]
        assert catalog["count"] >= 10
        scan = ap.improvement_scan()
        assert scan["governance"]["produces"] == "proposals only"
    finally:
        plane.close()


# -- REST surface ------------------------------------------------------------------------

def test_api_requires_auth_and_csrf(tmp_path):
    plane, client, profile = _setup(tmp_path)
    try:
        with client:
            anon = client.get("/api/v1/assistant/memory")
            assert anon.status_code in (401, 403)
            _s, _t, headers = login(client, profile=profile)
            ok = client.get("/api/v1/assistant/tools", headers=headers)
            assert ok.status_code == 200
            no_csrf = client.post("/api/v1/assistant/respond",
                                  json={"message": "hi"})
            assert no_csrf.status_code == 403
    finally:
        plane.close()


def test_api_conversation_and_state_round_trip(tmp_path):
    plane, client, profile = _setup(tmp_path)
    try:
        with client:
            _s, _t, headers = login(client, profile=profile)
            r = client.post("/api/v1/assistant/respond", headers=headers,
                            json={"message": "summarize what you know "
                                             "about this repo"})
            assert r.status_code == 200, r.text
            payload = r.json()
            sid = payload["session_id"]
            assert payload["kind"]
            state = client.get(f"/api/v1/assistant/sessions/{sid}",
                               headers=headers)
            assert state.status_code == 200
            body = state.json()
            assert body["found"] is True
            assert len(body["history"]) >= 2
            mem = client.get("/api/v1/assistant/memory?q=repo",
                             headers=headers)
            assert mem.status_code == 200
            scale = client.get("/api/v1/assistant/models/scale",
                               headers=headers)
            assert scale.status_code == 200
            assert "never invents" in scale.json()["legend"]
            research = client.post(
                "/api/v1/assistant/research", headers=headers,
                json={"question": "what is the failure-learning flow",
                      "allow_web": False})
            assert research.status_code == 200
            rep = research.json()
            assert rep["status"] in ("COMPLETE", "PARTIAL", "FAILED",
                                     "BLOCKED")
            versions = client.get(f"/api/v1/assistant/prompts?session_id={sid}",
                                  headers=headers)
            assert versions.status_code == 200
            assert versions.json()["count"] >= 1
            bad = client.post("/api/v1/assistant/respond", headers=headers,
                              json={"message": ""})
            assert bad.status_code in (400, 422)       # validation, not 500
    finally:
        plane.close()


def test_api_memory_controls_and_profile(tmp_path):
    plane, client, profile = _setup(tmp_path)
    try:
        with client:
            _s, _t, headers = login(client, profile=profile)
            r = client.post(
                "/api/v1/assistant/respond", headers=headers,
                json={"message": "remember that I prefer terse answers with "
                                 "file citations",
                      "session_id": "api-mem"})
            assert r.status_code == 200
            prof = client.post("/api/v1/assistant/profile", headers=headers,
                               json={"field": "response_style",
                                     "value": "terse"})
            assert prof.status_code == 200
            got = client.get("/api/v1/assistant/profile", headers=headers)
            assert got.status_code == 200
            bad_field = client.post("/api/v1/assistant/profile",
                                    headers=headers,
                                    json={"field": "allow_deploy",
                                          "value": "yes"})
            assert bad_field.status_code == 200        # refused, not crashed
            assert bad_field.json().get("saved") is False
            assert "allow_deploy" in bad_field.json().get("error", "")
            clear = client.post("/api/v1/assistant/memory/clear",
                                headers=headers,
                                json={"confirm": "FORGET EVERYTHING IN THIS "
                                                 "SCOPE"})
            assert clear.status_code == 200
    finally:
        plane.close()


# -- cockpit wiring ------------------------------------------------------------------------

def test_cockpit_assistant_view_is_wired():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    js = (WEB / "app.js").read_text(encoding="utf-8")
    assert '<template id="tpl-assistant">' in html
    assert 'href="#/assistant"' in html
    assert 'data-route="assistant"' in html
    routes_block = js.split("const ROUTES", 1)[1].split("};", 1)[0]
    assert "assistant:" in routes_block
    palette = js.split("const PALETTE_COMMANDS", 1)[1]
    assert "Go to Assistant" in palette
    assert 'id="assistant-send"' in html
    assert "/api/v1/assistant/respond" in js
    # the honesty affordance is in the view text itself
    assert "not stored" in html or "never" in html.lower()


def test_cockpit_palette_knows_the_view():
    from forge.cockpit_palette import VIEWS
    assert {"label": "Go to Assistant", "target": "assistant"} in VIEWS


# -- reality matrix (Stage R) ------------------------------------------------------------------

def test_reality_matrix_entries_are_honest():
    from forge.capabilities.reality import capability_snapshot
    snap = capability_snapshot()
    by_id = {c["capability_id"]: c for c in snap["capabilities"]}
    assert by_id["safe-research-networks"]["status"] == "ARCHITECTURE"
    assert by_id["massive-model-routing"]["simulated"] is False
    assert "route" in by_id["massive-model-routing"]["detail"].lower()
    assert by_id["personal-memory"]["implemented"] is True
    assert snap["honesty_contract"]["memory_storage_is_not_learning"] is True
    assert snap["honesty_contract"][
        "tool_registration_is_not_tool_success"] is True
    assert snap["honesty_contract"]["declared_scale_is_not_hosting"] is True
    # deep research is only READY unless the web layer is configured live
    assert by_id["deep-research"]["status"] in ("READY", "CONFIGURED")


# -- CLI ------------------------------------------------------------------------

def test_cli_assistant_commands_are_honest(tmp_path, monkeypatch, capsys):
    import json as _json

    import forge.cli as cli
    monkeypatch.chdir(tmp_path)
    def run_cli(*argv):
        monkeypatch.setattr(sys, "argv", ["forge", "assistant", *argv])
        code = 0
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 0
        assert code == 0

    run_cli("--db", str(tmp_path / "a.db"), "ask", "hello there", "--json")
    payload = _json.loads(capsys.readouterr().out)
    assert payload["kind"] in ("direct_answer", "research")
    # nothing was stored for a plain greeting
    run_cli("--db", str(tmp_path / "a.db"), "--project", "p",
            "memory", "inspect", "--json")
    rows = _json.loads(capsys.readouterr().out)
    assert rows == [] or all("hello" not in str(r) for r in rows)
    # scale table prints the unknown-means-unknown legend
    run_cli("--db", str(tmp_path / "a.db"), "scale")
    printed = capsys.readouterr().out
    assert "unknown" in printed or "not disclosed" in printed
