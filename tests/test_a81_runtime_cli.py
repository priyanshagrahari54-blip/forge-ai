"""A81 — the ``forge runtime`` CLI surface.

Every command runs offline: the default configuration enables only the native
backend with network access disabled, so no test can reach out to a server.
"""
from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

import pytest

from forge.cli import main

RUNTIME_ENV = ("FORGE_RUNTIME_BACKEND", "FORGE_RUNTIME_BACKENDS",
               "FORGE_RUNTIME_ALLOW_NETWORK", "FORGE_RUNTIME_OLLAMA_URL",
               "FORGE_RUNTIME_OLLAMA_MODEL", "FORGE_RUNTIME_MODEL_DIRS",
               "FORGE_RUNTIME_TIMEOUT", "FORGE_RUNTIME_MAX_TIMEOUT",
               "OLLAMA_BASE_URL", "OLLAMA_URL", "OLLAMA_MODEL")


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """A clean cwd with no runtime config and no inherited env overrides."""
    for name in RUNTIME_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def write_model(root: Path, name: str = "tiny-q4.gguf",
                tensors: int = 9) -> Path:
    models = root / "models"
    models.mkdir(exist_ok=True)
    path = models / name
    path.write_bytes(b"GGUF" + struct.pack("<IQQ", 3, tensors, 4) + b"\0" * 32)
    return path


def run_cli(argv) -> int:
    """Run the CLI and return its exit code (``forge runtime`` always exits)."""
    from unittest.mock import patch

    with patch.object(sys, "argv", argv):
        try:
            main()
        except SystemExit as exc:
            if exc.code is None:
                return 0
            return exc.code if isinstance(exc.code, int) else 1
    return 0


# -- forge runtime / forge runtime status ------------------------------------


def test_runtime_default_is_status(capsys):
    assert run_cli(["forge", "runtime"]) == 0
    out = capsys.readouterr().out
    assert "Forge Native Model Runtime" in out
    assert "default backend: native" in out
    assert "network access: disabled" in out
    assert "backends:" in out
    assert "health:" in out
    assert "resources:" in out


