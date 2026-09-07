import sys
from unittest.mock import patch

from forge.cli import main


def run_cli(argv):
    with patch.object(sys, "argv", argv):
        main()


def test_cli_models_lists_default_fabric(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    run_cli(["forge", "models"])
    captured = capsys.readouterr()
    assert "Models" in captured.out
    assert "local-fallback" in captured.out
    assert "ollama/" in captured.out


def test_cli_models_capability_filter(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    run_cli(["forge", "models", "--capability", "coding"])
    captured = capsys.readouterr()
    assert "local-fallback" in captured.out


def test_cli_models_capabilities_vocabulary(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    run_cli(["forge", "models", "--capabilities"])
    captured = capsys.readouterr()
    for capability in ("coding", "vision", "speech_to_text", "long_context"):
        assert capability in captured.out


def test_cli_models_json(tmp_path, monkeypatch, capsys):
    import json

    monkeypatch.chdir(tmp_path)
    run_cli(["forge", "models", "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert "models" in payload
    assert "capabilities" in payload
    assert any(model["name"] == "local-fallback" for model in payload["models"])


def test_cli_models_health(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    run_cli(["forge", "models", "health"])
    captured = capsys.readouterr()
    assert "Model Health" in captured.out
    assert "ollama/" in captured.out


def test_cli_models_providers(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    run_cli(["forge", "models", "providers"])
    captured = capsys.readouterr()
    assert "Providers" in captured.out
    assert "ollama" in captured.out


def test_cli_models_capabilities_subcommand(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    run_cli(["forge", "models", "capabilities"])
    captured = capsys.readouterr()
    assert "image_generation" in captured.out


def test_cli_models_test(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    run_cli(["forge", "models", "test"])
    captured = capsys.readouterr()
    assert "Model Fabric self-test" in captured.out
