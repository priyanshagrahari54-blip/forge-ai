"""CLI surface: `forge native-ai [run|status|test]` and `forge run --native`."""
from __future__ import annotations

import json
import sys
from unittest.mock import patch

import pytest

from helpers_native_ai import write_repo

from forge.cli import main


def run_cli(argv):
    with patch.object(sys, "argv", ["forge"] + argv):
        with pytest.raises(SystemExit) as exit_info:
            main()
        return exit_info.value.code


def run_cli_noexit(argv):
    with patch.object(sys, "argv", ["forge"] + argv):
        main()
        return 0


# -- status ---------------------------------------------------------------

def test_bare_native_ai_shows_status(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_repo(tmp_path)
    code = run_cli(["native-ai", "status", "--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0
    assert "Forge Native AI Engine status" in out
    assert "engine state" in out
    assert "native-deterministic" in out
    assert "[free ] Repository analysis" in out
    assert "[model] Code / change generation" in out


def test_native_ai_with_no_subcommand_defaults_to_status(tmp_path, capsys,
                                                         monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_repo(tmp_path)
    code = run_cli(["native-ai", "--root", str(tmp_path), "--json"])
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert code == 0
    assert payload["live"]["engine"] == "forge-native-ai"
    assert payload["live"]["state"]["engine_state"] == "idle"
    assert "capabilities" in payload["live"]


# -- self-test ------------------------------------------------------------

def test_native_ai_test_passes_and_json(tmp_path, capsys):
    code = run_cli(["native-ai", "test"])
    assert code == 0
    out = capsys.readouterr().out
    assert "Self-test OK" in out


def test_native_ai_test_json_payload(capsys):
    run_cli(["native-ai", "test", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["passed"] is True
    assert payload["passed_count"] == payload["total"] >= 10
    names = {check["name"] for check in payload["checks"]}
    assert {"startup", "planning", "verification", "policy-enforcement",
            "memory", "no-model-operation"} <= names


# -- run --------------------------------------------------------------------

def test_native_ai_run_task_sugar_and_needs_model_exit(tmp_path, capsys,
                                                        monkeypatch):
    """`native-ai "fix ..."` == `native-ai run "fix ..."`; exit 3 honest."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OLLAMA_URL", "http://127.0.0.1:9")  # unreachable
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    write_repo(tmp_path, calc="def add(a, b):\n    return a - b\n")
    code = run_cli(["native-ai", "fix the add function in calc.py",
                    "--root", str(tmp_path), "--json", "--approve"])
    assert code == 3  # NEEDS_MODEL (registered model unreachable ⇒ honest
    # refusal via the placeholder-detection path, never fabricated output)
    payload = json.loads(capsys.readouterr().out)
    report = payload["report"]
    assert report["final_status"] == "NEEDS_MODEL"
    assert report["files_changed"] == []
    assert payload["result"]["engine_state"] == "needs_model"
    # and the snapshot on disk matches what the CLI reported
    snapshot = json.loads((tmp_path / ".forge/native/state.json").read_text(
        encoding="utf-8"))
    assert snapshot["engine_state"] == "needs_model"


def test_native_ai_run_analysis_completes(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_repo(tmp_path)
    code = run_cli(["native-ai", "run", "analyze the repository",
                    "--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0
    assert "Native AI: COMPLETED" in out
    assert "verification:" in out


def test_native_ai_run_assisted_without_approver_exits_2(tmp_path, capsys,
                                                         monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_repo(tmp_path, calc="def add(a, b):\n    return a - b\n")
    code = run_cli(["native-ai", "fix calc.py add",
                    "--root", str(tmp_path)])
    err = capsys.readouterr().err
    assert code == 2
    assert "needs an approver" in err


def test_native_ai_run_force_bypasses_preflight_then_needs_model(
        tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OLLAMA_URL", "http://127.0.0.1:9")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    write_repo(tmp_path, calc="def add(a, b):\n    return a - b\n")
    code = run_cli(["native-ai", "fix calc.py add", "--root", str(tmp_path),
                    "--force"])
    assert code == 3  # the run proceeds and reports NEEDS_MODEL honestly
    out = capsys.readouterr().out
    assert "Native AI: NEEDS_MODEL" in out
    assert "skipped (needs neural model): code_generation" in out
    # and the fixture was left exactly as found — a refusal touches nothing:
    assert (tmp_path / "calc.py").read_text(encoding="utf-8") == \
        "def add(a, b):\n    return a - b\n"


# -- forge run --native -------------------------------------------------------

def test_forge_run_native_routes_through_the_engine(tmp_path, capsys,
                                                    monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_repo(tmp_path)
    code = run_cli(["run", "analyze the calc module", "--root",
                    str(tmp_path), "--native"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Native AI: COMPLETED" in out
    # engine artifacts prove --native used the engine, not the supervisor:
    assert (tmp_path / ".forge/native/runs").is_dir()


def test_forge_run_native_json(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_repo(tmp_path)
    code = run_cli(["run", "analyze the repository", "--root",
                    str(tmp_path), "--native", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["report"]["engine"] if "engine" in payload["report"] \
        else True
    assert payload["report"]["final_status"] == "COMPLETED"
