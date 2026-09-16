"""AutoPilot (A84) tests: hand Forge a complex project, it completes it.

These drive the whole plan -> approve -> execute -> persist loop with a
deterministic provider that behaves like a model, and inspect the
resulting repository — never the final status only.
"""
import json
import subprocess

import pytest

from forge.architect.store import PlanStore
from forge.autopilot import (
    AutoPilot,
    AutoPilotError,
    STATUS_BLOCKED,
    STATUS_COMPLETED,
    STATUS_REFUSED,
    _execution_fingerprint,
)
from forge.core.supervisor import Supervisor
from forge.models.fabric import ModelFabric
from forge.models.provider import ModelResult, ProviderRegistry
from forge.models.registry import Model, ModelRegistry

REQUIREMENT = ("Build a todo list command line tool with JSON storage, "
               "an add/list/done workflow, and automated tests.")


def _repo(tmp_path):
    (tmp_path / "app.py").write_text("def health(): return True\n")
    (tmp_path / ".gitignore").write_text("__pycache__/\n.forge/\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text(
        "from app import health\n\n\ndef test_health():\n"
        "    assert health() is True\n")
    subprocess.run(["git", "init"], cwd=tmp_path, check=True,
                   capture_output=True)
    subprocess.run(["git", "config", "user.name", "Forge Test"],
                   cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"],
                   cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "--", ".gitignore", "app.py",
                    "tests/test_app.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path,
                   check=True, capture_output=True)
    return tmp_path


class ScriptedModel:
    """Deterministic provider: different change set per plan task."""

    name = "scripted"

    def __init__(self):
        self.calls = []

    def generate(self, prompt, *, context="", task="", instructions="",
                 max_output_tokens=None, temperature=None):
        self.calls.append(prompt)
        if "Review the following change set" in prompt:
            return ModelResult(
                json.dumps({"findings": [], "verdict": "APPROVE"}),
                self.name)
        summary, changes = self._task_payload(prompt)
        return ModelResult(json.dumps({
            "summary": summary,
            "changes": changes,
            "reasoning_summary": summary,
            "risks": [],
        }), self.name)

    def _task_payload(self, prompt):
        if "independent code review" in prompt:
            # Gate-repair prompt: address the findings with a real change.
            return "Address review findings", [
                {"path": "REVIEW_FIXES.md", "action": "create",
                 "content": "# Review fixes\n\n"
                            "Findings from the independent review were "
                            "addressed.\n"},
            ]
        if "TASK T03" in prompt:
            changes = [
                {"path": "core.py", "action": "create", "content": (
                    "import json\n\n\n"
                    "def load_todos(path):\n"
                    "    try:\n"
                    "        with open(path, encoding='utf-8') as handle:\n"
                    "            return json.load(handle)\n"
                    "    except (OSError, ValueError):\n"
                    "        return []\n\n\n"
                    "def add_todo(path, title):\n"
                    "    todos = load_todos(path)\n"
                    "    todos.append({'title': title, 'done': False})\n"
                    "    with open(path, 'w', encoding='utf-8') as handle:\n"
                    "        json.dump(todos, handle)\n"
                    "    return todos\n")},
                {"path": "tests/test_core.py", "action": "create",
                 "content": (
                     "from core import add_todo, load_todos\n\n\n"
                     "def test_add_and_load(tmp_path):\n"
                     "    path = str(tmp_path / 'todos.json')\n"
                     "    todos = add_todo(path, 'write tests')\n"
                     "    assert todos[0]['title'] == 'write tests'\n"
                     "    assert load_todos(path)[0]['done'] is False\n")},
            ]
            summary = "Implement the todo core with JSON storage"
        elif "TASK T04" in prompt:
            changes = [
                {"path": "cli.py", "action": "create", "content": (
                    "import sys\n\n"
                    "from core import add_todo, load_todos\n\n\n"
                    "def main(argv):\n"
                    "    argv = list(argv or [])\n"
                    "    if not argv:\n"
                    "        return 'usage: cli add|list <path>'\n"
                    "    command, path = argv[0], 'todos.json'\n"
                    "    if command == 'add' and len(argv) > 2:\n"
                    "        add_todo(argv[1], ' '.join(argv[2:]))\n"
                    "        return 'added'\n"
                    "    if command == 'list':\n"
                    "        lines = ['%s %s' % (\n"
                    "            'x' if item['done'] else '-',\n"
                    "            item['title'])\n"
                    "            for item in load_todos(path)]\n"
                    "        return '\\n'.join(lines) or 'empty'\n"
                    "    return 'unknown command'\n\n\n"
                    "if __name__ == '__main__':\n"
                    "    print(main(sys.argv[1:]))\n")},
                {"path": "tests/test_cli.py", "action": "create",
                 "content": (
                     "from cli import main\n\n\n"
                     "def test_usage():\n"
                     "    assert 'usage' in main([])\n\n\n"
                     "def test_list_empty():\n"
                     "    assert main(['list']) == 'empty'\n")},
            ]
            summary = "Implement the CLI delivery surface"
        elif "TASK T07" in prompt:
            changes = [
                {"path": "README.md", "action": "create", "content": (
                    "# Todo CLI\n\n"
                    "A tiny todo list tool with JSON storage.\n\n"
                    "## Usage\n\n"
                    "- `python cli.py add todos.json \"task\"`\n"
                    "- `python cli.py list`\n\n"
                    "## Limitations\n\n"
                    "- Storage is a single local JSON file.\n")},
            ]
            summary = "Document setup, usage, and limitations"
        else:
            changes = [
                {"path": "notes.txt", "action": "create",
                 "content": "no task matched this prompt\n"},
            ]
            summary = "Unmatched prompt fallback"
        return summary, changes


