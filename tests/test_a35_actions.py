"""A35 desktop action vocabulary and validation tests."""
from __future__ import annotations

import pytest

from forge.desktop.actions import (
    DesktopActionKind,
    DesktopRequest,
    validate_request,
)
from forge.desktop.agent import resource_scope
from forge.security.policy import RESOURCE_OPERATIONS, Resource


def req(action, **kwargs):
    kwargs.setdefault("agent", "tester")
    return DesktopRequest(action, **kwargs)


def test_vocabulary_covers_required_capabilities():
    kinds = {kind.value for kind in DesktopActionKind}
    assert {
        "screenshot", "read_screen", "window", "mouse_move", "mouse_click",
        "keyboard", "launch", "file_select", "clipboard", "file_access",
        "app_action", "process", "system_info", "window_list",
        "process_list",
    } <= kinds
    assert len(kinds) == 15


def test_policy_operations_are_valid_a33_vocabulary():
    desktop_ops = RESOURCE_OPERATIONS[Resource.DESKTOP]
    for kind in DesktopActionKind:
        op = req(kind).policy_operation()
        assert op in desktop_ops, (kind.value, op)


def test_resource_scopes():
    assert resource_scope(req("screenshot")) == "screen"
    assert resource_scope(req("read_screen")) == "screen"
    assert resource_scope(req("launch", target="notepad")) == "app:notepad"
    assert resource_scope(req("window", target="calc")) == "window:calc"
    assert resource_scope(req("keyboard", target="")) == "input"
    assert resource_scope(req("file_access", target="a.txt")) == "file:a.txt"
    assert resource_scope(req("process", target="42")) == "process:42"


def test_observation_classification():
    assert req("screenshot").is_observation()
    assert req("system_info").is_observation()
    assert not req("launch", target="x").is_observation()
    assert not req("keyboard").is_observation()


def test_validation_requires_agent_identity():
    ok, err = validate_request(DesktopRequest("screenshot", agent=""))
    assert not ok and "identity" in err


@pytest.mark.parametrize("action,params", [
    ("mouse_move", {"x": -1, "y": 0}),
    ("mouse_move", {"x": 0}),  # missing y
    ("mouse_move", {"x": "10", "y": 20}),
    ("mouse_click", {"x": 10, "y": 20, "button": "middle4"}),
    ("window", {"op": "destroy"}),
    ("keyboard", {}),
    ("keyboard", {"text": "x" * 2001}),
    ("keyboard", {"keys": ["a"] * 33}),
    ("clipboard", {"op": "paste"}),
    ("clipboard", {"op": "write", "content": "x" * 10001}),
    ("launch", {"args": ["ok; rm -rf /"]}),
    ("launch", {"args": ["`touch x`"]}),
    ("launch", {"args": ["a"] * 17}),
    ("file_access", {"mode": "read", "path": "../secret"}),
    ("file_access", {"mode": "read", "path": "/etc/passwd"}),
    ("file_access", {"mode": "write", "path": "ok.txt"}),
    ("file_access", {"mode": "delete", "path": "C:\\Windows\\x"}),
    ("process", {"pid": 0}),
])
def test_validation_rejects_bad_requests(action, params):
    ok, err = validate_request(req(action, params=params))
    assert not ok, (action, params, err)


def test_validation_accepts_good_requests():
    assert validate_request(req("mouse_move", params={"x": 1, "y": 2}))[0]
    assert validate_request(req("mouse_click", params={"x": 1, "y": 2,
                                                       "button": "right",
                                                       "double": True}))[0]
    assert validate_request(req("keyboard", params={"text": "hello",
                                                    "keys": ["ctrl+c"]}))[0]
    assert validate_request(req("clipboard",
                                params={"op": "write",
                                        "content": "hello"}))[0]
    assert validate_request(req("launch", target="notepad",
                                params={"args": ["report.txt"]}))[0]
    assert validate_request(req("file_access", target="notes/report.txt",
                                params={"mode": "read"}))[0]
    assert validate_request(req("file_access", target="out.txt",
                                params={"mode": "write",
                                        "content": "draft"}))[0]


def test_validation_bounds_target_and_reason():
    ok, err = validate_request(req("screenshot", target="\x01"))
    assert not ok and "control" in err
    ok, err = validate_request(req("screenshot", target="x" * 301))
    assert not ok
    ok, err = validate_request(req("screenshot", reason="x" * 501))
    assert not ok


def test_params_type_restrictions():
    ok, err = validate_request(req("screenshot",
                                   params={"nested": [1, 2, 3, object()]}))
    assert not ok and "unsupported" in err
    ok, err = validate_request(req("screenshot",
                                   params={"deep": {"a": {"b": {"c": {
                                       "d": {"e": {"f": {"g": 1}}}}}}}}))
    assert not ok and "deep" in err


def test_request_to_dict_round_trip():
    request = req("launch", target="notepad", params={"args": ["a"]},
                  task_id="t1", session_id="s1", reason="open editor")
    data = request.to_dict()
    assert data["action"] == "launch"
    assert data["session_id"] == "s1"
    assert request.approval_task_id == "t1"
