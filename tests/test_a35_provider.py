"""A35 fake desktop provider state-machine tests.

The fake provider is a deterministic, scriptable simulation used by the
sandboxed desktop E2E environment. These tests verify its state
transitions — real behavior, not mocked success.
"""
from __future__ import annotations

import pytest

from forge.desktop.provider import DesktopProviderError, FakeDesktopProvider


def make(**kwargs):
    return FakeDesktopProvider(**kwargs)


def test_initial_state_is_deterministic():
    provider = make()
    snapshot = provider.snapshot()
    assert snapshot["simulation"] is True
    assert snapshot["focused"] == "desktop"
    assert snapshot["window_count"] == 1
    assert snapshot["processes"] == ["init", "desktop-agent"]
    assert provider.healthy()


def test_launch_creates_window_and_process_and_focuses():
    provider = make()
    result = provider.launch("notepad", {"args": ["draft.txt"]})
    assert result["ok"] and result["app"] == "notepad"
    assert result["pid"] == 5000
    assert provider.focused == "notepad"
    assert "notepad" in provider.windows
    assert provider.windows["notepad"]["visible"]
    procs = {proc["name"]: proc for proc in provider.processes}
    assert procs["notepad"]["args"] == ["draft.txt"]
    # screenshot reflects the focus
    shot = provider.screenshot()
    assert shot["focused"] == "notepad"
    assert any("notepad" in row for row in shot["rows"])


def test_window_operations_mutate_state():
    provider = make()
    provider.launch("calculator", {})
    assert provider.window("calculator", {"op": "minimize"})["ok"]
    assert not provider.windows["calculator"]["visible"]
    assert provider.window("calculator", {"op": "focus"})["ok"]
    assert provider.windows["calculator"]["visible"]
    assert provider.focused == "calculator"
    assert provider.window("calculator", {"op": "close"})["ok"]
    assert "calculator" not in provider.windows
    assert provider.focused == "desktop"  # focus falls back
    missing = provider.window("ghost", {"op": "focus"})
    assert not missing["ok"] and missing["kind"] == "not_found"


def test_mouse_and_keyboard_record_input():
    provider = make()
    assert provider.mouse_move("", {"x": 10, "y": 20})["ok"]
    assert provider.mouse_click("desktop", {"x": 5, "y": 5,
                                            "button": "right"})["ok"]
    assert provider.keyboard("desktop", {"text": "hi"})["ok"]
    kinds = [event["action"] for event in provider.input_log]
    assert kinds == ["mouse_move", "mouse_click", "keyboard"]
    bad = provider.mouse_move("", {"x": 99999, "y": 0})
    assert not bad["ok"] and bad["kind"] == "out_of_bounds"


def test_keyboard_refuses_unfocused_target():
    provider = make()
    provider.launch("notepad", {})
    provider.launch("calculator", {})
    result = provider.keyboard("notepad", {"text": "hello"})
    assert not result["ok"] and result["kind"] == "unfocused"


def test_clipboard_round_trip():
    provider = make()
    assert provider.clipboard("", {"op": "write",
                                   "content": "secret plan"})["chars"] == 11
    read = provider.clipboard("", {"op": "read"})
    assert read["content"] == "secret plan"


def test_file_select_and_file_access_scoped_to_desktop_root():
    provider = make()
    selection = provider.file_select("", {"pattern": "notes/"})
    assert selection["files"] == ["notes/report.txt"]
    write = provider.file_access("notes/new.txt",
                                 {"mode": "write", "content": "draft"})
    assert write["ok"] and write["chars"] == 5
    read = provider.file_access("notes/new.txt", {"mode": "read"})
    assert read["content"] == "draft"
    assert provider.file_access("ghost.txt", {"mode": "read"})["kind"] \
        == "not_found"
    deleted = provider.file_access("notes/new.txt", {"mode": "delete"})
    assert deleted["ok"] and "notes/new.txt" not in provider.files
    assert not provider.file_access("notes/new.txt",
                                    {"mode": "delete"})["ok"]


def test_app_action_and_process_views():
    provider = make()
    provider.launch("notepad", {})
    assert provider.app_action("notepad", {"action": "save"})["ok"]
    assert provider.windows["notepad"]["title"].endswith("saved")
    listing = provider.list_processes()
    names = [proc["name"] for proc in listing["processes"]]
    assert "notepad" in names
    info = provider.process("notepad", {})
    assert info["ok"] and info["process"]["name"] == "notepad"
    assert not provider.process("ghost", {})["ok"]
    sysinfo = provider.system_info()
    assert sysinfo["simulation"] is True and sysinfo["os"]


def test_read_screen_reports_active_window():
    provider = make()
    provider.launch("browser", {})
    state = provider.read_screen()
    assert state["active_window"] == "browser"
    assert state["app"] == "browser"
    assert state["window_count"] == 2


def test_disconnect_and_failure_injection():
    provider = make()
    provider.disconnect()
    assert not provider.healthy()
    with pytest.raises(DesktopProviderError) as exc:
        provider.screenshot()
    assert exc.value.kind == "disconnected"
    provider.reconnect()
    assert provider.healthy()
    provider.fail_next("launch")
    with pytest.raises(DesktopProviderError) as exc:
        provider.launch("notepad", {})
    assert exc.value.kind == "unavailable"
    # one-shot: the next launch succeeds
    assert provider.launch("notepad", {})["ok"]
    provider.fail_all()
    with pytest.raises(DesktopProviderError) as exc:
        provider.system_info()
    assert exc.value.kind == "unavailable"
    provider.fail_all(enabled=False)
    assert provider.system_info()["ok"]


def test_snapshot_never_leaks_clipboard_content():
    provider = make()
    provider.clipboard("", {"op": "write", "content": "super secret"})
    snapshot = provider.snapshot()
    assert snapshot["clipboard_chars"] == 12
    assert "secret" not in str(snapshot)
