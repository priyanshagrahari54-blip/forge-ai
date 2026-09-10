"""Procedural Blender scenes: validation, script generation, headless runs."""
from __future__ import annotations

import json
import stat
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from forge.cli import main  # noqa: E402
from forge.media.blender import (  # noqa: E402
    BlenderRunner,
    BlenderSpecError,
    build_scene,
    example_scene,
    find_blender,
    render_script,
)
from forge.tools.blender import BlenderTool  # noqa: E402


def run_cli(argv):
    with patch.object(sys, "argv", argv):
        main()


def _spec(**overrides):
    data = example_scene()
    data.update(overrides)
    return data


# -- spec validation ------------------------------------------------------------


def test_example_scene_validates_and_round_trips():
    scene = build_scene(example_scene())
    assert scene.name == "forge_example"
    assert scene.animated is False
    clone = build_scene(scene.to_dict())
    assert clone.to_dict() == scene.to_dict()


def test_build_scene_rejects_garbage():
    with pytest.raises(BlenderSpecError):
        build_scene([])
    with pytest.raises(BlenderSpecError):
        build_scene(_spec(meshes="nope"))
    with pytest.raises(BlenderSpecError):
        build_scene(_spec(name="has spaces!"))
    with pytest.raises(BlenderSpecError):
        build_scene({"name": "empty"})
    with pytest.raises(BlenderSpecError):
        build_scene(_spec(meshes=[{"kind": "cube", "name": "A"},
                                  {"kind": "cube", "name": "A"}],
                           texts=[], lights=[]))
    with pytest.raises(BlenderSpecError):
        build_scene(_spec(meshes=[{"kind": "dragon", "name": "A"}],
                           texts=[], lights=[]))
    with pytest.raises(BlenderSpecError):
        build_scene(_spec(resolution=[8, 8]))
    with pytest.raises(BlenderSpecError):
        build_scene(_spec(frame_start=1, frame_end=700))
    with pytest.raises(BlenderSpecError):
        build_scene(_spec(engine="unreal"))
    with pytest.raises(BlenderSpecError):
        build_scene(_spec(samples=99999))
    with pytest.raises(BlenderSpecError):
        build_scene(_spec(camera={"track": "Ghost"}))
    with pytest.raises(BlenderSpecError):
        build_scene(_spec(texts=[{"name": "T", "body": "x" * 501}],
                           meshes=[], lights=[]))
    with pytest.raises(BlenderSpecError):
        build_scene(_spec(meshes=[{"kind": "cube", "name": "A",
                                   "scale": [1.0, 0.0, 1.0]}],
                           texts=[], lights=[]))
    with pytest.raises(BlenderSpecError):
        build_scene(_spec(lights=[{"name": "L", "type": "laser"}],
                           meshes=[], texts=[]))
    with pytest.raises(BlenderSpecError):
        build_scene(_spec(output="../escape"))


def test_animated_scene_requires_frame_range():
    scene = build_scene(_spec(frame_start=1, frame_end=48))
    assert scene.animated is True


# -- script generation ------------------------------------------------------------


def test_render_script_is_static_and_compiles():
    scene = build_scene(example_scene())
    script = render_script(scene)
    # Static program: no scene content interpolated (sidecar carries data).
    assert "Hero" not in script
    assert "forge_example" not in script
    assert "Editorial" not in script
    compile(script, "render.py", "exec")
    assert "BLENDER_EEVEE_NEXT" in script  # 3.x/4.x probing
    assert "FORGE_RENDER_DONE" in script


# -- binary discovery ------------------------------------------------------------


