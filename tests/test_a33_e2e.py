"""A33 end-to-end scenarios: full permission-gated engineering loop (§29)
and mock browser/desktop flows (§30)."""
import json
import subprocess

from forge.core.supervisor import Supervisor
from forge.models.fabric import ModelFabric
from forge.models.provider import ModelResult, ProviderRegistry
from forge.models.registry import Model, ModelRegistry
from forge.security.approvals import ApprovalStore
from forge.security.audit import AuditLog
from forge.security.policy import (
    PermissionPolicy,
    PermissionRule,
    Resource,
)
from forge.tools.browser import MockBrowser
from forge.tools.desktop import (
    DesktopAction,
    DesktopActionRequest,
    DesktopResource,
    MockDesktop,
)


def _repo(root):
    (root / "app.py").write_text("def rows():\n    return [{'n': 'Ada'}]\n")
    (root / ".gitignore").write_text("__pycache__/\n.forge/\n")
    (root / "tests").mkdir()
    (root / "tests" / "test_app.py").write_text(
        "from app import rows\ndef test_rows():\n    assert rows()\n")
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Forge Test"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(
        ["git", "add", "--", ".gitignore", "app.py", "tests/test_app.py"],
        cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=root, check=True,
                   capture_output=True)


class ScriptedLoop:
    name = "scripted-loop"

    def __init__(self, coder_payload, verdict="APPROVE"):
        self.coder_payload = coder_payload
        self.verdict = verdict

    def generate(self, prompt, *, context="", task="", instructions="",
                 max_output_tokens=None, temperature=None):
        if "Review the following change set" in prompt:
            findings = [] if self.verdict == "APPROVE" else [
                {"severity": "HIGH", "message": "scripted rejection",
                 "path": "app.py"}]
            return ModelResult(json.dumps(
                {"findings": findings, "verdict": self.verdict}), self.name)
        return ModelResult(self.coder_payload, self.name)


def _fabric(provider):
    return ModelFabric(
        registry=ModelRegistry([
            Model(name="m/a33", provider="p",
                  capabilities=("coding", "debugging", "review"),
                  free=True, local=True),
        ]),
        providers=ProviderRegistry({"p": provider}),
    )


CSV_PAYLOAD = json.dumps({
    "summary": "Add CSV export",
    "changes": [
        {"path": "app.py", "action": "modify",
         "content": ("def rows():\n    return [{'n': 'Ada'}]\n\n"
                     "def export_csv():\n    return 'n\\nAda\\n'\n")},
        {"path": "tests/test_csv.py", "action": "create",
         "content": ("from app import export_csv\ndef test_csv():\n"
                     "    assert export_csv() == 'n\\nAda\\n'\n")},
    ],
    "tests_to_run": ["tests/test_csv.py"],
    "reasoning_summary": "added export",
    "risk_level": "low",
})


def _platform_policy():
    return PermissionPolicy(rules=[
        PermissionRule("task-write", Resource.FILESYSTEM, "write",
                       "ALLOW", scope="**",
                       reason="platform E2E allows task writes"),
        PermissionRule("no-secrets", Resource.FILESYSTEM, "write",
                       "DENY", scope="secrets/**",
                       reason="secrets stay frozen"),
        PermissionRule("commit-gate", Resource.GIT, "commit",
                       "REQUIRE_APPROVAL",
                       reason="publishing needs explicit approval"),
    ])


