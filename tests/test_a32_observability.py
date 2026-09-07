"""Supervisor observability tests (A32.10/A32.11/A32.14 rebuild).

Every run emits an ordered, redacted event log, measured per-phase timings,
and real model latency/token metadata when providers report it. Secrets never
appear in reports.
"""
import json
import subprocess

from forge.core.report import REDACTED, TaskReport, redact
from forge.core.supervisor import Supervisor
from forge.models.fabric import ModelFabric
from forge.models.provider import ModelResult, ProviderRegistry
from forge.models.registry import Model, ModelRegistry


def _repo(root):
    (root / "app.py").write_text("def health(): return True\n")
    (root / ".gitignore").write_text("__pycache__/\n.forge/\n")
    (root / "tests").mkdir()
    (root / "tests" / "test_app.py").write_text(
        "from app import health\ndef test_health():\n    assert health()\n")
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Forge Test"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(
        ["git", "add", "--", ".gitignore", "app.py", "tests/test_app.py"],
        cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=root, check=True,
                   capture_output=True)


class CleanModel:
    name = "clean"

    def __init__(self, input_tokens=0, output_tokens=0):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens

    def generate(self, prompt, *, context="", task="", instructions="",
                 max_output_tokens=None, temperature=None):
        if "Review the following change set" in prompt:
            text = json.dumps({"findings": [], "verdict": "APPROVE"})
        else:
            text = json.dumps({
                "summary": "Add CSV export",
                "changes": [
                    {"path": "app.py", "action": "modify",
                     "content": "def health(): return True\n\ndef export_csv():\n    return 'x'\n"},
                    {"path": "tests/test_csv.py", "action": "create",
                     "content": "from app import export_csv\ndef test_csv():\n    assert export_csv() == 'x'\n"},
                ],
                "tests_to_run": ["tests/test_csv.py"],
                "reasoning_summary": "added export",
                "risk_level": "low",
            })
        return ModelResult(text, self.name, input_tokens=self.input_tokens,
                           output_tokens=self.output_tokens)


class AlwaysBrokenModel:
    name = "broken"

    def generate(self, prompt, *, context="", task="", instructions="",
                 max_output_tokens=None, temperature=None):
        if "Review the following change set" in prompt:
            return ModelResult(json.dumps({"findings": [], "verdict": "APPROVE"}),
                               self.name)
        return ModelResult(json.dumps({
            "changes": {"app.py": "def health(): return False\n"}}), self.name)


def _fabric(provider):
    return ModelFabric(
        registry=ModelRegistry([
            Model(name="m/a32", provider="p",
                  capabilities=("coding", "debugging", "review"),
                  free=True, local=True),
        ]),
        providers=ProviderRegistry({"p": provider}),
    )


def _event_names(report):
    return [item["name"] for item in report["events"]]


def _assert_subsequence(names, expected):
    cursor = 0
    for name in expected:
        assert name in names[cursor:], f"{name} missing after {names[:cursor]}"
        cursor = names.index(name, cursor) + 1


# -- events ---------------------------------------------------------------

def test_success_path_emits_ordered_events(tmp_path):
    _repo(tmp_path)
    outcome = Supervisor("obs", tmp_path).run(
        "Add CSV export functionality and tests", approved=True,
        fabric=_fabric(CleanModel()))
    assert outcome["accepted"]
    _assert_subsequence(_event_names(outcome["report"]), [
        "task_started", "agents_selected", "model_selected", "change_proposed",
        "permission_decision", "change_applied", "test_executed",
        "review_result", "security_result", "benchmark_result",
        "acceptance_result", "commit",
    ])
    decisions = [item for item in outcome["report"]["events"]
                 if item["name"] == "permission_decision"]
    assert len(decisions) == 2
    assert all(item["details"]["decision"] == "ALLOW" for item in decisions)
    assert all(isinstance(item["t"], float) and item["t"] >= 0
               for item in outcome["report"]["events"])