def test_find_blender_missing_reports_none(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv("BLENDER_BIN", raising=False)
    assert find_blender() is None


def test_find_blender_prefers_explicit_executable(tmp_path, monkeypatch):
    binary = tmp_path / "blender"
    binary.write_text("#!/bin/sh\necho fake\n")
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    assert find_blender(str(binary)) == str(binary)
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.delenv("BLENDER_BIN", raising=False)
    assert find_blender(str(tmp_path / "nope")) is None


def test_runner_check_unavailable_has_guidance(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv("BLENDER_BIN", raising=False)
    report = BlenderRunner().check()
    assert report["available"] is False
    assert "blender.org" in report["hint"]


def _fake_blender(path: Path, body: str) -> str:
    binary = path / "blender"
    binary.write_text("#!/bin/sh\n" + body + "\n")
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    return str(binary)


def test_runner_check_available_reports_version(tmp_path):
    binary = _fake_blender(tmp_path, 'echo "Blender 4.2.0 (hash deadbeef)"')
    report = BlenderRunner(binary).check()
    assert report["available"] is True
    assert "Blender 4.2.0" in report["version"]


# -- rendering -----------------------------------------------------------------------


def test_render_without_blender_validates_and_writes_workfiles(
        tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    (tmp_path / "empty").mkdir()
    monkeypatch.delenv("BLENDER_BIN", raising=False)
    scene = build_scene(example_scene())
    result = BlenderRunner().render(scene, tmp_path / "out")
    assert result.available is False
    assert result.success is False
    assert "not installed" in result.error
    work = tmp_path / "out" / ".forge-blender"
    sidecar = json.loads((work / "forge_example.json").read_text())
    assert sidecar["name"] == "forge_example"
    compile((work / "forge_example.py").read_text(), "render.py", "exec")


def test_render_success_with_stub_binary(tmp_path):
    binary = _fake_blender(
        tmp_path,
        'if [ "$1" = "--version" ]; then echo "Blender 4.2.0 (stub)"; exit 0; '
        'fi; out=""; prev=""; for arg in "$@"; do '
        'if [ "$prev" = "--" ]; then spec="$arg"; elif [ -n "$prev" ] && '
        '[ "$arg" != "--" ]; then out="$arg"; fi; '
        'prev="$arg"; done; '
        'if [ -z "$out" ]; then echo "no output base" >&2; exit 2; fi; '
        'echo "FORGE_RENDER_DONE $out"; touch "$out.png"')
    scene = build_scene(example_scene())
    result = BlenderRunner(binary).render(scene, tmp_path / "out",
                                          timeout=30.0)
    assert result.available is True
    assert result.success is True
    assert result.output_files == (str(tmp_path / "out" / "forge_example.png"),)
    assert "FORGE_RENDER_DONE" in result.stdout_tail


def test_render_failure_reports_exit_code(tmp_path):
    binary = _fake_blender(tmp_path, 'echo "boom" >&2; exit 3')
    scene = build_scene(example_scene())
    result = BlenderRunner(binary).render(scene, tmp_path / "out",
                                          timeout=30.0)
    assert result.success is False
    assert "code 3" in result.error
    assert "boom" in result.stdout_tail


def test_render_timeout_kills_stub(tmp_path):
    binary = _fake_blender(tmp_path, "sleep 30")
    scene = build_scene(example_scene())
    result = BlenderRunner(binary).render(scene, tmp_path / "out",
                                          timeout=0.3)
    assert result.success is False
    assert "exceeded" in result.error


def test_render_rejects_bad_timeout(tmp_path):
    scene = build_scene(example_scene())
    with pytest.raises(BlenderSpecError):
        BlenderRunner().render(scene, tmp_path / "out", timeout=0)


# -- tool wrapper ----------------------------------------------------------------------


def test_tool_validate_and_render_file(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv("BLENDER_BIN", raising=False)
    tool = BlenderTool()
    assert tool.validate(example_scene())["valid"] is True
    assert tool.validate({"name": "empty"})["valid"] is False
    spec_path = tmp_path / "scene.json"
    spec_path.write_text(json.dumps(example_scene()))
    report = tool.render_file(str(spec_path), str(tmp_path / "out"))
    assert report["success"] is False  # no Blender here, but validated
    assert report["available"] is False
    assert tool.render_file(str(tmp_path / "missing.json"))["success"] is False
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert "not JSON" in tool.render_file(str(bad))["error"]
    arr = tmp_path / "arr.json"
    arr.write_text("[]")
    assert "JSON object" in tool.render_file(str(arr))["error"]


def test_tool_render_rejects_invalid_spec():
    report = BlenderTool().render({"name": "empty"})
    assert report["success"] is False
    assert "Invalid scene" in report["error"]


# -- CLI ---------------------------------------------------------------------------------


def test_cli_blender_check_json(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv("BLENDER_BIN", raising=False)
    with pytest.raises(SystemExit) as exc:
        run_cli(["forge", "blender", "check", "--json"])
    assert exc.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["available"] is False


def test_cli_blender_example_and_render(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv("BLENDER_BIN", raising=False)
    with pytest.raises(SystemExit) as ok:
        run_cli(["forge", "blender", "example", "--out", "scene.json"])
    assert ok.value.code == 0
    assert json.loads((tmp_path / "scene.json").read_text())["name"]
    with pytest.raises(SystemExit) as exc:
        run_cli(["forge", "blender", "render", "scene.json",
                 "--out", "renders"])
    assert exc.value.code == 1  # honest: no Blender installed
    assert "Render failed" in capsys.readouterr().out


def test_cli_blender_check_available(tmp_path, monkeypatch, capsys):
    binary = _fake_blender(tmp_path, 'echo "Blender 3.6.9"')
    monkeypatch.setenv("BLENDER_BIN", binary)
    with pytest.raises(SystemExit) as exc:
        run_cli(["forge", "blender", "check"])
    assert exc.value.code == 0
    assert "Blender 3.6.9" in capsys.readouterr().out