def test_csv_e2e_through_permission_platform(tmp_path):
    _repo(tmp_path)
    (tmp_path / "notes.txt").write_text("user work\n")
    store = ApprovalStore()
    audit = AuditLog()
    outcome = Supervisor("csv-a33", tmp_path).run(
        "Add CSV export functionality and update its tests.", approved=True,
        fabric=_fabric(ScriptedLoop(CSV_PAYLOAD)),
        policy=_platform_policy(), approval_store=store, audit_log=audit)

    # -- flow completed through every gate -----------------------------------
    assert outcome["accepted"] is True
    for stage in ("PLAN", "MODEL", "CODE", "TEST", "REVIEW", "SECURITY",
                  "ACCEPTANCE", "CHECKPOINT", "COMMIT", "COMPLETED"):
        assert stage in outcome["stages"], stage
    assert "ROLLBACK" not in outcome["stages"]

    # -- only authorized files changed -----------------------------------------
    assert outcome["files"] == ["app.py", "tests/test_csv.py"]
    committed = subprocess.run(
        ["git", "show", "--format=", "--name-only", "HEAD"],
        cwd=tmp_path, text=True, capture_output=True).stdout.splitlines()
    assert committed == ["app.py", "tests/test_csv.py"]
    assert (tmp_path / "notes.txt").read_text() == "user work\n"
    status = subprocess.run(["git", "status", "--short"], cwd=tmp_path,
                            text=True, capture_output=True).stdout
    assert "notes.txt" in status

    # -- temporary task-scoped permission ---------------------------------------
    assert outcome["task_grant"]["files"] == ["app.py", "tests/test_csv.py"]
    assert outcome["task_grant"]["task_id"] == outcome["task"]["id"]
    assert store.active_grants(outcome["task"]["id"]) == []

    # -- audit report -------------------------------------------------------------
    events = outcome["audit_events"]
    assert events
    assert all(item["task_id"] == outcome["task"]["id"] for item in events)
    writes = [item for item in events
              if item["resource"] == "filesystem"
              and item["operation"] == "write_file"]
    assert writes and all(item["decision"] == "ALLOW" for item in writes)
    commits = [item for item in events if item["operation"] == "git_commit"]
    assert commits and all(item["decision"] == "ALLOW" for item in commits)
    assert "AKIA" not in json.dumps(events)

    # -- approval semantics: the same run without approval stops at the gate ----
    _repo2 = tmp_path / "unapproved"
    _repo2.mkdir()
    _repo(_repo2)
    denied = Supervisor("csv-a33-denied", _repo2).run(
        "Add CSV export functionality and update its tests.", approved=False,
        fabric=_fabric(ScriptedLoop(CSV_PAYLOAD)),
        policy=_platform_policy(), approval_store=ApprovalStore(),
        audit_log=AuditLog())
    assert not denied["accepted"]
    assert denied["stages"] == ["PLAN", "AGENTS", "MODEL", "ROLLBACK"]
    assert (_repo2 / "app.py").read_text().startswith("def rows()")
    assert not (_repo2 / "tests" / "test_csv.py").exists()


def test_e2e_rollback_when_late_stage_fails(tmp_path):
    _repo(tmp_path)
    outcome = Supervisor("csv-a33-review", tmp_path).run(
        "Add CSV export functionality and update its tests.", approved=True,
        fabric=_fabric(ScriptedLoop(CSV_PAYLOAD, verdict="CHANGES_REQUESTED")),
        policy=_platform_policy(), approval_store=ApprovalStore(),
        audit_log=AuditLog())
    assert not outcome["accepted"]
    assert outcome["rollback"] is True
    assert "ROLLBACK" in outcome["stages"]
    assert (tmp_path / "app.py").read_text() == (
        "def rows():\n    return [{'n': 'Ada'}]\n")
    assert not (tmp_path / "tests" / "test_csv.py").exists()
    log = subprocess.run(["git", "log", "--oneline"], cwd=tmp_path, text=True,
                         capture_output=True).stdout
    assert log.count("initial") == 1


def test_mock_browser_and_desktop_e2e():
    store = ApprovalStore()
    audit = AuditLog()
    policy = PermissionPolicy(rules=[
        PermissionRule("site", Resource.BROWSER, "navigate", "ALLOW",
                       scope="example.com", reason="listed site"),
        PermissionRule("files", Resource.DESKTOP, "file_access", "ALLOW",
                       reason="listed mock files"),
        PermissionRule("keys", Resource.DESKTOP, "keyboard",
                       "REQUIRE_APPROVAL", reason="input needs approval"),
    ])
    browser = MockBrowser(policy, store=store, audit=audit)
    desktop = MockDesktop(policy, store=store, audit=audit)

    allowed_site = browser.navigate("https://example.com/", agent="BrowserAgent")
    assert allowed_site.allowed
    assert not browser.navigate("https://unauthorized.example/",
                                agent="BrowserAgent").allowed

    mock_file = DesktopActionRequest(
        DesktopAction.FILE_ACCESS, DesktopResource("file", "mock-doc.txt"),
        agent="DesktopAgent")
    assert desktop.perform(mock_file).allowed
    protected = DesktopActionRequest(
        DesktopAction.KEYBOARD, DesktopResource("application", "terminal"),
        agent="DesktopAgent")
    gated = desktop.perform(protected)
    assert not gated.allowed and gated.approval_required

    resources = {event.resource for event in audit.events}
    assert {"browser", "desktop"} <= resources
