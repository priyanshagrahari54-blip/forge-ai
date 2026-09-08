"""Computer-use engine tests (A40): bounded state, redaction, fail-closed
guards, risk escalation, honest cycles."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from helpers_a39 import b64, make_png  # noqa: E402

from forge.computer.engine import ComputerUseEngine  # noqa: E402
from forge.computer.elements import build_element_tree  # noqa: E402
from forge.computer.state import (ComputerStateStore,  # noqa: E402
                                  detect_confirm_dialog, redact_params)
from forge.desktop.actions import (DesktopActionKind,  # noqa: E402
                                   DesktopRequest)
from forge.desktop.agent import DesktopAgent  # noqa: E402
from forge.desktop.profiles import DesktopProfile  # noqa: E402
from forge.desktop.provider import FakeDesktopProvider  # noqa: E402
from forge.security.permissions import OperationMode  # noqa: E402
from forge.security.policy import (  # noqa: E402
    PermissionPolicy,
    PermissionRule,
    Resource,
)
from forge.vision.simulated import SimulatedVisionProvider  # noqa: E402


def policy() -> PermissionPolicy:
    return PermissionPolicy(rules=[
        PermissionRule(id="v-analyze", resource=Resource.VISION,
                       operation="analyze", scope="", effect="ALLOW"),
        PermissionRule(id="v-execute", resource=Resource.VISION,
                       operation="execute", scope="", effect="ALLOW"),
        PermissionRule(id="d-click", resource=Resource.DESKTOP,
                       operation="mouse_click", scope="", effect="ALLOW"),
        PermissionRule(id="d-move", resource=Resource.DESKTOP,
                       operation="mouse_move", scope="", effect="ALLOW"),
        PermissionRule(id="d-key", resource=Resource.DESKTOP,
                       operation="keyboard", scope="", effect="ALLOW"),
        PermissionRule(id="d-proc", resource=Resource.DESKTOP,
                       operation="process", scope="", effect="ALLOW"),
        PermissionRule(id="d-obs", resource=Resource.DESKTOP,
                       operation="screenshot", scope="", effect="ALLOW"),
    ])


def engine(profile_mode: str = "autonomous",
           max_actions: int = 20) -> ComputerUseEngine:
    from forge.security.approvals import ApprovalStore
    agent = DesktopAgent(FakeDesktopProvider(), policy=policy(),
                         store=ApprovalStore(),
                         profile=DesktopProfile(mode=profile_mode))
    eng = ComputerUseEngine(agent, max_actions_per_task=max_actions)
    # Task-scope grants: input covers mouse/keyboard actuation,
    # process covers process actions (incl. terminate).
    eng.desktop.scope_checker.grant("t", ["input", "process:*"])
    return eng


def understanding(text_chunks: tuple[str, ...] = ()) -> dict:
    provider = SimulatedVisionProvider()
    return provider.analyze(make_png(
        text_chunks=text_chunks)).to_dict()


def click_request(task_id: str = "t") -> DesktopRequest:
    return DesktopRequest(DesktopActionKind.MOUSE_CLICK, agent="forge-computer",
                          task_id=task_id, params={"x": 10, "y": 10})


def move_request(task_id: str = "t") -> DesktopRequest:
    """LOW-risk actuation: AUTO under the autonomous profile."""
    return DesktopRequest(DesktopActionKind.MOUSE_MOVE, agent="forge-computer",
                          task_id=task_id, params={"x": 10, "y": 20})


def test_snapshots_are_versioned_and_bounded():
    store = ComputerStateStore()
    image = make_png()
    for index in range(10):
        snapshot = store.record_snapshot("t", image, goal="g",
                                         understanding=understanding())
        assert snapshot.version == index + 1
    versions = store.state("t").snapshot_versions()
    assert len(versions) == 8  # oldest evicted
    assert versions == [3, 4, 5, 6, 7, 8, 9, 10]


def test_redact_params_never_leaks_typed_text():
    redacted = redact_params("keyboard", {"text": "my secret password"})
    assert redacted["text"] == "<redacted: 18 chars>"
    assert "my secret password" not in str(redacted)
    nested = redact_params("app_action", {"args": {"path": "/etc/shadow"}})
    assert "shadow" not in str(nested)
    untouched = redact_params("mouse_click", {"x": 3, "y": 4})
    assert untouched == {"x": 3, "y": 4}


def test_confirm_dialog_detection_fails_closed():
    assert detect_confirm_dialog({"findings": [
        {"content": "Are you sure you want to proceed?"}]})
    assert detect_confirm_dialog({"findings": [
        {"content": "Press OK/Cancel to continue"}]})
    assert not detect_confirm_dialog({"findings": [
        {"content": "Welcome to the editor"}]})
    assert not detect_confirm_dialog({"findings": []})


def test_element_tree_is_bounded_and_honest():
    tree = build_element_tree(understanding())
    assert tree.root.kind == "screen"
    assert len(tree.root.children) >= 1
    for node in tree.nodes:
        assert 0.0 <= node.confidence <= 1.0
    assert tree.find("nope") is None


def test_safe_and_locked_modes_deny_all_actuation():
    for mode in (OperationMode.SAFE, OperationMode.LOCKED):
        result = engine().act("s", "t", click_request(), mode=mode)
        assert result["allowed"] is False
        assert result["executed"] is False
        assert "observation only" in result["reason"]


def test_confirm_dialog_on_screen_fails_closed():
    eng = engine()
    eng.state_store.record_snapshot(
        "t", make_png(), goal="g",
        understanding=understanding(text_chunks=("Are you sure?",)))
    result = eng.act("s", "t", click_request(), mode=OperationMode.AUTONOMOUS)
    assert result["allowed"] is False
    assert "dialog" in result["reason"]


def test_action_budget_is_a_hard_cap():
    eng = engine(max_actions=2)
    for _index in range(2):
        result = eng.act("s", "t", move_request(),
                         mode=OperationMode.AUTONOMOUS)
        assert result["executed"] is True
    exhausted = eng.act("s", "t", move_request(),
                        mode=OperationMode.AUTONOMOUS)
    assert exhausted["allowed"] is False
    assert "budget exhausted" in exhausted["reason"]


def test_high_risk_action_escalates_to_approval():
    request = DesktopRequest(
        DesktopActionKind.PROCESS, target="browser", agent="forge-computer",
        task_id="t", params={"op": "terminate"})
    result = engine().act("s", "t", request, mode=OperationMode.AUTONOMOUS)
    assert result["allowed"] is False
    assert result["approval_required"] is True
    assert result["approval_request_id"]
    assert "escalat" in result["reason"].lower() or "risk" in \
        result["reason"].lower()
    assert result["risk"] in ("HIGH", "CRITICAL")


def test_proposals_never_execute():
    eng = engine()
    proposed = eng.propose("s", "t", goal="click OK",
                           understanding=understanding())
    assert proposed["executed"] is False
    assert proposed["proposals"]
    for proposal in proposed["proposals"]:
        assert proposal.get("status") != "executed"
    # History shows no executed actions after proposing.
    assert eng.history("t")["executed_actions"] == 0


def test_cycle_executes_at_most_one_safe_action_and_is_honest():
    eng = engine()
    payload = eng.cycle("s", "t", make_png(), goal="click OK",
                        understanding=understanding(),
                        mode=OperationMode.AUTONOMOUS)
    assert payload["allowed"] is True
    executed = payload["executed_action"]
    if executed is not None:
        assert executed["executed"] is True
    assert "cycle" in payload["note"].lower()
    assert eng.history("t")["executed_actions"] <= 1


def test_history_is_redacted_by_construction():
    eng = engine()
    request = DesktopRequest(
        DesktopActionKind.KEYBOARD, target="editor", agent="forge-computer",
        task_id="t", params={"text": "the launch codes are 1234"})
    eng.act("s", "t", request, mode=OperationMode.ASSISTED)
    history = eng.history("t")
    assert "launch codes" not in str(history)
    assert any("<redacted" in str(action) for action in history["actions"])