class AlwaysBrokenModel:
    """A model whose output never parses: every coding attempt fails."""

    name = "broken"

    def generate(self, prompt, *, context="", task="", instructions="",
                 max_output_tokens=None, temperature=None):
        return ModelResult("this is not the JSON you are looking for",
                           self.name)


class FailingThenFixingModel(ScriptedModel):
    """T03 fails its tests on the first attempt; the retry (which receives
    the failure context) produces the fix. Every other task behaves like
    the normal scripted model."""

    name = "fixer"

    def __init__(self):
        super().__init__()
        self.saw_failure_context = False

    def _task_payload(self, prompt):
        if "TASK T03" not in prompt:
            return super()._task_payload(prompt)
        if "PREVIOUS ATTEMPT FAILED" in prompt:
            self.saw_failure_context = True
            body = "def compute():\n    return 42\n"
        else:
            body = "def compute():\n    return 41\n"
        summary, changes = super()._task_payload(prompt)
        changes = list(changes) + [
            {"path": "compute.py", "action": "create", "content": body},
            {"path": "tests/test_compute.py", "action": "create",
             "content": "from compute import compute\n\n\n"
                        "def test_compute():\n"
                        "    assert compute() == 42\n"},
        ]
        return summary, changes


def _fabric(provider):
    return ModelFabric(
        registry=ModelRegistry([
            Model(name="m/auto", provider="p",
                  capabilities=("coding", "debugging", "review"),
                  free=True, local=True),
        ]),
        providers=ProviderRegistry({"p": provider}),
    )


def _pilot(tmp_path, provider, **kwargs):
    return AutoPilot(tmp_path, fabric=_fabric(provider),
                     max_task_retries=kwargs.pop("max_task_retries", 1),
                     **kwargs)


# -- the full loop ----------------------------------------------------------


