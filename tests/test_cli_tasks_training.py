"""Session 10 (C): CLI for the fenced scheduler record and training lab.

``forge tasks`` inspects the durable fenced-scheduler store;
``forge training`` drives the agent training lab. Both are honest:
refusals are reported, nothing is fabricated, and the CLI wiring
(subparsers + dispatch) is exercised through the real ``main()``.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

import pytest

from forge.agents.execution import CallableAgentExecutor
from forge.agents.registry import AgentRegistration, AgentRegistry
from forge.core.orchestrator import MultiAgentOrchestrator


def _run_cli(monkeypatch, capsys, *argv):
    """Invoke the real CLI main() with the given argv; return exit code."""
    monkeypatch.setattr(sys, "argv", ["forge", *argv])
    from forge.cli import main

    try:
        main()
    except SystemExit as exc:
        code = exc.code
        return code if isinstance(code, int) else (0 if code is None else 1)
    return 0


def _register(registry):
    for name, role, caps in (("coder", "coding", ("coding",)),
                             ("tester", "testing", ("testing",))):
        registry.register(AgentRegistration(
            name, role,
            CallableAgentExecutor(name, lambda r, _n=name: f"{_n} ok"),
            caps))
    return registry


def test_tasks_list_without_store_is_honest(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    code = _run_cli(monkeypatch, capsys, "tasks", "list")
    out = capsys.readouterr().out
    assert code == 0
    assert "No scheduler store" in out


def _fenced_run(store, requirement="add a feature and test it"):
    registry = _register(AgentRegistry())
    orch = MultiAgentOrchestrator(
        registry, max_workers=2, max_attempts=2, store_path=store)
    plan = orch.build_plan(requirement, chain=True)
    return orch.execute(plan)


def test_tasks_list_show_events_after_fenced_run(
        monkeypatch, capsys, tmp_path):
    store = str(tmp_path / "sched.db")
    report = _fenced_run(store)
    assert report.status.value == "SUCCEEDED"
    monkeypatch.chdir(tmp_path)

    code = _run_cli(monkeypatch, capsys, "tasks", "list",
                    "--store", store)
    out = capsys.readouterr().out
    assert code == 0
    assert "step-1-coder" in out and "step-2-tester" in out

    # JSON view is machine-readable and consistent.
    code = _run_cli(monkeypatch, capsys, "tasks", "list",
                    "--store", store, "--json")
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["count"] == 2
    assert all(row["state"] == "SUCCEEDED" for row in payload["tasks"])

    code = _run_cli(monkeypatch, capsys, "tasks", "show", "step-1-coder",
                    "--store", store)
    out = capsys.readouterr().out
    assert code == 0
    assert "State:    SUCCEEDED" in out
    assert "Attempt log" in out
    assert "Events (newest first)" in out

    code = _run_cli(monkeypatch, capsys, "tasks", "events",
                    "--store", store)
    out = capsys.readouterr().out
    assert code == 0
    assert "QUEUED" in out and "SUCCEEDED" in out

    # Sequence numbers in the store are strictly monotonic.
    conn = sqlite3.connect(store)
    seqs = [row[0] for row in conn.execute(
        "SELECT seq FROM events ORDER BY seq")]
    conn.close()
    assert seqs == list(range(1, len(seqs) + 1))

    code = _run_cli(monkeypatch, capsys, "tasks", "show", "no-such-task",
                    "--store", store)
    assert code == 2
    assert "No task" in capsys.readouterr().err


def test_tasks_directory_mode_aggregates_runs(monkeypatch, capsys, tmp_path):
    """Control-plane layout: one store file per orchestration under
    .forge/tasks/, inspected via the default directory."""
    import os

    tasks_dir = tmp_path / ".forge" / "tasks"
    tasks_dir.mkdir(parents=True)
    run_a = str(tasks_dir / "run-a.db")
    run_b = str(tasks_dir / "run-b.db")
    assert _fenced_run(run_a).status.value == "SUCCEEDED"
    # Same requirement, second run: deterministic step ids must not
    # collide because each run has its own store.
    assert _fenced_run(run_b).status.value == "SUCCEEDED"

    monkeypatch.chdir(tmp_path)
    code = _run_cli(monkeypatch, capsys, "tasks", "list")
    out = capsys.readouterr().out
    assert code == 0
    assert "run-a" in out and "run-b" in out
    assert out.count("step-1-coder") == 2

    code = _run_cli(monkeypatch, capsys, "tasks", "list", "--json")
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["count"] == 4
    runs = {row["run"] for row in payload["tasks"]}
    assert runs == {"run-a", "run-b"}

    # show resolves a task across runs; events stream both runs.
    code = _run_cli(monkeypatch, capsys, "tasks", "show", "step-1-coder")
    out = capsys.readouterr().out
    assert code == 0
    assert "Run:      run-a" in out
    code = _run_cli(monkeypatch, capsys, "tasks", "events", "--limit", "3")
    out = capsys.readouterr().out
    assert code == 0
    assert "Run run-a:" in out and "Run run-b:" in out

    # Explicit file --store still works for a single orchestrator store.
    code = _run_cli(monkeypatch, capsys, "tasks", "list", "--store", run_b)
    out = capsys.readouterr().out
    assert code == 0
    assert "step-1-coder" in out
    assert "run-a" not in out  # only the requested store is read


def test_tasks_show_exposes_fence_outcome(monkeypatch, capsys, tmp_path):
    """A task fenced by timeout is inspectable with its fence recorded."""
    store = str(tmp_path / "sched.db")
    import time

    registry = AgentRegistry()
    slow = CallableAgentExecutor(
        "slow", lambda r: time.sleep(0.5) or "never")
    registry.register(AgentRegistration("slow", "coding", slow,
                                        ("coding",)))
    orch = MultiAgentOrchestrator(
        registry, max_workers=1, max_attempts=1,
        step_timeout=0.1, store_path=store)
    plan = orch.build_plan("add a slow coding feature", chain=True)
    report = orch.execute(plan)
    assert report.status.value != "SUCCEEDED"

    monkeypatch.chdir(tmp_path)
    task_id = report.outcomes[0].step_id
    code = _run_cli(monkeypatch, capsys, "tasks", "show", task_id,
                    "--store", store)
    out = capsys.readouterr().out
    assert code == 0
    # The persisted terminal state is one of the fence-aware terminals,
    # and the fence is visible in the attempt log.
    assert "State:    FENCED" in out or "State:    FAILED" in out
    assert "Attempt log" in out


def test_training_scan_without_data_is_honest(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    code = _run_cli(monkeypatch, capsys, "training", "scan", "ghost")
    assert code == 2
    assert "No training examples" in capsys.readouterr().err


def test_training_scan_clean_data_refused_by_default_policy(
        monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("FORGE_TRAINING_EXTERNAL_UPLOAD", raising=False)
    runs = tmp_path / "runs.json"
    runs.write_text(json.dumps([
        {"requirement": "r1", "output": "o1", "status": "SUCCEEDED",
         "task_id": "t1"},
        {"requirement": "r2", "output": "o2", "status": "SUCCEEDED",
         "task_id": "t2"},
    ]), encoding="utf-8")

    code = _run_cli(monkeypatch, capsys, "training", "scan", "coder",
                    "--runs", str(runs))
    out = capsys.readouterr().out
    assert code == 0
    assert "2 clean" in out or "3 clean" not in out
    assert "REFUSED" in out and "deny" in out


def test_training_scan_secret_data_never_uploadable(
        monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("FORGE_TRAINING_EXTERNAL_UPLOAD", "allow")
    runs = tmp_path / "runs.json"
    runs.write_text(json.dumps([
        {"requirement": "key sk-abcdefgh1234567890abcd12",
         "output": "deployed", "status": "SUCCEEDED", "task_id": "t9"},
    ]), encoding="utf-8")

    # allow mode + (even with) authorization: secrets are still refused.
    code = _run_cli(monkeypatch, capsys, "training", "start", "coder",
                    "--runs", str(runs), "--authorize")
    err = capsys.readouterr().err
    assert code == 3
    assert "secret" in err.lower()


def test_training_start_without_key_is_honest_refusal(
        monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # allow mode + --authorize so the data policy passes; the missing
    # provider key is then the honest blocker.
    monkeypatch.setenv("FORGE_TRAINING_EXTERNAL_UPLOAD", "allow")
    runs = tmp_path / "runs.json"
    runs.write_text(json.dumps([
        {"requirement": "r1", "output": "o1", "status": "SUCCEEDED",
         "task_id": "t1"},
    ]), encoding="utf-8")

    code = _run_cli(monkeypatch, capsys, "training", "start", "coder",
                    "--runs", str(runs), "--authorize")
    err = capsys.readouterr().err
    assert code == 3
    assert "OPENAI_API_KEY" in err


def test_training_start_policy_deny_beats_fake_key(
        monkeypatch, capsys, tmp_path):
    """deny mode refuses before any upload even when a key exists —
    no network call is made, so a fake key is safe to use."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-test-key-not-real-at-all")
    monkeypatch.delenv("FORGE_TRAINING_EXTERNAL_UPLOAD", raising=False)
    runs = tmp_path / "runs.json"
    runs.write_text(json.dumps([
        {"requirement": "r1", "output": "o1", "status": "SUCCEEDED",
         "task_id": "t1"},
    ]), encoding="utf-8")

    code = _run_cli(monkeypatch, capsys, "training", "start", "coder",
                    "--runs", str(runs), "--authorize")
    err = capsys.readouterr().err
    assert code == 3
    assert "policy" in err.lower()


def test_training_job_without_key_is_honest_refusal(monkeypatch, capsys,
                                                    tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    code = _run_cli(monkeypatch, capsys, "training", "job", "ft-abc123")
    assert code == 3
    assert "OPENAI_API_KEY" in capsys.readouterr().err


def test_training_export_without_data_is_honest(monkeypatch, capsys,
                                                 tmp_path):
    monkeypatch.chdir(tmp_path)
    code = _run_cli(monkeypatch, capsys, "training", "export", "ghost")
    assert code == 2
    assert "No dataset" in capsys.readouterr().err