def test_failure_path_emits_test_repair_and_rollback_events(tmp_path):
    _repo(tmp_path)
    outcome = Supervisor("obs-fail", tmp_path).run(
        "Add a feature", approved=True, fabric=_fabric(AlwaysBrokenModel()),
        max_debug_retries=2)
    assert not outcome["accepted"]
    names = _event_names(outcome["report"])
    _assert_subsequence(names, [
        "task_started", "change_applied", "test_executed", "test_failed",
        "repair_attempted", "rollback",
    ])
    assert "commit" not in names
    assert "acceptance_result" not in names
    failed = [item for item in outcome["report"]["events"]
              if item["name"] == "test_failed"]
    assert len(failed) == 2
    assert all(item["details"]["exit_code"] == 1 for item in failed)


# -- timings ---------------------------------------------------------------

def test_success_timings_cover_every_phase(tmp_path):
    _repo(tmp_path)
    outcome = Supervisor("obs-time", tmp_path).run(
        "Add CSV export", approved=True, fabric=_fabric(CleanModel()))
    timings = outcome["report"]["timings"]
    for phase in ("plan", "code", "test", "review", "security", "benchmark",
                  "acceptance", "commit", "total"):
        assert phase in timings, phase
        assert isinstance(timings[phase], float) and timings[phase] >= 0
    assert outcome["timings"] == timings


def test_failure_timings_record_completed_phases(tmp_path):
    _repo(tmp_path)
    outcome = Supervisor("obs-time-fail", tmp_path).run(
        "Add a feature", approved=True, fabric=_fabric(AlwaysBrokenModel()),
        max_debug_retries=1)
    timings = outcome["report"]["timings"]
    assert timings["plan"] >= 0 and timings["code"] >= 0
    assert timings["test"] >= 0 and timings["total"] >= 0
    assert "review" not in timings  # never reached


# -- model metadata ----------------------------------------------------------

def test_token_and_latency_metadata_recorded_when_reported(tmp_path):
    _repo(tmp_path)
    outcome = Supervisor("obs-tok", tmp_path).run(
        "Add CSV export", approved=True,
        fabric=_fabric(CleanModel(input_tokens=10, output_tokens=5)))
    report = outcome["report"]
    assert report["input_tokens"] == 20  # coder + reviewer calls
    assert report["output_tokens"] == 10
    assert report["model_latency_seconds"] >= 0
    assert outcome["token_usage"] == {"input": 20, "output": 10}


def test_tokens_unknown_on_legacy_router_without_token_data(tmp_path):
    from forge.models.router import ModelInfo, ModelRouter
    _repo(tmp_path)
    router = ModelRouter([ModelInfo(
        "legacy", "coding", available=True, provider=CleanModel(),
        capabilities=("coding", "debugging"))])
    outcome = Supervisor("obs-tok-none", tmp_path).run(
        "Add CSV export", approved=True, router=router)
    assert outcome["accepted"]
    assert outcome["report"]["input_tokens"] is None
    assert outcome["report"]["output_tokens"] is None
    assert outcome["token_usage"] == {"input": None, "output": None}


# -- redaction ---------------------------------------------------------------

def test_report_redacts_secrets_from_requirement_and_events(tmp_path):
    _repo(tmp_path)
    requirement = ("Add export with api_key = 'sup3r-secret-value' and "
                   "AWS key AKIA1234567890ABCDEF")
    outcome = Supervisor("obs-redact", tmp_path).run(
        requirement, approved=True, fabric=_fabric(CleanModel()))
    assert outcome["accepted"]
    dumped = json.dumps(outcome["report"])
    assert "sup3r-secret-value" not in dumped
    assert "AKIA1234567890ABCDEF" not in dumped
    assert REDACTED in outcome["report"]["requirement"]


def test_redact_masks_nested_values_and_private_keys():
    payload = {
        "token": "token = 'abcdef123456'",
        "nested": [{"key": "-----BEGIN RSA PRIVATE KEY-----\nabc\n"}],
        "db": "postgres://user:pw@host/db",
        "clean": "tokenizer.py",
    }
    masked = redact(payload)
    assert masked["token"] == REDACTED
    assert masked["nested"][0]["key"] == REDACTED
    assert masked["db"] == REDACTED
    assert masked["clean"] == "tokenizer.py"


def test_task_report_event_helper():
    report = TaskReport(task_id="t")
    report.record_event("demo", 1.5, {"ok": True})
    assert report.to_dict()["events"] == [
        {"name": "demo", "t": 1.5, "details": {"ok": True}}]
