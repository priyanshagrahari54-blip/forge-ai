"""Agent Creation Engine (A81): the `forge agents` CLI.

The CLI is the operator surface for the engine: create, list, test,
enable, disable, show, versions, templates, export. Commands must
refuse the same illegal operations the manager refuses (untested
enables, escalations) with a non-zero exit.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from forge.cli import main


def run_cli(argv):
    with patch.object(sys, "argv", argv):
        try:
            main()
            return 0
        except SystemExit as exc:
            return exc.code if exc.code is not None else 0


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_agents_list_empty(cli_env, capsys):
    assert run_cli(["forge", "agents"]) == 0
    assert "No agents created yet" in capsys.readouterr().out


def test_agents_templates(cli_env, capsys):
    assert run_cli(["forge", "agents", "templates"]) == 0
    out = capsys.readouterr().out
    for template in ("coding", "research", "security", "game-dev",
                     "os-dev", "documentation"):
        assert template in out


def test_agents_create_test_enable_list(cli_env, capsys):
    assert run_cli(["forge", "agents", "create", "coder-1",
                    "--template", "coding"]) == 0
    assert run_cli(["forge", "agents", "list"]) == 0
    out = capsys.readouterr().out
    assert "coder-1" in out and "validated" in out

    assert run_cli(["forge", "agents", "enable", "coder-1"]) == 1
    assert "tested" in capsys.readouterr().err

    assert run_cli(["forge", "agents", "test", "coder-1"]) == 0
    out = capsys.readouterr().out
    assert "tested" in out and "score=1.00" in out

    assert run_cli(["forge", "agents", "enable", "coder-1"]) == 0
    assert run_cli(["forge", "agents", "list"]) == 0
    assert "* coder-1" in capsys.readouterr().out


def test_agents_disable_and_retire(cli_env, capsys):
    run_cli(["forge", "agents", "create", "coder-1",
             "--template", "coding"])
    run_cli(["forge", "agents", "test", "coder-1"])
    run_cli(["forge", "agents", "enable", "coder-1"])
    assert run_cli(["forge", "agents", "disable", "coder-1"]) == 0
    assert "disabled" in capsys.readouterr().out
    assert run_cli(["forge", "agents", "retire", "coder-1"]) == 0
    assert "retired" in capsys.readouterr().out
    # Retired agents can never be re-enabled.
    assert run_cli(["forge", "agents", "enable", "coder-1"]) == 1


def test_agents_show_and_versions(cli_env, capsys):
    run_cli(["forge", "agents", "create", "coder-1",
             "--template", "coding"])
    assert run_cli(["forge", "agents", "show", "coder-1"]) == 0
    out = capsys.readouterr().out
    assert "purpose:" in out
    assert "permissions:" in out
    assert "limits:" in out
    assert run_cli(["forge", "agents", "versions", "coder-1"]) == 0
    assert "v1" in capsys.readouterr().out


def test_agents_create_from_spec_file(cli_env, capsys):
    from forge.agent_engine.templates import build_template_spec

    spec = build_template_spec("research", "researcher-1")
    spec_path = Path("spec.json")
    spec_path.write_text(json.dumps(spec.to_dict(), indent=2))
    assert run_cli(["forge", "agents", "create", "researcher-1",
                    "--spec", "spec.json"]) == 0
    out = capsys.readouterr().out
    assert "researcher-1" in out
    assert run_cli(["forge", "agents", "show", "researcher-1",
                    "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["agent"]["name"] == "researcher-1"
    assert "write_file" not in payload["agent"]["spec"]["tools"]


def test_agents_update_escalation_requires_confirmation(cli_env, capsys):
    from forge.agent_engine.templates import build_template_spec

    run_cli(["forge", "agents", "create", "coder-1",
             "--template", "coding"])
    escalated = build_template_spec("coding", "coder-1").to_dict()
    escalated["tools"] += ["git_push"]
    escalated["permissions"] += ["git_push"]
    Path("escalated.json").write_text(json.dumps(escalated))
    # Without confirmation the escalation must be refused.
    assert run_cli(["forge", "agents", "update", "coder-1",
                    "--spec", "escalated.json"]) == 1
    assert "self-grant" in capsys.readouterr().err
    # With explicit confirmation it succeeds and is audited.
    assert run_cli(["forge", "agents", "update", "coder-1",
                    "--spec", "escalated.json",
                    "--confirm-escalation"]) == 0
    assert "v2" in capsys.readouterr().out


def test_agents_export_package(cli_env, capsys):
    run_cli(["forge", "agents", "create", "coder-1",
             "--template", "coding"])
    capsys.readouterr()  # discard the create banner
    assert run_cli(["forge", "agents", "export", "coder-1"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload["files"]) == {"agent.json", "spec.json",
                                     "permissions.lock"}


def test_agents_unknown_subcommand(cli_env, capsys):
    assert run_cli(["forge", "agents", "frobnicate"]) == 2
    err = capsys.readouterr().err
    assert "invalid choice" in err and "frobnicate" in err


def test_agents_json_output(cli_env, capsys):
    run_cli(["forge", "agents", "create", "coder-1",
             "--template", "coding", "--json"])
    created = json.loads(capsys.readouterr().out)
    assert created["ok"] is True
    assert created["agent"]["name"] == "coder-1"
    run_cli(["forge", "agents", "list", "--json"])
    listed = json.loads(capsys.readouterr().out)
    assert [agent["name"] for agent in listed["agents"]] == ["coder-1"]


def test_agents_validate_and_show_run_coding_pipeline(cli_env, capsys):
    run_cli(["forge", "agents", "create", "coder-1",
             "--template", "coding"])
    assert run_cli(["forge", "agents", "validate", "coder-1"]) == 0
    capsys.readouterr()  # discard non-JSON banners
    assert run_cli(["forge", "agents", "test", "coder-1", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["benchmark"]["score"] == 1.0
    assert payload["current_lifecycle"] == "tested"
