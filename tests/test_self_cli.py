import sys
from pathlib import Path
from unittest.mock import patch

from forge.cli import main


def test_cli_self_analyze(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "forge").mkdir()
    (tmp_path / "forge" / "__init__.py").write_text("", encoding="utf-8")

    test_args = ["forge", "self-analyze"]
    with patch.object(sys, "argv", test_args):
        main()

    captured = capsys.readouterr()
    assert "Forge Self Analysis" in captured.out


def test_cli_self_status(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "forge").mkdir()
    (tmp_path / "forge" / "__init__.py").write_text("", encoding="utf-8")

    test_args = ["forge", "self-status"]
    with patch.object(sys, "argv", test_args):
        main()

    captured = capsys.readouterr()
    assert "Forge Self-Development Status" in captured.out


def test_cli_self_improve(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "forge").mkdir()
    (tmp_path / "forge" / "__init__.py").write_text("", encoding="utf-8")

    test_args = ["forge", "self-improve", "--iterations", "1"]
    with patch.object(sys, "argv", test_args):
        main()

    captured = capsys.readouterr()
    assert "Starting Forge Self-Improvement Loop" in captured.out