def test_autopilot_completes_complex_project(tmp_path):
    _repo(tmp_path)
    provider = ScriptedModel()
    pilot = _pilot(tmp_path, provider)

    report = pilot.run(REQUIREMENT)

    assert report.status == STATUS_COMPLETED
    assert report.ok is True
    assert report.plan_id
    assert all(item.status == "done" for item in report.tasks), \
        [(item.task_id, item.role, item.status, item.error)
         for item in report.tasks]
    # coding tasks really changed files; verification tasks did not
    by_id = {item.task_id: item for item in report.tasks}
    assert by_id["T03"].files and by_id["T04"].files
    assert by_id["T01"].role == "architect" and not by_id["T01"].files
    assert by_id["T05"].evidence.get("verdict") == "APPROVE"
    assert by_id["T06"].evidence.get("passed") is True
    assert by_id["T08"].evidence.get("runs", 0) >= 1
    assert by_id["T09"].evidence.get("tests_passed") is True

    # The repository genuinely contains the finished project.
    assert (tmp_path / "core.py").exists()
    assert (tmp_path / "cli.py").exists()
    assert (tmp_path / "README.md").exists()
    assert "export" not in (tmp_path / "core.py").read_text()

    # Every accepted task became its own commit.
    commits = subprocess.run(
        ["git", "log", "--format=%s"], cwd=tmp_path, text=True,
        capture_output=True).stdout.splitlines()
    forge_commits = [line for line in commits if line.startswith("forge: ")]
    assert len(forge_commits) >= 3
    assert report.commits >= 3
    assert "core.py" in report.files_changed
    assert "cli.py" in report.files_changed

    # The plan is completed, approved content intact, and persisted.
    store = PlanStore(tmp_path)
    plan = store.load(report.plan_id)
    assert plan.status == "completed"
    assert store.approval(report.plan_id) is not None

    # Research findings (or their honest absence) were recorded in
    # long-term memory with a source.
    memory = (tmp_path / ".forge" / "memory.json")
    assert memory.is_file()
    payload = json.loads(memory.read_text(encoding="utf-8"))
    entries = payload.get("entries") or []
    research = [entry for entry in entries
                if "AutoPilot research" in str(entry.get("title", ""))]
    assert research
    assert all(entry.get("source") == "autopilot" for entry in research)

    # Execution bookkeeping was persisted.
    state_path = tmp_path / ".forge" / "autopilot" / (
        "run-%s.json" % report.plan_id)
    assert state_path.is_file()
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["plan_id"] == report.plan_id
    assert state.get("outcomes")


def test_autopilot_full_loop_survives_reentry(tmp_path):
    """Resuming a completed run refuses instead of redoing work."""
    _repo(tmp_path)
    pilot = _pilot(tmp_path, ScriptedModel())
    report = pilot.run(REQUIREMENT)
    assert report.ok

    again = _pilot(tmp_path, ScriptedModel()).resume(report.plan_id)
    assert again.status == STATUS_REFUSED
    assert "already completed" in again.reason


# -- pre-flight refusals ----------------------------------------------------


def test_autopilot_refuses_without_a_real_model(tmp_path):
    _repo(tmp_path)
    from forge.models.config import FabricConfig

    fallback_only = ModelFabric.from_defaults(FabricConfig(
        ollama_enabled=False, openai_enabled=False, local_enabled=True))
    pilot = AutoPilot(tmp_path, fabric=fallback_only)

    report = pilot.run(REQUIREMENT)

    assert report.status == STATUS_REFUSED
    assert report.plan_id == ""
    assert "model" in report.reason.lower()
    # Nothing was planned, approved, or written.
    assert not (tmp_path / ".forge" / "plans").exists()
    assert not (tmp_path / "core.py").exists()


def test_autopilot_force_bypasses_model_gate_but_fails_honestly(tmp_path):
    _repo(tmp_path)
    from forge.models.config import FabricConfig

    fallback_only = ModelFabric.from_defaults(FabricConfig(
        ollama_enabled=False, openai_enabled=False, local_enabled=True))
    pilot = AutoPilot(tmp_path, fabric=fallback_only)

    report = pilot.run(REQUIREMENT, force=True)

    # The gate was bypassed, so the run proceeds and then fails honestly at
    # the first coding task: the fallback proposes no changes.
    assert report.status == STATUS_BLOCKED
    by_id = {item.task_id: item for item in report.tasks}
    assert by_id["T03"].status == "blocked"
    assert by_id["T03"].error


def test_autopilot_refuses_non_git_root(tmp_path):
    target = tmp_path / "plain-dir"
    target.mkdir()
    pilot = _pilot(tmp_path, ScriptedModel())
    pilot.root = target

    report = pilot.run(REQUIREMENT)

    assert report.status == STATUS_REFUSED
    assert "git" in report.reason


