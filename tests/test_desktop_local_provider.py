"""The local desktop provider drives the real machine (or refuses honestly)."""
from __future__ import annotations

import base64
import os
import shutil
import signal
import stat
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from forge.desktop.agent import DesktopAgent
from forge.desktop.bridge import DesktopBridge, DesktopBridgeError
from forge.desktop.local_provider import LocalDesktopProvider
from forge.desktop.provider import FakeDesktopProvider


def _provider(tmp_path, **kwargs):
    kwargs.setdefault("file_root", tmp_path / "desk")
    return LocalDesktopProvider(**kwargs)


def _no_gui(monkeypatch):
    """Simulate a headless box: no display, no helper tools."""
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("XDG_SESSION_TYPE", "")
    monkeypatch.setattr(shutil, "which", lambda *args, **kwargs: None)


def _exe(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _tiny_png(width: int = 64, height: int = 32) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + ihdr
            + b"\x00\x00\x00\x00" + struct.pack(">I", 0) + b"IEND"
            + b"\xae\x42\x60\x82")


# -- always-real surface -------------------------------------------------------------


def test_system_info_is_real(tmp_path):
    info = _provider(tmp_path).system_info()
    assert info["ok"] is True
    assert info["simulation"] is False
    assert info["pid"] == os.getpid()
    assert info["cpu_count"] == os.cpu_count()
    assert info["cwd"] == os.getcwd()
    assert "Linux" in info["os"] or info["os"]


def test_process_table_contains_self(tmp_path):
    provider = _provider(tmp_path)
    listing = provider.list_processes()
    assert listing["ok"] is True
    assert listing["processes"]
    me = [proc for proc in listing["processes"]
          if proc["pid"] == os.getpid()]
    assert me and me[0]["name"]


def test_process_poll_and_terminate_are_real(tmp_path):
    provider = _provider(tmp_path)
    assert provider.process("", {"pid": os.getpid()})["running"] is True
    proc = subprocess.Popen(["sleep", "30"])
    try:
        assert provider.process("", {"pid": proc.pid})["running"] is True
        result = provider.process("", {"pid": proc.pid, "op": "terminate"})
        assert result["ok"] is True
        assert result["signalled"] == "SIGTERM"
        assert proc.wait(timeout=10) == -signal.SIGTERM
        assert provider.process(
            "", {"pid": proc.pid})["running"] is False
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_process_refuses_self_destruct(tmp_path):
    provider = _provider(tmp_path)
    assert provider.process("", {"pid": 1, "op": "kill"})["kind"] == "refused"
    assert provider.process(
        "", {"pid": os.getpid(), "op": "terminate"})["kind"] == "refused"
    assert provider.process("", {"pid": -5})["kind"] == "invalid"
    assert provider.process("", {"pid": 1, "op": "explode"})["kind"] == "invalid"


def test_files_are_confined_and_real(tmp_path):
    provider = _provider(tmp_path)
    assert provider.file_access("docs/n.txt", {
        "mode": "write", "content": "hello"})["ok"] is True
    read = provider.file_access("docs/n.txt", {"mode": "read"})
    assert read["content"] == "hello"
    assert (tmp_path / "desk" / "docs" / "n.txt").read_text() == "hello"
    assert "n.txt" in provider.file_access("docs", {"mode": "list"})["entries"]
    assert "docs/n.txt" in provider.file_select("", {})["files"]
    assert provider.file_access("docs/n.txt", {"mode": "delete"})["ok"] is True
    assert provider.file_access("docs/n.txt", {"mode": "delete"})["ok"] is False
    assert provider.file_access("../escape.txt", {
        "mode": "write", "content": "x"})["kind"] == "invalid"
    assert not (tmp_path / "escape.txt").exists()


def test_launch_runs_real_processes(tmp_path):
    provider = _provider(tmp_path)
    result = provider.launch(
        sys.executable, {"args": ["-c", "import sys; sys.exit(0)"]})
    assert result["ok"] is True
    assert result["executable"] == sys.executable
    assert isinstance(result["pid"], int) and result["pid"] > 0
    missing = provider.launch("forge-no-such-command-xyz", {})
    assert missing["ok"] is False and missing["kind"] == "not_found"
    assert provider.launch("", {})["kind"] == "invalid"
    assert provider.launch("true", {"args": "nope"})["kind"] == "invalid"


def test_snapshot_and_health(tmp_path):
    provider = _provider(tmp_path)
    assert provider.healthy() is True
    snapshot = provider.snapshot()
    assert snapshot["connected"] is True
    assert snapshot["simulation"] is False
    assert snapshot["provider"] == "local"
    assert snapshot["process_count"] and snapshot["process_count"] > 0


def test_provider_flags():
    assert LocalDesktopProvider.simulation is False
    assert LocalDesktopProvider.name == "local"
    assert FakeDesktopProvider.simulation is True
    assert FakeDesktopProvider.name == "fake"


# -- honest unsupported paths ------------------------------------------------------------


def test_gui_without_backends_refuses_honestly(tmp_path, monkeypatch):
    _no_gui(monkeypatch)
    provider = _provider(tmp_path)
    assert provider.screenshot()["kind"] == "unsupported"
    assert "screenshot" in provider.screenshot()["error"]
    assert provider.list_windows()["kind"] == "unsupported"
    assert provider.mouse_move("", {"x": 10, "y": 10})["kind"] == "unsupported"
    assert provider.mouse_click("", {"x": 1, "y": 1})["kind"] == "unsupported"
    assert provider.keyboard("", {"text": "hi"})["kind"] == "unsupported"
    assert provider.window("w", {"op": "focus"})["kind"] == "unsupported"
    assert provider.clipboard("", {})["kind"] == "unsupported"
    assert provider.app_action("w", {"action": "activate"})["kind"] \
        == "unsupported"
    assert provider.read_screen()["ocr_available"] is False


