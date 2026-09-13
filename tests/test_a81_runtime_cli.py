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


# -- forge runtime metrics ---------------------------------------------------


def test_runtime_metrics_reports_an_empty_window_honestly(capsys):
    assert run_cli(["forge", "runtime", "metrics", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["requests"] == 0
    # An empty window reports None, not a fabricated 0.0 that reads "instant".
    assert payload["success_rate"] is None
    assert payload["latency_ms"]["p50"] is None
    assert payload["error_kinds"] == {}


def test_runtime_metrics_text_output(capsys):
    assert run_cli(["forge", "runtime", "metrics"]) == 0
    out = capsys.readouterr().out
    assert "Runtime metrics" in out
    assert "success rate: n/a" in out
    assert "p50=None" in out


def test_runtime_metrics_can_be_scoped_to_a_backend(capsys):
    assert run_cli(["forge", "runtime", "metrics", "--backend", "native",
                    "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["backend"] == "native"
    assert payload["requests"] == 0


def test_runtime_metrics_never_contains_prompt_text(capsys):
    run_cli(["forge", "runtime", "metrics", "--json"])
    assert "prompt" not in capsys.readouterr().out


# -- forge runtime test ------------------------------------------------------


def test_runtime_self_test_passes_every_contract_check(capsys):
    """The self-check runs real runtime code paths and must pass them all."""
    assert run_cli(["forge", "runtime", "test"]) == 0
    out = capsys.readouterr().out

    assert "Runtime self-check" in out
    assert "FAIL" not in out
    # The summary must agree with the individual results, not overstate them.
    summary = next(line for line in out.splitlines()
                   if "contract checks passed" in line)
    passed, total = summary.strip().split()[0].split("/")
    assert passed == total
    assert int(total) >= 10


def test_runtime_self_test_is_honest_about_real_inference(capsys):
    """Passing contract checks must not be presented as working inference."""
    assert run_cli(["forge", "runtime", "test"]) == 0
    out = capsys.readouterr().out

    assert "Real inference backends" in out
    # On a default install no backend can serve a real model, and the command
    # says so instead of implying the loopback checks proved otherwise.
    assert "No backend can serve a real model right now" in out
    assert "loopback" in out


def test_runtime_self_test_json(capsys):
    assert run_cli(["forge", "runtime", "test", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["passed"] == payload["total"]
    assert payload["total"] >= 10
    assert all(check["ok"] for check in payload["checks"])
    # Every check names what it observed, so a failure is diagnosable.
    for check in payload["checks"]:
        assert check["name"]
        assert "detail" in check
    assert payload["ready_backends"] == []


def test_runtime_self_test_covers_the_core_guarantees(capsys):
    run_cli(["forge", "runtime", "test", "--json"])
    payload = json.loads(capsys.readouterr().out)
    names = " | ".join(check["name"] for check in payload["checks"])

    for expected in ("timeout", "cancel", "protocol", "redact", "stream",
                     "duplicate", "metrics", "in-flight"):
        assert expected in names, expected


def test_runtime_self_test_never_leaks_the_secret_it_plants(capsys):
    """The check plants a key-shaped string and must show it redacted."""
    run_cli(["forge", "runtime", "test"])
    out = capsys.readouterr().out
    assert "sk-abcdefgh1234567890" not in out


def test_runtime_self_test_does_not_touch_the_network(capsys):
    """Default config disables network; the self-check must not need it."""
    run_cli(["forge", "runtime", "backends", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert all(info["available"] is False for info in payload["backends"]
               if info["requires_network"])

    assert run_cli(["forge", "runtime", "test", "--json"]) == 0
    json.loads(capsys.readouterr().out)


def test_runtime_subcommands_are_all_listed(capsys):
    # ``run_cli`` swallows SystemExit, so assert on the rendered help.
    run_cli(["forge", "runtime", "--help"])
    out = capsys.readouterr().out
    for name in ("status", "models", "health", "backends", "metrics", "test",
                 "load", "unload"):
        assert name in out, name
