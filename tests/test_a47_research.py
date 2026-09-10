"""Research / intelligence (A47): evidence-based answers, never
fabricated."""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


from helpers_a34 import login, make_client, make_plane, make_repo  # noqa: E402

from forge.research.engine import ResearchEngine  # noqa: E402

WEB = Path(__file__).parent.parent / "forge" / "cockpit" / "web"


def make_module_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "helpers.py").write_text("def util(): return 1\n")
    (root / "core.py").write_text(
        "import helpers\n\n\n"
        "class Engine:\n"
        "    def run(self):\n"
        "        return helpers.util()\n")
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "test_core.py").write_text(
        "from core import Engine\n\n"
        "def test_run():\n"
        "    assert Engine().run() == 1\n")


def test_ask_symbol_question_with_real_evidence(tmp_path):
    make_module_repo(tmp_path)
    engine = ResearchEngine(tmp_path)
    result = engine.ask("where is Engine defined?")
    assert result["honest"] is True
    assert result["confidence"] > 0
    assert result["evidence"], result
    for item in result["evidence"]:
        assert Path(tmp_path, item["path"]).exists(), item
    assert any(item["path"] == "core.py" and item["kind"] == "symbol:class"
               for item in result["evidence"])


def test_ask_dependency_question(tmp_path):
    make_module_repo(tmp_path)
    engine = ResearchEngine(tmp_path)
    result = engine.ask("what imports helpers?")
    assert result["evidence"], result
    kinds = {item["kind"] for item in result["evidence"]}
    assert kinds & {"dependency", "dependent"}, kinds
    assert all(Path(tmp_path, item["path"]).exists()
               for item in result["evidence"])


def test_ask_test_coverage_question(tmp_path):
    make_module_repo(tmp_path)
    engine = ResearchEngine(tmp_path)
    result = engine.ask("which tests cover Engine?")
    assert result["evidence"], result
    assert any(item["kind"] == "test"
               and item["path"] == "tests/test_core.py"
               for item in result["evidence"])


def test_ask_without_evidence_is_honest(tmp_path):
    make_module_repo(tmp_path)
    engine = ResearchEngine(tmp_path)
    result = engine.ask("where is the flux capacitor?")
    assert result["evidence"] == []
    assert result["confidence"] == 0.0
    assert "no evidence" in result["answer"].lower()
    assert ".py" not in result["answer"]


def test_report_matches_real_counts(tmp_path):
    make_module_repo(tmp_path)
    engine = ResearchEngine(tmp_path)
    report = engine.report()
    assert report["source_file_count"] == 3
    assert report["test_file_count"] == 1
    assert report["package_count"] == 0
    assert report["evidence_based"] is True
    assert report["root"] == str(tmp_path.resolve())


def test_plane_research_and_audit(tmp_path):
    plane = make_plane(tmp_path, start=True)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        answer = plane.research_ask(session, "where is health defined?")
        assert answer["evidence"], answer
        assert all(
            Path(plane.projects["demo"].root, item["path"]).exists()
            for item in answer["evidence"])
        report = plane.research_report(session)
        assert report["source_file_count"] == 2
        assert report["test_file_count"] == 1
        audited = plane.audit.query(resource="research")
        assert len(audited) >= 2
        assert audited[-1].decision.value == "ALLOW"


def test_research_api(tmp_path):
    plane = make_plane(tmp_path, start=True)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        assert client.get("/api/v1/research/report").status_code == 401
        _session, _token, headers = login(client)
        ask = client.post("/api/v1/research/ask", headers=headers,
                          json={"question": "where is health defined?"})
        assert ask.status_code == 200
        assert ask.json()["evidence"]
        assert client.post("/api/v1/research/ask", headers=headers,
                           json={"question": ""}).status_code == 400
        report = client.get("/api/v1/research/report", headers=headers)
        assert report.status_code == 200
        assert report.json()["source_file_count"] == 2


def test_cockpit_research_view_contracts():
    html = (WEB / "index.html").read_text()
    js = (WEB / "app.js").read_text()
    assert '<template id="tpl-research">' in html
    for hook in ("research-report", "research-answer",
                 "research-input", "research-ask"):
        assert f'id="{hook}"' in html, hook
    assert 'href="#/research"' in html
    routes_block = js.split("const ROUTES", 1)[1].split("};", 1)[0]
    assert "research" in routes_block
    palette = js.split("const PALETTE_COMMANDS", 1)[1]
    assert "Go to Research" in palette
    renderer = js.split("function renderResearchView", 1)[1].split(
        "\n/* ----------", 1)[0]
    calls = re.findall(r'api\("(/api/v1/[^"]+)"', renderer)
    assert calls and set(calls) == {"/api/v1/research/ask",
                                    "/api/v1/research/report"}, calls
    for forbidden in ("localStorage", "sessionStorage", "document.cookie",
                      "fetch(", "XMLHttpRequest", "onclick="):
        assert forbidden not in renderer, forbidden