def test_validation_precedes_backend_detection(tmp_path, monkeypatch):
    _no_gui(monkeypatch)
    provider = _provider(tmp_path)
    assert provider.mouse_move("", {"x": "NaN"})["kind"] == "invalid"
    assert provider.mouse_click("", {"x": 1, "y": 1, "button": "nuke"})[
        "kind"] == "invalid"
    assert provider.keyboard("", {"text": "x" * 2001})["kind"] == "too_large"
    assert provider.keyboard("", {"text": "ok", "keys": ["a b"]})[
        "kind"] == "invalid"
    assert provider.window("", {})["kind"] == "invalid"
    assert provider.clipboard("", {"op": "explode"})["kind"] == "invalid"
    assert provider.app_action("w", {"action": "hack"})[
        "kind"] == "unsupported"


# -- fake backends prove the real plumbing ---------------------------------------------------


@pytest.fixture()
def gui_tools(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _exe(bindir / "xdotool", """case \"$1\" in
  getdisplaygeometry) echo \"1920 1080\";;
  getactivewindow) if [ \"$2\" = \"getwindowname\" ]; then echo \"Fake Active\"; else echo \"0xABC\"; fi;;
  search) echo \"0xABC\";;
  getwindowname) echo \"Fake Win\";;
  *) exit 0;;
esac""")
    png = tmp_path / "shot.png"
    png.write_bytes(_tiny_png(64, 32))
    _exe(bindir / "scrot", f"cp {png} \"$1\"")
    clip = tmp_path / "clipboard.txt"
    _exe(bindir / "xclip", """if [ \"$3\" = \"-o\" ]; then cat \"$CLIPFILE\" 2>/dev/null || true; else cat > \"$CLIPFILE\"; fi""".replace(
        "$CLIPFILE", str(clip)))
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    return bindir


@pytest.mark.skipif(os.environ.get("WAYLAND_DISPLAY"), reason="needs X11 env")
def test_mouse_and_keyboard_through_stub_backend(tmp_path, gui_tools):
    provider = _provider(tmp_path)
    assert provider.backends()["input"] is not None
    moved = provider.mouse_move("", {"x": 100, "y": 200})
    assert moved["ok"] is True and moved["x"] == 100
    assert provider.mouse_move("", {"x": 5000, "y": 5})[
        "kind"] == "out_of_bounds"
    clicked = provider.mouse_click("", {"x": 5, "y": 6, "double": True})
    assert clicked["ok"] is True and clicked["double"] is True
    typed = provider.keyboard("", {"text": "hi", "keys": ["Return"]})
    assert typed["ok"] is True and typed["typed_chars"] == 2


@pytest.mark.skipif(os.environ.get("WAYLAND_DISPLAY"), reason="needs X11 env")
def test_windows_and_read_screen_through_stub(tmp_path, gui_tools):
    provider = _provider(tmp_path)
    listing = provider.list_windows()
    assert listing["ok"] is True
    assert listing["windows"][0]["title"] == "Fake Win"
    assert listing["focused"] == "0xABC"
    assert provider.window("Fake", {"op": "focus"})["ok"] is True
    assert provider.app_action("Fake", {"action": "activate"})["ok"] is True
    screen = provider.read_screen()
    assert screen["active_window"] == "Fake Active"
    assert screen["ocr_available"] is False


def test_screenshot_parses_real_png(tmp_path, gui_tools, monkeypatch):
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    provider = _provider(tmp_path)
    shot = provider.screenshot()
    assert shot["ok"] is True
    assert shot["format"] == "png"
    assert (shot["width"], shot["height"]) == (64, 32)
    assert base64.b64decode(shot["image_b64"]) == _tiny_png(64, 32)


def test_clipboard_round_trip_through_stub(tmp_path, gui_tools, monkeypatch):
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    provider = _provider(tmp_path)
    assert provider.clipboard("", {"op": "write", "content": "abc"})[
        "ok"] is True
    assert provider.clipboard("", {"op": "read"})["content"] == "abc"


# -- bridge wiring ---------------------------------------------------------------------------


def _bridged(provider):
    agent = DesktopAgent(provider)
    return DesktopBridge(agent)


def test_bridge_simulate_only_on_simulation_providers(tmp_path):
    fake_bridge = _bridged(FakeDesktopProvider())
    session = fake_bridge.open_session("alice", "demo")
    assert fake_bridge.simulate(session.bridge_id, width=800,
                                height=600) is True

    local_bridge = _bridged(_provider(tmp_path))
    local_session = local_bridge.open_session("alice", "demo")
    with pytest.raises(DesktopBridgeError):
        local_bridge.simulate(local_session.bridge_id, width=800,
                              height=600)
    assert local_bridge.snapshot(local_session.bridge_id)["provider"][
        "simulation"] is False


def test_control_plane_accepts_real_provider(tmp_path):
    from forge.control.control_plane import ControlConfig, ControlPlane

    root = tmp_path / "proj"
    root.mkdir()
    plane = ControlPlane(ControlConfig(
        db_path=str(tmp_path / "cockpit.db"),
        projects={"demo": str(root)},
        desktop_provider=_provider(tmp_path)))
    try:
        assert "LocalDesktopProvider" in (
            plane.desktop_bridge.provider_kind())
    finally:
        plane.stop()