def test_runtime_status_subcommand_and_json(capsys):
    assert run_cli(["forge", "runtime", "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["runtime"]["version"]
    assert payload["runtime"]["config"]["allow_network"] is False
    assert [info["name"] for info in payload["backends"]] == ["native"]
    assert payload["models"]["total"] == 0
    assert payload["resources"]["python_version"]


def test_runtime_status_honest_about_no_inference(capsys):
    assert run_cli(["forge", "runtime", "status"]) == 0
    out = capsys.readouterr().out
    assert "degraded" in out
    assert "inference adapter" in out.lower()


def test_runtime_status_probe_flag_is_accepted(capsys):
    assert run_cli(["forge", "runtime", "status", "--probe"]) == 0
    assert "Forge Native Model Runtime" in capsys.readouterr().out


# -- forge runtime models ----------------------------------------------------


def test_runtime_models_reports_no_models_without_configuration(capsys):
    assert run_cli(["forge", "runtime", "models"]) == 0
    out = capsys.readouterr().out
    assert "Runtime models" in out
    assert "none discovered" in out


def test_runtime_models_discovers_from_an_explicit_directory(tmp_path, capsys):
    write_model(tmp_path, tensors=21)
    run_cli(["forge", "runtime", "models", "--model-dir",
             str(tmp_path / "models")])
    out = capsys.readouterr().out
    assert "native:tiny-q4.gguf" in out
    assert "format=gguf" in out
    assert "'tensor_count': 21" in out


def test_runtime_models_json(tmp_path, capsys):
    write_model(tmp_path)
    run_cli(["forge", "runtime", "models", "--json", "--model-dir",
             str(tmp_path / "models")])
    payload = json.loads(capsys.readouterr().out)
    ids = [model["model_id"] for model in payload["models"]]
    assert ids == ["native:tiny-q4.gguf"]
    assert payload["models"][0]["metadata"]["header_ok"] is True
    assert payload["discovery"]["native"]["refreshed"] is False


def test_runtime_models_no_discover_flag(tmp_path, capsys):
    write_model(tmp_path)
    run_cli(["forge", "runtime", "models", "--no-discover", "--model-dir",
             str(tmp_path / "models")])
    out = capsys.readouterr().out
    assert "none discovered" in out


def test_runtime_models_accepts_flags_before_the_subcommand(tmp_path, capsys):
    write_model(tmp_path)
    run_cli(["forge", "runtime", "--model-dir", str(tmp_path / "models"),
             "models"])
    assert "native:tiny-q4.gguf" in capsys.readouterr().out


# -- forge runtime health ----------------------------------------------------


def test_runtime_health_reports_degraded_and_exits_nonzero(capsys):
    assert run_cli(["forge", "runtime", "health"]) == 1
    out = capsys.readouterr().out
    assert "Runtime health" in out
    assert "native: degraded" in out
    assert "NOT READY" in out


def test_runtime_health_json(capsys):
    assert run_cli(["forge", "runtime", "health", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["health"][0]["backend"] == "native"
    assert payload["health"][0]["status"] == "degraded"


def test_runtime_health_offline_skips_the_probe(capsys):
    assert run_cli(["forge", "runtime", "health", "--offline", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["health"][0]["probed"] is False


# -- forge runtime backends --------------------------------------------------


def test_runtime_backends_lists_what_is_registered(capsys):
    assert run_cli(["forge", "runtime", "backends"]) == 0
    out = capsys.readouterr().out
    assert "Runtime backends" in out
    assert "native: kind=native" in out


def test_runtime_backends_can_be_selected_explicitly(capsys):
    run_cli(["forge", "runtime", "backends", "--backend", "ollama", "--json"])
    payload = json.loads(capsys.readouterr().out)
    names = [info["name"] for info in payload["backends"]]
    assert "native" in names and "ollama" in names
    ollama = next(info for info in payload["backends"]
                  if info["name"] == "ollama")
    assert ollama["requires_network"] is True
    assert ollama["available"] is False  # network stays disabled


def test_runtime_rejects_an_unknown_backend(capsys):
    code = run_cli(["forge", "runtime", "backends", "--backend",
                    "my.evil.Backend"])
    assert code == 2
    assert "Unknown backend" in capsys.readouterr().err


# -- load / unload -----------------------------------------------------------


def test_runtime_load_and_unload(tmp_path, capsys):
    write_model(tmp_path)
    model_dir = str(tmp_path / "models")

    # A fresh CLI process has an empty registry, so load runs discovery first
    # and then admits the metadata-verified artifact.
    assert run_cli(["forge", "runtime", "load", "tiny-q4.gguf", "--model-dir",
                    model_dir]) == 0
    assert "Loaded native:tiny-q4.gguf" in capsys.readouterr().out

    # Unloading in a *new* process has nothing resident, and says so rather
    # than claiming a release that did not happen.
    assert run_cli(["forge", "runtime", "unload", "tiny-q4.gguf",
                    "--model-dir", model_dir]) == 0
    assert "released=False" in capsys.readouterr().out


def test_runtime_load_reports_an_unknown_model(tmp_path, capsys):
    write_model(tmp_path)
    code = run_cli(["forge", "runtime", "load", "absent.gguf", "--model-dir",
                    str(tmp_path / "models")])
    assert code == 1
    assert "Load failed" in capsys.readouterr().err


def test_runtime_load_json_failure(tmp_path, capsys):
    assert run_cli(["forge", "runtime", "load", "absent.gguf", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["loaded"] is False
    assert "absent.gguf" in payload["error"]


# -- configuration -----------------------------------------------------------


def test_runtime_reads_a_config_file(tmp_path, capsys):
    (tmp_path / ".forge").mkdir()
    (tmp_path / ".forge" / "runtime.json").write_text(json.dumps({
        "default_backend": "native",
        "backends": ["native"],
        "model_dirs": [str(tmp_path / "models")],
        "timeout_seconds": 7,
    }), encoding="utf-8")
    write_model(tmp_path)

    assert run_cli(["forge", "runtime", "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["runtime"]["config"]["timeout_seconds"] == 7.0
    assert payload["runtime"]["config"]["model_dirs"] == [
        str(tmp_path / "models")]

    assert run_cli(["forge", "runtime", "models"]) == 0
    assert "native:tiny-q4.gguf" in capsys.readouterr().out


def test_runtime_never_enables_network_without_the_flag(capsys):
    run_cli(["forge", "runtime", "backends", "--backend", "ollama", "--json"])
    payload = json.loads(capsys.readouterr().out)
    ollama = next(info for info in payload["backends"]
                  if info["name"] == "ollama")
    assert ollama["available"] is False

    run_cli(["forge", "runtime", "backends", "--backend", "ollama",
             "--allow-network", "--json"])
    payload = json.loads(capsys.readouterr().out)
    ollama = next(info for info in payload["backends"]
                  if info["name"] == "ollama")
    # Available now means "the endpoint is configured and permitted", not
    # "it answered": nothing is probed by `backends`.
    assert ollama["available"] is True
    assert "not probed" in ollama["detail"]


def test_runtime_config_error_exits_two(tmp_path, capsys):
    (tmp_path / ".forge").mkdir()
    (tmp_path / ".forge" / "runtime.json").write_text(json.dumps({
        "default_backend": "ghost",
        "backends": ["native"],
    }), encoding="utf-8")
    assert run_cli(["forge", "runtime", "status"]) == 2
    assert "Runtime configuration error" in capsys.readouterr().err
