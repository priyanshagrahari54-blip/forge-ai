"""Strengthened autonomous-engineering E2E (A32 hardening).

Requirement: 'Add CSV export functionality to the project, add tests, and
update documentation.' A deterministic mock provider drives the exact same
production interfaces (Model Fabric -> coder -> ChangeSet -> PolicyGate ->
apply -> test -> review -> security -> acceptance -> safe commit) with no
bypasses, and the resulting behavior is verified for real.
"""
import json
import subprocess
import sys

from forge.core.supervisor import Supervisor
from forge.models.fabric import ModelFabric
from forge.models.provider import MockProvider, ProviderRegistry
from forge.models.registry import Model, ModelRegistry

REQUIREMENT = ("Add CSV export functionality to the project, add tests, "
               "and update documentation.")


def _repo(root):
    (root / "app.py").write_text(
        "def rows():\n    return [{'name': 'Ada', 'score': 3}]\n")
    (root / "docs").mkdir()
    (root / "docs" / "usage.md").write_text("# Usage\n")
    (root / ".gitignore").write_text("__pycache__/\n.forge/\n")
    (root / "tests").mkdir()
    (root / "tests" / "test_app.py").write_text(
        "from app import rows\n"
        "def test_rows():\n"
        "    assert rows()[0]['name'] == 'Ada'\n")
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Forge Test"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(
        ["git", "add", "--", ".gitignore", "app.py", "docs/usage.md",
         "tests/test_app.py"],
        cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=root, check=True,
                   capture_output=True)


PAYLOAD = json.dumps({
    "summary": "Add CSV export with tests and docs",
    "changes": [
        {"path": "app.py", "action": "modify",
         "content": (
             "import csv\nfrom io import StringIO\n\n"
             "def rows():\n    return [{'name': 'Ada', 'score': 3}]\n\n"
             "def export_csv():\n"
             "    output = StringIO()\n"
             "    writer = csv.DictWriter(output, fieldnames=['name', 'score'],"
             " lineterminator='\\n')\n"
             "    writer.writeheader()\n"
             "    writer.writerows(rows())\n"
             "    return output.getvalue()\n")},
        {"path": "tests/test_csv.py", "action": "create",
         "content": (
             "from app import export_csv\n"
             "def test_csv_export():\n"
             "    assert export_csv() == 'name,score\\nAda,3\\n'\n")},
        {"path": "docs/usage.md", "action": "modify",
         "content": "# Usage\n\nUse `export_csv()` to export rows as CSV.\n"},
    ],
    "tests_to_run": ["tests/test_csv.py"],
    "reasoning_summary": "added export, regression test, and docs",
    "risk_level": "low",
})


def _fabric():
    return ModelFabric(
        registry=ModelRegistry([
            Model(name="mock/e2e", provider="mock",
                  capabilities=("coding", "debugging", "review"),
                  free=True, local=True),
        ]),
        providers=ProviderRegistry({"mock": MockProvider(PAYLOAD)}),
    )


def test_csv_feature_tests_and_docs_end_to_end(tmp_path):
    _repo(tmp_path)
    (tmp_path / "notes.txt").write_text("user work\n")

    outcome = Supervisor("csv-e2e", tmp_path).run(
        REQUIREMENT, approved=True, fabric=_fabric())

    # -- orchestration ---------------------------------------------------
    assert outcome["accepted"] is True
    for stage in ("PLAN", "AGENTS", "MODEL", "CODE", "TEST", "RETEST",
                  "REVIEW", "SECURITY", "BENCHMARK", "ACCEPTANCE",
                  "CHECKPOINT", "COMMIT", "COMPLETED"):
        assert stage in outcome["stages"], stage
    assert "ROLLBACK" not in outcome["stages"]
    assert outcome["selected_agents"]
    assert outcome["selected_model"] == "mock/e2e"
    assert outcome["review"]["verdict"] == "APPROVE"
    assert outcome["acceptance"]["accepted"] is True
    assert outcome["acceptance"]["failed_gates"] == []

    # -- nothing bypassed the ChangeSet or the PolicyGate -----------------
    changed = ["app.py", "docs/usage.md", "tests/test_csv.py"]
    assert outcome["files"] == changed
    decisions = [item["details"] for item in outcome["report"]["events"]
                 if item["name"] == "permission_decision"]
    writes = [item for item in decisions if item["operation"] == "write_file"]
    assert sorted(item["path"] for item in writes) == changed
    assert all(item["decision"] == "ALLOW" for item in writes)
    commits = [item for item in decisions if item["operation"] == "git_commit"]
    assert len(commits) == 1 and commits[0]["decision"] == "ALLOW"

    # -- real behavior ----------------------------------------------------
    assert "export_csv" in (tmp_path / "docs" / "usage.md").read_text()
    check = subprocess.run(
        [sys.executable, "-c",
         "from app import export_csv; "
         "assert export_csv() == 'name,score\\nAda,3\\n'"],
        cwd=tmp_path, text=True, capture_output=True)
    assert check.returncode == 0, check.stderr

    # -- safe commit -------------------------------------------------------
    committed = subprocess.run(
        ["git", "show", "--format=", "--name-only", "HEAD"],
        cwd=tmp_path, text=True, capture_output=True).stdout.splitlines()
    assert committed == changed
    status = subprocess.run(["git", "status", "--short"], cwd=tmp_path,
                            text=True, capture_output=True).stdout
    assert "notes.txt" in status  # unrelated work survives, uncommitted
    assert (tmp_path / "notes.txt").read_text() == "user work\n"

    # -- observability ------------------------------------------------------
    report = outcome["report"]
    assert report["final_status"] == "COMPLETED"
    assert report["files_changed"] == changed
    assert report["timings"]["total"] >= 0
    assert "API key" not in json.dumps(report)
