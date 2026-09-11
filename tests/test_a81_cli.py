"""Agent Creation Engine (A81): the `forge agents` CLI."""
from __future__ import annotations

import json
import sys
from unittest.mock import patch

from forge.cli import main

from helpers_a81 import make_repo


def run_cli(argv) -> int:
    with patch.object(sys, "argv", argv):
        try:
            main()
        except SystemExit as exit_code:
            return int(exit_code.code or 0)
    return 0


def test_cli_agents_lists_templates_and_empty_store(tmp_path, capsys):
    make_repo(tmp_path)
    assert run_cli(["forge", "agents", "--root", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "No agents" in out
    assert run_cli(["forge", "agents", "templates",
                    "--root", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    for template in ("coding", "research", "security", "game-dev",
                     "os-dev", "documentation"):
        assert template in out


def test_cli_agents_create_requires_template_or_file(tmp_path, capsys):
    make_repo(tmp_path)
    code = run_cli(["forge", "agents", "create", "--root", str(tmp_path)])
    assert code == 1
    assert "template" in capsys.readouterr().err.lower()


def test_cli_full_lifecycle_and_json(tmp_path, capsys):
    make_repo(tmp_path)
    root = ["--root", str(tmp_path)]

    # create from a template
    assert run_cli(["forge", "agents", "create", *root, "--template",
                    "coding", "--name", "cli-coder",
                    "--purpose", "CLI driven coding"]) == 0
    assert "cli-coder" in capsys.readouterr().out

    # list shows it in `created`
    assert run_cli(["forge", "agents", *root]) == 0
    assert "cli-coder" in capsys.readouterr().out

    # create with --json emits the manifest
    assert run_cli(["forge", "agents", "create", *root, "--template",
                    "research", "--name", "cli-reader", "--json"]) == 0
    manifest = json.loads(capsys.readouterr().out)
    assert manifest["name"] == "cli-reader"
    assert manifest["status"] == "created"

    # validate -> test -> enable
    assert run_cli(["forge", "agents", "validate", *root,
                    "cli-coder"]) == 0
    assert run_cli(["forge", "agents", "test", *root, "cli-coder"]) == 0
    test_out = capsys.readouterr().out
    assert "Benchmark PASSED" in test_out
    assert "[PASS] permission-ceiling" in test_out
    assert run_cli(["forge", "agents", "enable", *root, "cli-coder"]) == 0
    assert "enabled" in capsys.readouterr().out

    # versions + show
    assert run_cli(["forge", "agents", "versions", *root, "cli-coder"]) == 0
    assert "v1" in capsys.readouterr().out
    assert run_cli(["forge", "agents", "show", *root, "cli-coder"]) == 0
    manifest = json.loads(capsys.readouterr().out)
    assert manifest["status"] == "enabled"

    # run a task (text-only answer from the offline fallback)
    assert run_cli(["forge", "agents", "run", *root, "cli-coder",
                    "say hello"]) == 0

    # pause -> resume -> disable
    assert run_cli(["forge", "agents", "pause", *root, "cli-coder"]) == 0
    assert run_cli(["forge", "agents", "resume", *root, "cli-coder"]) == 0
    assert run_cli(["forge", "agents", "disable", *root,
                    "cli-coder"]) == 0
    assert run_cli(["forge", "agents", *root]) == 0
    assert "disabled" in capsys.readouterr().out


def test_cli_refuses_shortcuts_and_bad_input(tmp_path, capsys):
    make_repo(tmp_path)
    root = ["--root", str(tmp_path)]
    assert run_cli(["forge", "agents", "create", *root, "--template",
                    "coding", "--name", "shortcut"]) == 0
    capsys.readouterr()
    # enabling a created agent directly is refused
    assert run_cli(["forge", "agents", "enable", *root, "shortcut"]) == 1
    assert "Cannot move" in capsys.readouterr().err
    # unknown agent
    assert run_cli(["forge", "agents", "validate", *root, "ghost"]) == 1
    assert "ghost" in capsys.readouterr().err
    # unknown template
    assert run_cli(["forge", "agents", "create", *root, "--template",
                    "toast", "--name", "burnt"]) == 1
    # invalid name
    assert run_cli(["forge", "agents", "create", *root, "--template",
                    "coding", "--name", "Bad Name"]) == 1


def test_cli_create_from_spec_file(tmp_path, capsys):
    make_repo(tmp_path)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps({
        "name": "file-agent",
        "purpose": "Created from a spec file",
        "capabilities": ["documentation"],
        "tools": ["read_file", "search"],
        "permissions": ["filesystem:read"],
    }))
    assert run_cli(["forge", "agents", "create",
                    "--root", str(tmp_path), "--file",
                    str(spec_path)]) == 0
    out = capsys.readouterr().out
    assert "file-agent" in out
    # the package landed in the persistent store
    index = json.loads((tmp_path / ".forge/agents/index.json").read_text())
    assert "file-agent" in index


def test_cli_rollback_creates_new_version(tmp_path, capsys):
    make_repo(tmp_path)
    root = ["--root", str(tmp_path)]
    assert run_cli(["forge", "agents", "create", *root, "--template",
                    "coding", "--name", "versioned",
                    "--purpose", "first purpose"]) == 0
    capsys.readouterr()
    spec_path = tmp_path / "v2.json"
    spec_path.write_text(json.dumps({
        "name": "versioned",
        "purpose": "second purpose",
        "capabilities": ["coding"],
        "tools": ["read_file", "search"],
        "permissions": ["filesystem:read"],
    }))
    assert run_cli(["forge", "agents", "create", *root, "--file",
                    str(spec_path)]) == 1  # duplicate name refused
    # update via factory path is exercised in engine tests; use rollback
    assert run_cli(["forge", "agents", "validate", *root,
                    "versioned"]) == 0
    assert run_cli(["forge", "agents", "rollback", *root, "versioned",
                    "1"]) == 0
    assert "rolled back" in capsys.readouterr().out
