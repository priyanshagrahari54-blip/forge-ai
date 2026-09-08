"""AI Council (A45): simulated deliberation, honest consensus,
preserved minorities, advisory-only status."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from helpers_a34 import login, make_client, make_plane, make_repo  # noqa: E402

from forge.council.engine import (  # noqa: E402
    AICouncilEngine,
    SimulatedCouncilModel,
)


def test_council_opinions_are_simulated_and_marked():
    engine = AICouncilEngine()
    result = engine.deliberate("Should the exporter be rewritten?")
    assert result["simulation"] is True
    assert len(result["opinions"]) == 3
    for opinion in result["opinions"]:
        assert opinion["simulation"] is True
        assert "simulated" in opinion["reasoning"].lower()
    # No pretend unanimity machinery: stances come from real opinions.
    assert result["stance"] in {"approve", "reject"}
    with pytest.raises(ValueError):
        engine.deliberate("")
    with pytest.raises(ValueError):
        engine.deliberate("x" * 4001)


def test_disagreement_flagged_and_minority_preserved():
    engine = AICouncilEngine()
    result = engine.deliberate("Deploy on a Friday?")
    # Default members: alpha approve, beta approve, gamma reject.
    assert result["stance"] == "approve"
    assert result["consensus"] is False
    assert result["disagreements"] == ["gamma"]
    assert len(result["minority_opinions"]) == 1
    assert "gamma" in result["final_answer"].upper() or \
        "gamma" in result["final_answer"]
    assert "disagreement flagged" in result["final_answer"].lower()
    assert result["confidence"] == pytest.approx(0.667, abs=0.001)


def test_unanimity_is_honest():
    engine = AICouncilEngine(members=[
        SimulatedCouncilModel("one", stance="approve",
                              stance_label="cautious"),
        SimulatedCouncilModel("two", stance="approve",
                              stance_label="cautious"),
    ])
    result = engine.deliberate("Keep the current design?")
    assert result["consensus"] is True
    assert result["disagreements"] == []
    assert result["minority_opinions"] == []
    assert result["confidence"] == pytest.approx(1.0)


def test_council_needs_members():
    engine = AICouncilEngine(members=[])
    with pytest.raises(ValueError):
        engine.deliberate("Anything at all")


def test_plane_council_is_advisory_and_audited(tmp_path):
    plane = make_plane(tmp_path, start=True)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        payload, _token, headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        caps = plane.council_capabilities(session)
        assert caps["advisory_only"] is True
        assert caps["simulation"] is True
        verdict = plane.council_convene(
            session, "Should we add a cache to the indexer?")
        assert verdict["simulation"] is True
        assert verdict["final_answer"].startswith("Council verdict")
        history = plane.council_history(session)
        assert len(history["deliberations"]) == 1
        audited = plane.audit.query(resource="council")
        assert audited and audited[-1].decision.value == "ALLOW"


def test_council_api(tmp_path):
    plane = make_plane(tmp_path, start=True)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        assert client.get("/api/v1/council/capabilities").status_code == 401
        _session, _token, headers = login(client)
        caps = client.get("/api/v1/council/capabilities",
                          headers=headers).json()
        assert caps["advisory_only"] is True
        convene = client.post("/api/v1/council/convene", headers=headers,
                              json={"question": "Is the plan sound?"})
        assert convene.status_code == 200
        body = convene.json()
        assert body["simulation"] is True
        assert body["opinions"]
        assert client.post("/api/v1/council/convene", headers=headers,
                           json={"question": ""}).status_code == 400
        history = client.get("/api/v1/council/history", headers=headers)
        assert history.status_code == 200
        assert history.json()["deliberations"]
