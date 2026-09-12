"""CLI surface for long-term memory: ``forge memory``, ``search``, ``stats``,
plus the ``add``/``correct``/``delete`` management subcommands."""
from __future__ import annotations

import json
import sys

import pytest

import forge.cli as cli


def run_cli(argv, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["forge"] + argv)
    code = 0
    try:
        cli.main()
    except SystemExit as exc:
        code = exc.code if exc.code is not None else 0
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def run_cli_json(argv, monkeypatch, capsys):
    code, out, err = run_cli(argv, monkeypatch, capsys)
    assert code == 0, f"expected success, got {code}: {err or out}"
    return json.loads(out)


def test_memory_add_and_list_json(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "m.db")
    code, out, _ = run_cli(
        ["memory", "add", "--db", db, "--project", "demo",
         "--type", "project", "The cache uses Redis with a 5 minute TTL."],
        monkeypatch, capsys)
    assert code == 0
    assert "Stored" in out

    payload = run_cli_json(
        ["memory", "--db", db, "--project", "demo", "--json"],
        monkeypatch, capsys)
    assert len(payload["records"]) == 1
    assert payload["records"][0]["type"] == "project"


def test_memory_search_and_stats(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "m.db")
    run_cli(["memory", "add", "--db", db, "--project", "demo",
             "--type", "project", "PostgreSQL 15 powers the database."],
            monkeypatch, capsys)
    run_cli(["memory", "add", "--db", db, "--project", "demo",
             "--type", "failure", "The database pool exhausted connections."],
            monkeypatch, capsys)

    results = run_cli_json(
        ["memory", "search", "--db", db, "--project", "demo",
         "--json", "postgres database"], monkeypatch, capsys)
    assert results["results"]
    assert "PostgreSQL" in results["results"][0]["record"]["content"]

    stats = run_cli_json(
        ["memory", "stats", "--db", db, "--project", "demo", "--json"],
        monkeypatch, capsys)
    assert stats["total"] == 2
    assert stats["by_type"] == {"project": 1, "failure": 1}


def test_memory_add_rejects_secret(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "m.db")
    secret = "sk-" + "a" * 32
    code, out, _ = run_cli(
        ["memory", "add", "--db", db, "--project", "demo", secret],
        monkeypatch, capsys)
    assert code == 1
    assert "secret" in out.lower()


def test_memory_correct_and_delete(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "m.db")
    code, out, _ = run_cli(
        ["memory", "add", "--db", db, "--project", "demo",
         "--json", "The TTL is five minutes."], monkeypatch, capsys)
    assert code == 0
    record_id = json.loads(out)["record"]["id"]

    code, out, _ = run_cli(
        ["memory", "correct", "--db", db, "--project", "demo",
         record_id, "The TTL is thirty minutes.", "--json"],
        monkeypatch, capsys)
    assert code == 0
    corrected = json.loads(out)
    assert corrected["version"] == 2
    assert corrected["supersedes"] == record_id

    code, out, _ = run_cli(
        ["memory", "delete", "--db", db, "--project", "demo",
         corrected["id"], "--json"], monkeypatch, capsys)
    assert code == 0
    assert json.loads(out)["deleted"] is True

    payload = run_cli_json(
        ["memory", "--db", db, "--project", "demo", "--json"],
        monkeypatch, capsys)
    assert payload["records"] == []


def test_memory_unknown_type_fails_cleanly(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "m.db")
    code, _, err = run_cli(
        ["memory", "add", "--db", db, "--project", "demo",
         "--type", "bogus", "some content"], monkeypatch, capsys)
    assert code == 2
    assert "memory" in err.lower()


def test_memory_help_lists_subcommands(monkeypatch, capsys):
    code, out, _ = run_cli(["memory", "--help"], monkeypatch, capsys)
    assert code == 0
    assert "search" in out
    assert "stats" in out
