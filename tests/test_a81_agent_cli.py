"""A81: the `forge agents` CLI and the desktop Agent Manager."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from forge.agent_engine import cli as agents_cli
from forge.desktop_app.agent_manager import AgentManager, AgentManagerError


def invoke(store, *argv, as_json=True):
    parser = argparse.ArgumentParser(prog="forge")
    subparsers = parser.add_subparsers(dest="command")
    agents_cli.add_parser(subparsers)
    args = parser.parse_args(["agents", "--store", str(store)]
                             + (["--json"] if as_json else [])
                             + list(argv))
    return agents_cli.run(args)


@pytest.fixture()
def store(tmp_path):
    return tmp_path / "agents.json"


def test_list_is_empty_before_anything_is_created(store, capsys):
    assert invoke(store, "list") == 0
    assert json.loads(capsys.readouterr().out) == []


def test_templates_are_listed(store, capsys):
    assert invoke(store, "templates") == 0
    payload = json.loads(capsys.readouterr().out)
    assert {row["template"] for row in payload} == {
        "coding", "research", "security", "gamedev", "osdev",
        "documentation"}


def test_create_test_enable_disable_flow(store, capsys):
    assert invoke(store, "create", "--template", "documentation") == 0
    created = json.loads(capsys.readouterr().out)
    name = created["agent"]
    assert created["validation"]["valid"]
    assert store.exists()

    assert invoke(store, "list") == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["name"] == name
    assert rows[0]["state"] == "validated"

    assert invoke(store, "test", name, "--no-model") == 0
    report = json.loads(capsys.readouterr().out)
    assert report["passed"]

    assert invoke(store, "enable", name) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "enabled"

    assert invoke(store, "disable", name) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "disabled"


def test_state_survives_across_cli_invocations(store, capsys):
    invoke(store, "create", "--template", "security")
    capsys.readouterr()
    invoke(store, "test", "security-agent", "--no-model")
    capsys.readouterr()
    invoke(store, "enable", "security-agent")
    capsys.readouterr()

    assert invoke(store, "show", "security-agent") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["manifest"]["state"] == "enabled"
    assert payload["runnable"] is True


def test_enabling_an_untested_agent_is_refused(store, capsys):
    invoke(store, "create", "--template", "research")
    capsys.readouterr()
    assert invoke(store, "enable", "research-agent") == 1
    assert "error" in json.loads(capsys.readouterr().out)


def test_unknown_agent_is_refused_not_crashed(store, capsys):
    assert invoke(store, "show", "ghost-agent") == 1
    assert "Unknown agent" in json.loads(capsys.readouterr().out)["error"]


def test_create_without_a_template_or_spec_is_refused(store, capsys):
    assert invoke(store, "create", as_json=False) == 2
    assert "template" in capsys.readouterr().out


def test_create_from_a_spec_file(tmp_path, store, capsys):
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps({
        "name": "spec-agent",
        "purpose": "Created straight from a JSON specification.",
        "capabilities": ["research"],
        "tools": ["read_file"],
        "model_requirements": {"capability": "research"},
    }), encoding="utf-8")
    assert invoke(store, "create", "--spec", str(spec_path)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["agent"] == "spec-agent"
    assert payload["validation"]["valid"]


def test_a_spec_file_cannot_smuggle_escalation(tmp_path, store, capsys):
    spec_path = tmp_path / "bad.json"
    spec_path.write_text(json.dumps({
        "name": "sneaky-agent",
        "purpose": "Try to grant itself power.",
        "capabilities": ["coding"],
        "tools": ["grant_permission"],
    }), encoding="utf-8")
    assert invoke(store, "create", "--spec", str(spec_path)) == 1
    assert "error" in json.loads(capsys.readouterr().out)
    assert invoke(store, "list") == 0
    assert json.loads(capsys.readouterr().out) == []


def test_retire_is_terminal_through_the_cli(store, capsys):
    invoke(store, "create", "--template", "documentation")
    capsys.readouterr()
    assert invoke(store, "retire", "documentation-agent") == 0
    capsys.readouterr()
    assert invoke(store, "enable", "documentation-agent") == 1


def test_human_readable_output(store, capsys):
    assert invoke(store, "create", "--template", "coding",
                  as_json=False) == 0
    out = capsys.readouterr().out
    assert "Created coding-agent v1.0.0" in out
    assert invoke(store, "list", as_json=False) == 0
    assert "coding-agent" in capsys.readouterr().out


# -- desktop Agent Manager ----------------------------------------------

def test_agent_manager_mirrors_the_cli_store(store):
    manager = AgentManager(str(store))
    assert manager.list_agents() == []
    created = manager.create("gamedev")
    assert created["agent"] == "game-development-agent"
    assert manager.test(created["agent"])["passed"]
    assert manager.enable(created["agent"])["state"] == "enabled"

    # A fresh CLI invocation sees the same enabled agent.
    engine = agents_cli.load_engine(str(store))
    assert engine.lifecycle.state("game-development-agent") == "enabled"


def test_agent_manager_summary_lists_the_envelope(store):
    manager = AgentManager(str(store))
    manager.create("research")
    summary = manager.summary("research-agent")
    assert "research-agent" in summary
    assert "Permissions" in summary
    assert "approval required for writes: True" in summary
    assert "Gates:" in summary


def test_agent_manager_reports_refusals_as_errors(store):
    manager = AgentManager(str(store))
    manager.create("documentation")
    with pytest.raises(AgentManagerError):
        manager.enable("documentation-agent")
    with pytest.raises(AgentManagerError):
        manager.get_agent("ghost")
    with pytest.raises(AgentManagerError):
        manager.create("no-such-template")


def test_agent_manager_exposes_no_permission_grant(store):
    manager = AgentManager(str(store))
    for forbidden in ("grant", "grant_permission", "escalate",
                      "set_permissions"):
        assert not hasattr(manager, forbidden)


def test_agent_manager_reload_picks_up_external_changes(store):
    first = AgentManager(str(store))
    first.create("security")
    second = AgentManager(str(store))
    assert [row["name"] for row in second.list_agents()] == [
        "security-agent"]
    first.create("documentation")
    names = [row["name"] for row in second.reload()]
    assert names == ["documentation-agent", "security-agent"]