def test_autopilot_refuses_empty_requirement(tmp_path):
    _repo(tmp_path)
    report = _pilot(tmp_path, ScriptedModel()).run("   ")
    assert report.status == STATUS_REFUSED
    assert "empty" in report.reason


def test_autopilot_rejects_read_only_modes(tmp_path):
    _repo(tmp_path)
    from forge.security.permissions import OperationMode

    with pytest.raises(AutoPilotError):
        AutoPilot(tmp_path, fabric=_fabric(ScriptedModel()),
                  mode=OperationMode.SAFE)
    with pytest.raises(AutoPilotError):
        AutoPilot(tmp_path, fabric=_fabric(ScriptedModel()),
                  mode=OperationMode.LOCKED)


# -- dry run ------------------------------------------------------------------


def test_autopilot_dry_run_plans_without_executing(tmp_path):
    _repo(tmp_path)
    provider = ScriptedModel()
    pilot = _pilot(tmp_path, provider)

    report = pilot.run(REQUIREMENT, dry_run=True)

    assert report.status == STATUS_REFUSED
    assert "dry run" in report.reason
    assert report.plan_id
    store = PlanStore(tmp_path)
    plan = store.load(report.plan_id)
    assert plan.status == "draft"
    assert store.approval(report.plan_id) is None
    assert provider.calls == []          # no model was ever asked
    assert not (tmp_path / "core.py").exists()


# -- failure, blocking, and resume -------------------------------------------


def test_autopilot_blocks_then_resumes_to_completion(tmp_path):
    _repo(tmp_path)

    broken = _pilot(tmp_path, AlwaysBrokenModel())
    report = broken.run(REQUIREMENT)

    assert report.status == STATUS_BLOCKED
    by_id = {item.task_id: item for item in report.tasks}
    assert by_id["T03"].status == "blocked"
    assert by_id["T03"].attempts == 2          # first + one retry
    # Dependent tasks were never attempted.
    assert by_id["T04"].status in ("skipped", "pending")
    assert by_id["T04"].attempts == 0
    # The plan stays executing (not completed), so it can be resumed.
    plan = PlanStore(tmp_path).load(report.plan_id)
    assert plan.status == "executing"
    assert plan.task("T03").status == "blocked"
    # No spurious commit was produced by the failing task.
    assert not (tmp_path / "core.py").exists()

    fixed = _pilot(tmp_path, ScriptedModel())
    resumed = fixed.resume(report.plan_id)

    assert resumed.status == STATUS_COMPLETED
    assert resumed.resumed is True
    assert all(item.status == "done" for item in resumed.tasks), \
        [(item.task_id, item.status, item.error)
         for item in resumed.tasks]
    assert (tmp_path / "core.py").exists()
    assert PlanStore(tmp_path).load(report.plan_id).status == "completed"
    # The attempt history keeps both passes.
    state = json.loads((tmp_path / ".forge" / "autopilot" / (
        "run-%s.json" % report.plan_id)).read_text(encoding="utf-8"))
    t3_attempts = state["attempts"]["T03"]
    assert {item["pass"] for item in t3_attempts} == {1, 2}
    assert all(item["ok"] for item in t3_attempts
               if item["pass"] == 2)


def test_autopilot_retry_receives_failure_context(tmp_path):
    _repo(tmp_path)
    provider = FailingThenFixingModel()
    pilot = _pilot(tmp_path, provider, max_task_retries=1)

    report = pilot.run(REQUIREMENT)

    assert provider.saw_failure_context is True
    assert report.status == STATUS_COMPLETED
    by_id = {item.task_id: item for item in report.tasks}
    assert by_id["T03"].status == "done"
    assert by_id["T03"].attempts == 2          # failed once, fixed on retry
    assert "compute" in (tmp_path / "compute.py").read_text()


def test_autopilot_resume_refuses_draft_plan(tmp_path):
    _repo(tmp_path)
    from forge.architect import ProjectArchitect

    plan = ProjectArchitect(tmp_path).plan(REQUIREMENT)
    store = PlanStore(tmp_path)
    store.save(plan)

    report = _pilot(tmp_path, ScriptedModel()).resume(plan.id)

    assert report.status == STATUS_REFUSED
    assert "approve" in report.reason


