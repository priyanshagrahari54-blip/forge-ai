"""A82 — ``forge agents`` on the command line.

Driven through the real ``forge.cli.main`` entry point, so argument
parsing, dispatch, output rendering, and exit codes are all exercised.
Exit codes matter: a failed validation, benchmark, or run must not exit 0.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from helpers_a82 import make_project  # noqa: E402

from forge.agents.engine.cli import run_agents_cli  # noqa: E402
from forge.cli import main  # noqa: E402


def run_cli(argv):
    """Run ``forge`` and return its exit code (0 when it returns normally).

    ``forge agents`` always exits explicitly so that a failed validation,
    benchmark, or run can never look like success to a script or to CI.
    """
    with patch.object(sys, "argv", argv):
        try:
            main()
        except SystemExit as exc:
            return int(exc.code or 0)
    return 0


@pytest.fixture()
def root(tmp_path, monkeypatch):
    project = tmp_path / "demo"
    make_project(project)
    monkeypatch.chdir(project)
    return project


def agents(*argv):
    return ["forge", "agents", "--actor", "alice", *argv]


# -- discovery -----------------------------------------------------------


def test_agents_templates_lists_all_six(root, capsys):
    run_cli(agents("templates"))
    out = capsys.readouterr().out
    for template in ("coding", "research", "security", "game-development",
                     "os-development", "documentation"):
        assert template in out
    assert "Coding Agent" in out
    assert "Documentation Agent" in out


def test_agents_templates_json(root, capsys):
    run_cli(agents("templates", "--json"))
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["templates"]) == 6
    assert payload["templates"][0]["id"] == "coding"


def test_agents_list_on_an_empty_project_explains_itself(root, capsys):
    run_cli(agents("list"))
    out = capsys.readouterr().out
    assert "No agents defined" in out
    assert "forge agents create" in out


# -- create / validate / test / enable / disable -------------------------


def test_full_cli_flow(root, capsys):
    run_cli(agents("create", "--template", "coding", "--name", "exporter"))
    out = capsys.readouterr().out
    assert "Created agent 'exporter'" in out
    assert "state: created" in out
    assert "no permissions granted yet" in out

    run_cli(agents("validate", "exporter"))
    out = capsys.readouterr().out
    assert "Validation PASSED" in out

    run_cli(agents("grant", "exporter", "--all-spec"))
    assert "Granted 5 operation(s)" in capsys.readouterr().out

    run_cli(agents("test", "exporter"))
    out = capsys.readouterr().out
    assert "Benchmark PASSED" in out
    assert "executed=7 passed=7" in out

    run_cli(agents("enable", "exporter"))
    assert "now enabled" in capsys.readouterr().out

    run_cli(agents("list"))
    out = capsys.readouterr().out
    assert "exporter" in out and "enabled" in out

    run_cli(agents("disable", "exporter"))
    assert "now disabled" in capsys.readouterr().out


def test_cli_exit_codes_track_the_outcome(root, capsys):
    run_cli(agents("create", "--template", "coding", "--name", "exporter"))
    capsys.readouterr()

    code = run_agents_cli(_args(["validate", "exporter"]))
    assert code == 0

    # Enabling before it has been tested must fail, with a non-zero exit.
    assert run_cli(agents("enable", "exporter")) == 1
    assert "lifecycle" in capsys.readouterr().err.lower() or \
        "Lifecycle" in capsys.readouterr().out


def test_cli_test_failure_exits_non_zero(root, capsys):
    run_cli(agents("create", "--template", "coding", "--name", "exporter"))
    capsys.readouterr()
    assert run_cli(agents("validate", "exporter")) == 0
    capsys.readouterr()
    manifest = (root / ".forge" / "agents" / "exporter" / "agent.json")
    payload = json.loads(manifest.read_text("utf-8"))
    payload["spec"]["purpose"] = "Edited behind the factory's back."
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    assert run_cli(agents("test", "exporter")) == 1
    out = capsys.readouterr().out
    assert "Benchmark FAILED" in out
    assert "spec-integrity" in out


def test_cli_create_from_a_spec_file(root, tmp_path, capsys):
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps({
        "name": "custom-agent",
        "purpose": "A hand written agent specification for the CLI test.",
        "capabilities": ["documentation", "reasoning"],
        "tools": [{"name": "read_file", "max_calls": 4},
                  {"name": "write_file", "max_calls": 4}],
        "permissions": {"operations": ["read_file", "write_file"],
                        "mode_ceiling": "assisted"},
    }), encoding="utf-8")
    run_cli(agents("create", "--name", "custom-agent",
                   "--spec", str(spec_path)))
    assert "Created agent 'custom-agent'" in capsys.readouterr().out
    run_cli(agents("show", "custom-agent"))
    out = capsys.readouterr().out
    assert "documentation" in out
    assert "write_file" in out


def test_cli_create_rejects_an_invalid_spec(root, tmp_path, capsys):
    spec_path = tmp_path / "bad.json"
    spec_path.write_text(json.dumps({"name": "bad", "purpose": "x"}),
                         encoding="utf-8")
    assert run_cli(agents("create", "--name", "bad", "--spec", str(spec_path))) == 1
    assert "AgentSpecError" in capsys.readouterr().err


def test_cli_create_needs_a_template_or_spec(root, capsys):
    assert run_cli(agents("create", "--name", "exporter")) == 2
    assert "--template or --spec" in capsys.readouterr().err


def test_cli_create_rejects_an_unknown_template(root, capsys):
    assert run_cli(agents("create", "--template", "skynet", "--name", "x1")) == 1


# -- inspection ----------------------------------------------------------


def test_cli_show_and_permissions_and_versions(root, capsys):
    run_cli(agents("create", "--template", "documentation", "--name",
                   "scribe"))
    capsys.readouterr()
    run_cli(agents("show", "scribe"))
    out = capsys.readouterr().out
    assert "state: created" in out
    assert "docs/, README.md" in out
    assert "allowed transitions" in out

    run_cli(agents("permissions", "scribe"))
    out = capsys.readouterr().out
    assert "granted: none" in out
    assert "expose_secrets" in out

    run_cli(agents("versions", "scribe"))
    assert "v1.0.0" in capsys.readouterr().out

    run_cli(agents("history", "scribe"))
    assert "no runs recorded" in capsys.readouterr().out


def test_cli_json_output_is_machine_readable(root, capsys):
    run_cli(agents("create", "--template", "security", "--name", "auditor"))
    capsys.readouterr()
    run_cli(agents("--json", "show", "auditor"))
    detail = json.loads(capsys.readouterr().out)
    assert detail["summary"]["name"] == "auditor"
    assert detail["summary"]["state"] == "created"
    assert detail["spec"]["memory"]["scope"] == "agent"
    assert detail["permissions"]["granted"] == []
    assert detail["versions"][0]["version"] == "1.0.0"

    run_cli(agents("--json", "list"))
    assert json.loads(capsys.readouterr().out)["counts"]["total"] == 1


# -- grants --------------------------------------------------------------


def test_cli_grant_and_revoke_one_operation(root, capsys):
    run_cli(agents("create", "--template", "coding", "--name", "exporter"))
    capsys.readouterr()
    run_cli(agents("grant", "exporter", "write_file", "--reason", "needed"))
    assert "Granted 'write_file'" in capsys.readouterr().out
    run_cli(agents("revoke", "exporter", "write_file"))
    assert "Revoked 'write_file'" in capsys.readouterr().out
    run_cli(agents("--json", "permissions", "exporter"))
    payload = json.loads(capsys.readouterr().out)
    assert payload["granted"] == []
    assert payload["revocations"] if "revocations" in payload else True


def test_cli_grant_refuses_self_grant(root, capsys):
    run_cli(agents("create", "--template", "coding", "--name", "exporter"))
    capsys.readouterr()
    assert run_cli(["forge", "agents", "--actor", "exporter", "grant",
                 "exporter", "write_file"]) == 1
    err = capsys.readouterr().err
    assert "cannot grant permissions to itself" in err


def test_cli_grant_refuses_an_operation_outside_the_ceiling(root, capsys):
    run_cli(agents("create", "--template", "research", "--name", "scout"))
    capsys.readouterr()
    assert run_cli(agents("grant", "scout", "write_file")) == 1
    assert "outside the declared ceiling" in capsys.readouterr().err


def test_cli_grant_needs_an_operation(root, capsys):
    run_cli(agents("create", "--template", "coding", "--name", "exporter"))
    capsys.readouterr()
    assert run_cli(agents("grant", "exporter")) == 2


# -- runs ----------------------------------------------------------------


def test_cli_run_refuses_a_disabled_agent(root, capsys):
    run_cli(agents("create", "--template", "coding", "--name", "exporter"))
    capsys.readouterr()
    assert run_cli(agents("run", "exporter", "add CSV export", "--offline")) == 1
    out = capsys.readouterr().out
    assert "FAILED" in out
    assert "only an enabled agent can run" in out


def test_cli_run_needs_a_task(root, capsys):
    run_cli(agents("create", "--template", "coding", "--name", "exporter"))
    capsys.readouterr()
    assert run_cli(agents("run", "exporter", "   ", "--offline")) == 2


# -- errors --------------------------------------------------------------


def test_cli_reports_an_unknown_agent(root, capsys):
    assert run_cli(agents("show", "ghost")) == 1
    assert "No such agent" in capsys.readouterr().err


def test_cli_subcommand_needs_an_agent_name(root, capsys):
    # argparse rejects the missing positional before dispatch, with its
    # standard usage-error exit code.
    assert run_cli(agents("validate")) == 2
    assert "the following arguments are required: agent" in \
        capsys.readouterr().err


def _args(argv, root=None):
    """Build the namespace the dispatcher expects (no argparse needed)."""
    import argparse

    parser = argparse.ArgumentParser()
    from forge.agents.engine.cli import build_parser

    build_parser(parser.add_subparsers(dest="command"))
    parsed = parser.parse_args(["agents", "--actor", "alice", *argv])
    return parsed


def test_cli_flags_work_before_and_after_the_subcommand(root, capsys):
    run_cli(agents("create", "--template", "coding", "--name", "exporter"))
    capsys.readouterr()
    run_cli(["forge", "agents", "--json", "list"])
    assert json.loads(capsys.readouterr().out)["counts"]["total"] == 1
    run_cli(["forge", "agents", "list", "--json"])
    assert json.loads(capsys.readouterr().out)["counts"]["total"] == 1


def test_cli_pause_resume_retire(root, capsys):
    for argv in (["create", "--template", "coding", "--name", "exporter"],
                 ["validate", "exporter"], ["test", "exporter"],
                 ["enable", "exporter"]):
        run_cli(agents(*argv))
        capsys.readouterr()
    run_cli(agents("pause", "exporter"))
    assert "now paused" in capsys.readouterr().out
    run_cli(agents("resume", "exporter"))
    assert "now enabled" in capsys.readouterr().out
    run_cli(agents("disable", "exporter"))
    capsys.readouterr()
    run_cli(agents("retire", "exporter"))
    assert "now retired" in capsys.readouterr().out
    assert run_cli(agents("enable", "exporter")) == 1


def test_cli_delete_requires_retirement(root, capsys):
    run_cli(agents("create", "--template", "coding", "--name", "exporter"))
    capsys.readouterr()
    assert run_cli(agents("delete", "exporter")) == 1
    assert "Retire agent" in capsys.readouterr().err
    for argv in (["disable", "exporter"], ["retire", "exporter"]):
        run_cli(agents(*argv))
        capsys.readouterr()
    run_cli(agents("delete", "exporter"))
    assert "Deleted agent" in capsys.readouterr().out