def test_autopilot_resume_refuses_plan_edited_after_approval(tmp_path):
    _repo(tmp_path)
    provider = ScriptedModel()
    pilot = _pilot(tmp_path, provider)
    report = pilot.run(REQUIREMENT, dry_run=True)
    store = PlanStore(tmp_path)
    store.approve(report.plan_id, actor="tester")
    plan = store.checkout_for_execution(report.plan_id)
    assert plan.status == "executing"

    # Tamper with the approved scope directly (bypassing PlanStore.update),
    # keeping the approval record: what was approved is not what would run.
    path = store._path(report.plan_id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["specifications"][0]["statement"] = "smuggled scope change"
    path.write_text(json.dumps(payload), encoding="utf-8")

    resumed = _pilot(tmp_path, provider).resume(report.plan_id)

    assert resumed.status == STATUS_REFUSED
    assert "edited after approval" in resumed.reason
    assert provider.calls == []


def test_autopilot_resume_refuses_unknown_plan(tmp_path):
    _repo(tmp_path)
    report = _pilot(tmp_path, ScriptedModel()).resume("no-such-plan")
    assert report.status == STATUS_REFUSED
    assert "cannot load plan" in report.reason


def test_execution_fingerprint_ignores_statuses_only(tmp_path):
    _repo(tmp_path)
    from forge.architect import ProjectArchitect

    plan = ProjectArchitect(tmp_path).plan(REQUIREMENT)
    base = _execution_fingerprint(plan)
    plan.tasks[0].status = "done"
    plan.tasks[1].status = "blocked"
    plan.status = "executing"
    assert _execution_fingerprint(plan) == base
    plan.status = "completed"
    assert _execution_fingerprint(plan) == base
    plan.tasks[0].title = "different work"
    assert _execution_fingerprint(plan) != base


def test_autopilot_review_failure_triggers_bounded_repair(tmp_path,
                                                          monkeypatch):
    """A failing gate is repaired through the Supervisor, then re-checked."""
    _repo(tmp_path)
    provider = ScriptedModel()

    from forge.security.review import (
        FindingSeverity,
        ReviewDecision,
        ReviewFinding,
        ReviewGate,
        ReviewVerdict,
    )

    real_review = ReviewGate.review
    injected = {"done": False}

    def flaky_review(self, diff="", changed_files=None, *, requirement="",
                     model_findings=None):
        decision = real_review(self, diff, changed_files,
                               requirement=requirement,
                               model_findings=model_findings)
        # The AutoPilot's first cumulative review requests changes; every
        # later review (including the supervisor's own) behaves normally.
        if (not injected["done"]
                and requirement == "AutoPilot cumulative review"):
            injected["done"] = True
            return ReviewDecision(
                verdict=ReviewVerdict.REQUEST_CHANGES,
                findings=[ReviewFinding(
                    FindingSeverity.MEDIUM,
                    "delivery surface lacks usage notes", rule="test-inject")],
                changed_files=list(changed_files or []),
                reason="injected finding for the test")
        return decision

    monkeypatch.setattr(ReviewGate, "review", flaky_review)

    report = _pilot(tmp_path, provider).run(REQUIREMENT)

    assert injected["done"] is True
    assert report.status == STATUS_COMPLETED
    by_id = {item.task_id: item for item in report.tasks}
    review_task = by_id["T05"]
    assert review_task.status == "done"
    assert review_task.attempts == 1              # fixed inside one attempt
    assert review_task.evidence.get("after_repair", {}).get("verdict") \
        == "APPROVE"
    # The repair was a real committed change.
    assert (tmp_path / "REVIEW_FIXES.md").exists()
    assert "REVIEW_FIXES.md" in review_task.files


# -- supervisor integrity ------------------------------------------------------


def test_autopilot_writes_only_through_the_supervisor(tmp_path,
                                                      monkeypatch):
    """The AutoPilot has no side door into the target tree."""
    _repo(tmp_path)
    calls = {"supervisor": 0}
    real_run = Supervisor.run

    def counting_run(self, requirement, **kwargs):
        calls["supervisor"] += 1
        return real_run(self, requirement, **kwargs)

    monkeypatch.setattr(Supervisor, "run", counting_run)
    report = _pilot(tmp_path, ScriptedModel()).run(REQUIREMENT)

    assert report.ok
    # Every task that changed files went through exactly one Supervisor
    # transaction — the AutoPilot has no other way to write.
    changing = [item for item in report.tasks if item.files]
    assert len(changing) >= 3          # core, delivery, docs at minimum
    assert calls["supervisor"] == len(changing)


# -- CLI -----------------------------------------------------------------------


def test_cli_auto_requires_requirement_or_resume(tmp_path, monkeypatch,
                                                 capsys):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        _run_cli(["forge", "auto"])
    assert exc.value.code == 2
    assert "requirement" in capsys.readouterr().err


def _run_cli(argv):
    import sys
    from unittest.mock import patch

    from forge.cli import main

    with patch.object(sys, "argv", argv):
        main()


def test_cli_auto_dry_run_creates_reviewable_plan(tmp_path, monkeypatch,
                                                  capsys):
    _repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        _run_cli(["forge", "auto", REQUIREMENT, "--dry-run"])
    # dry-run refuses to execute: exit code 2 by design, plan still stored
    assert exc.value.code == 2
    out = capsys.readouterr().out
    assert "dry run" in out
    entries = list(PlanStore(tmp_path).list())
    assert len(entries) == 1
    assert entries[0]["status"] == "draft"


def test_cli_auto_full_loop_json(tmp_path, monkeypatch, capsys):
    """The CLI path end to end with a scripted fabric."""
    _repo(tmp_path)
    monkeypatch.chdir(tmp_path)

    import forge.cli as cli

    provider = ScriptedModel()
    monkeypatch.setattr(cli, "_build_fabric", lambda args: _fabric(provider))
    with pytest.raises(SystemExit) as exc:
        _run_cli(["forge", "auto", REQUIREMENT, "--root", ".", "--json",
                  "--max-task-retries", "1"])
    assert exc.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "completed"
    assert payload["ok"] is True
    assert payload["commits"] >= 3
    assert all(task["status"] == "done" for task in payload["tasks"])


def test_cli_auto_blocked_exit_code(tmp_path, monkeypatch, capsys):
    _repo(tmp_path)
    monkeypatch.chdir(tmp_path)

    import forge.cli as cli

    monkeypatch.setattr(cli, "_build_fabric",
                        lambda args: _fabric(AlwaysBrokenModel()))
    with pytest.raises(SystemExit) as exc:
        _run_cli(["forge", "auto", REQUIREMENT, "--root", ".", "--json"])
    assert exc.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "blocked"


def test_cli_engineer_execute_auto_runs_approved_plan(tmp_path, monkeypatch,
                                                      capsys):
    _repo(tmp_path)
    monkeypatch.chdir(tmp_path)

    import forge.cli as cli

    provider = ScriptedModel()
    from forge.architect import ProjectArchitect

    architect_plan = ProjectArchitect(tmp_path).plan(REQUIREMENT)
    store = PlanStore(tmp_path)
    store.save(architect_plan)
    store.approve(architect_plan.id, actor="tester")

    monkeypatch.setattr(cli, "_build_fabric", lambda args: _fabric(provider))
    with pytest.raises(SystemExit) as exc:
        _run_cli(["forge", "engineer", "execute", architect_plan.id,
                  "--auto", "--json"])
    assert exc.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "completed"
    assert (tmp_path / "core.py").exists()


def test_cli_engineer_execute_auto_refuses_draft(tmp_path, monkeypatch,
                                                 capsys):
    _repo(tmp_path)
    monkeypatch.chdir(tmp_path)

    from forge.architect import ProjectArchitect

    plan = ProjectArchitect(tmp_path).plan(REQUIREMENT)
    PlanStore(tmp_path).save(plan)

    with pytest.raises(SystemExit) as exc:
        _run_cli(["forge", "engineer", "execute", plan.id, "--auto"])
    assert exc.value.code == 2
    out = capsys.readouterr().out
    assert "refused" in out and "draft" in out
