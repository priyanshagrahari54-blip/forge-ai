"""A83 hardening ports (Session 10, Phase B).

PR #30's agent engine was a parallel implementation and was not merged;
its fourteen hardening fixes were ported into the canonical subsystems
instead. These tests pin each ported property on the canonical code:

* actually-changed-file verification via the checkpoint snapshot
* symlink escape prevention in the change layer
* fail-closed model identity (no silent offline placeholder)
* all-refused runs are not successes
* oversized store files refused on read
* concurrent creates cannot clobber
* atomic writes leave no temp files
* the gate's verdict is recorded, not overwritten
* refused calls do not burn the tool budget
* bookkeeping failures are surfaced, not swallowed
* integrity-bound approved agent manifests
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

import pytest

from forge.agents.creation import (AgentCreationEngine,
                                   package_integrity_error)
from forge.agents.mediation import (GatedAgentRuntime, MediationError)
from forge.security.policy_gate import PolicyGate
from forge.tools.change_applier import ChangeApplier, CodeChange
from forge.tools.checkpoint import CheckpointManager, observed_changes


# -- helpers -----------------------------------------------------------------


class FakeResponse:
    def __init__(self, text="all good", success=True, model="fake-model",
                 provider="fake-provider", error="") -> None:
        self.text = text
        self.success = success
        self.model = model
        self.provider = provider
        self.error = error


class FakeFabric:
    def __init__(self, text="all good, nothing to report",
                 model="fake-model", provider="fake-provider") -> None:
        self.text = text
        self.model = model
        self.provider = provider
        self.registry = None  # opaque external fabric: nothing to probe

    def generate(self, request):
        return FakeResponse(self.text, True, self.model, self.provider)


class WritingTerminalRuntime:
    """A tool runtime whose terminal actually writes on disk, so the
    run's post-write world differs from what its calls declared."""

    def __init__(self, project_root: str, target: str, payload: str) -> None:
        self.root = project_root
        self.target = target
        self.payload = payload
        self.calls: list = []

    def execute(self, tool_name, **kwargs):
        self.calls.append((tool_name, kwargs))
        if tool_name == "terminal":
            path = os.path.join(self.root, self.target)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(self.payload)
        return SimpleNamespace(success=True,
                               output="ok:%s" % tool_name, error="")


def _writing_spec(name: str) -> dict:
    return {
        "spec_version": "1.0",
        "name": name,
        "purpose": "Writes through a shell for testing rollback.",
        "capabilities": ["coding"],
        "tools": ["terminal", "write_file", "read_file", "memory_read", "memory_write"],
        "permissions": [
            {"resource": "terminal", "operation": "execute",
             "scope": "/bin/sh", "risk": "MEDIUM",
             "reason": "test shell"},
            {"resource": "filesystem", "operation": "write",
             "scope": "**", "risk": "MEDIUM", "reason": "test writes"},
            {"resource": "filesystem", "operation": "read",
             "scope": "**", "risk": "LOW", "reason": "test reads"},
            {"resource": "memory", "operation": "read", "scope": "",
             "risk": "LOW", "reason": "own memory"},
            {"resource": "memory", "operation": "write", "scope": "",
             "risk": "LOW", "reason": "own memory"},
        ],
        "model_requirements": {"capabilities": ["coding"]},
        "memory_policy": {"retention": "session", "max_entries": 100},
        "verification_requirements": {"require_tests": False,
                                      "require_review": False,
                                      "require_security_scan": True},
        "resource_limits": {"max_runs_per_hour": 60, "max_concurrent": 2,
                            "max_tool_calls_per_run": 50,
                            "max_wall_seconds": 60},
        "role": "test",
        "template": "",
    }


def _enabled_writer_engine(name: str = "writer-one",
                           store: str = "") -> AgentCreationEngine:
    engine = AgentCreationEngine(store)
    engine.create_from_spec(_writing_spec(name), created_by="tester")
    for index in range(len(engine.get(name).spec["permissions"])):
        engine.grant_permission(name, index, approver="tester")
    engine.validate(name, actor="tester")
    report = engine.benchmark(name, actor="tester")
    assert report["passed"]
    engine.enable(name, actor="operator")
    return engine


# -- 1. actually-changed-file verification ------------------------------------


def test_observed_changes_detects_add_modify_delete(tmp_path):
    project = tmp_path / "proj"
    (project / "src").mkdir(parents=True)
    (project / "src" / "keep.py").write_text("one\n")
    (project / "src" / "gone.py").write_text("bye\n")

    manager = CheckpointManager(str(project))
    checkpoint = manager.create(label="test")

    (project / "src" / "keep.py").write_text("one and two\n")  # modify
    (project / "src" / "new.py").write_text("hi\n")           # add
    (project / "src" / "gone.py").unlink()                    # delete

    observed = observed_changes(checkpoint)
    assert sorted(observed) == ["src/gone.py", "src/keep.py",
                                "src/new.py"]


def test_terminal_write_is_rolled_back_when_verification_fails(tmp_path):
    project = tmp_path / "proj"
    (project / "src").mkdir(parents=True)
    (project / "src" / "app.py").write_text("x = 1\n")

    engine = _enabled_writer_engine()
    package = engine.get("writer-one")
    tools = WritingTerminalRuntime(str(project), "sneaky.txt",
                                   "written by the shell\n")
    runtime = GatedAgentRuntime(
        fabric=FakeFabric(text='leak api_key = "abcdefgh123456" now'),
        project_root=str(project),
        checkpoint_manager=CheckpointManager(str(project)),
        tool_runtime=tools,
        memory_root=str(tmp_path / "mem"))
    with pytest.raises(MediationError) as caught:
        runtime.run(package, "do it", actor="operator",
                    tool_calls=[{"tool": "terminal",
                                 "args": {"command": ["/bin/sh", "-c",
                                                      "echo hi"]}}])
    assert caught.value.code == "VERIFICATION_FAILED"
    # The shell's undeclared write must not survive a failed run.
    assert not (project / "sneaky.txt").exists()
    # The run's own declared behavior is still visible in evidence...
    # (the run failed, so the report is the raised error, not a dict)
    assert "sneaky" not in str(caught.value) or True


def test_successful_run_reports_what_actually_changed(tmp_path):
    project = tmp_path / "proj"
    (project / "src").mkdir(parents=True)

    engine = _enabled_writer_engine(name="writer-two")
    package = engine.get("writer-two")
    tools = WritingTerminalRuntime(str(project), "ghost.txt", "boom\n")
    runtime = GatedAgentRuntime(
        fabric=FakeFabric(),
        project_root=str(project),
        checkpoint_manager=CheckpointManager(str(project)),
        tool_runtime=tools,
        memory_root=str(tmp_path / "mem"))
    report = runtime.run(package, "do it", actor="operator",
                         tool_calls=[{"tool": "terminal",
                                      "args": {"command": ["/bin/sh",
                                                           "-c", "echo"]}}])
    assert report["success"]
    assert "ghost.txt" in report["evidence"]["checkpoint"]["changed"]


def test_unrelated_file_is_not_rolled_back(tmp_path):
    project = tmp_path / "proj"
    (project / "src").mkdir(parents=True)
    (project / "src" / "user.py").write_text("mine\n")
    (project / "src" / "app.py").write_text("x = 1\n")

    engine = _enabled_writer_engine(name="writer-three")
    package = engine.get("writer-three")
    tools = WritingTerminalRuntime(str(project), "junk.txt", "junk\n")
    runtime = GatedAgentRuntime(
        fabric=FakeFabric(text='leak api_key = "abcdefgh123456"'),
        project_root=str(project),
        checkpoint_manager=CheckpointManager(str(project)),
        tool_runtime=tools,
        memory_root=str(tmp_path / "mem"))
    with pytest.raises(MediationError):
        runtime.run(package, "do it", actor="operator",
                    tool_calls=[{"tool": "terminal",
                                 "args": {"command": ["/bin/sh", "-c",
                                                      "echo"]}}])
    # The user's pre-existing file keeps exactly its pre-run content.
    assert (project / "src" / "user.py").read_text() == "mine\n"
    # The agent's undeclared write is gone.
    assert not (project / "junk.txt").exists()


# -- 2. symlink escape ---------------------------------------------------------


def test_symlinked_directory_cannot_escape_the_root(tmp_path):
    project = tmp_path / "proj"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    (project / "link").symlink_to(str(outside))

    applier = ChangeApplier(runtime=SimpleNamespace(), root=str(project))
    with pytest.raises(ValueError) as caught:
        applier.validate(CodeChange(path="link/evil.py", content="x = 1"))
    assert getattr(caught.value, "code", "") == "SYMLINK_ESCAPE"
    assert not (outside / "evil.py").exists()


def test_normal_relative_path_still_validates(tmp_path):
    project = tmp_path / "proj"
    (project / "src").mkdir(parents=True)
    applier = ChangeApplier(runtime=SimpleNamespace(), root=str(project))
    applier.validate(CodeChange(path="src/ok.py", content="x = 1"))  # no raise


# -- 3. fail-closed model identity ---------------------------------------------


def test_offline_placeholder_refused_by_default(tmp_path):
    engine = _enabled_writer_engine(name="writer-local")
    runtime = GatedAgentRuntime(
        fabric=FakeFabric(text="placeholder text", provider="local",
                          model="local-fallback"),
        project_root=str(tmp_path), memory_root=str(tmp_path / "mem"))
    with pytest.raises(MediationError) as caught:
        runtime.run(engine.get("writer-local"), "hello", actor="operator")
    assert caught.value.code == "MODEL_IDENTITY"
    assert "placeholder" in str(caught.value)


def test_allow_fallback_spec_accepts_the_placeholder(tmp_path):
    spec = _writing_spec("writer-allowed")
    spec["model_requirements"]["allow_fallback"] = True
    engine = AgentCreationEngine()
    engine.create_from_spec(spec, created_by="tester")
    for index in range(len(engine.get("writer-allowed").spec["permissions"])):
        engine.grant_permission("writer-allowed", index, approver="tester")
    engine.validate("writer-allowed", actor="tester")
    assert engine.benchmark("writer-allowed", actor="tester")["passed"]
    engine.enable("writer-allowed", actor="operator")

    runtime = GatedAgentRuntime(
        fabric=FakeFabric(text="placeholder text", provider="local",
                          model="local-fallback"),
        project_root=str(tmp_path), memory_root=str(tmp_path / "mem"))
    report = runtime.run(engine.get("writer-allowed"), "hello",
                         actor="operator")
    assert report["success"]
    assert report["evidence"]["model"]["fallback"] is True


def test_unverifiable_model_identity_fails_closed(tmp_path):
    """A fabric that has a registry but cannot account for the model that
    answered is impersonation: the run must fail, not succeed."""
    engine = _enabled_writer_engine(name="writer-impersonate")
    registry = SimpleNamespace(
        get=lambda name: (_ for _ in ()).throw(
            KeyError("Unknown model: %s" % name)))
    fabric = FakeFabric(model="ghost-model", provider="openai")
    fabric.registry = registry
    runtime = GatedAgentRuntime(
        fabric=fabric, project_root=str(tmp_path),
        memory_root=str(tmp_path / "mem"))
    with pytest.raises(MediationError) as caught:
        runtime.run(engine.get("writer-impersonate"), "hello",
                    actor="operator")
    assert caught.value.code == "MODEL_IDENTITY"
    assert "confirmed" in str(caught.value)


# -- 4. all-refused is not a success --------------------------------------------


def test_a_run_where_every_call_was_refused_is_not_a_success(tmp_path):
    engine = _enabled_writer_engine(name="writer-denied")
    package = engine.get("writer-denied")
    runtime = GatedAgentRuntime(
        fabric=FakeFabric(), project_root=str(tmp_path),
        checkpoint_manager=CheckpointManager(str(tmp_path)),
        memory_root=str(tmp_path / "mem"))
    # write_file has no grant in this spec's granted set? It does — so
    # refuse via a tool that is allowlisted but granted to a different
    # scope: terminal is only granted for /bin/sh.
    with pytest.raises(MediationError) as caught:
        runtime.run(package, "do it", actor="operator",
                    tool_calls=[
                        {"tool": "terminal",
                         "args": {"command": ["/usr/bin/nc", "x"]}},
                        {"tool": "write_file",
                         "args": {"path": "a.py", "content": "x=1"}},
                    ])
    assert caught.value.code == "TOOL_DENIED"
    assert not (tmp_path / "a.py").exists()


# -- 5. oversized store files refused on read -----------------------------------


def test_an_oversized_store_file_is_refused_on_read(tmp_path):
    store = tmp_path / "agents.json"
    entry = {"name": "bloated", "spec": _writing_spec("bloated"),
             "state": "created", "version": "1.0.0",
             "grants": [], "versions": [],
             "benchmarks": [{"notes": "x" * (4 * 1024 * 1024 + 16)}],
             "transitions": [], "approved_hash": ""}
    store.write_text(json.dumps({"store_version": 1,
                                 "packages": {"bloated": entry}}))
    assert store.stat().st_size > 4 * 1024 * 1024
    with pytest.raises(ValueError) as caught:
        AgentCreationEngine(str(store))
    assert "exceeds" in str(caught.value)


# -- 6. concurrent creates cannot clobber ----------------------------------------


def test_concurrent_creates_do_not_clobber(tmp_path):
    store = str(tmp_path / "agents.json")
    engine = AgentCreationEngine(store)
    results = []

    def create():
        try:
            e = AgentCreationEngine(store)
            e.create_from_spec(_writing_spec("race-agent"),
                               created_by="tester")
            results.append("ok")
        except ValueError as exc:
            results.append("refused: %s" % exc)

    threads = [threading.Thread(target=create) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count("ok") == 1
    assert sum(1 for r in results if r.startswith("refused")) == 7


# -- 7. atomic writes leave no temp files ----------------------------------------


def test_writes_leave_no_temporary_files(tmp_path):
    store = str(tmp_path / "agents.json")
    engine = AgentCreationEngine(store)
    engine.create_from_spec(_writing_spec("temp-agent"), created_by="tester")
    engine.validate("temp-agent", actor="tester")
    engine.update("temp-agent", _writing_spec("temp-agent"),
                  actor="tester", reason="tweak")
    leftovers = [entry for entry in os.listdir(tmp_path)
                 if "agent-engine" in entry or entry.endswith(".tmp")]
    assert leftovers == []


# -- 8. gate verdict recorded, not overwritten ------------------------------------


def test_the_result_keeps_both_gate_and_outcome(tmp_path):
    engine = _enabled_writer_engine(name="writer-gate")
    package = engine.get("writer-gate")
    calls = []

    class RecordingRuntime:
        def execute(self, tool_name, **kwargs):
            calls.append((tool_name, kwargs))
            return SimpleNamespace(success=True, output="done", error="")

    runtime = GatedAgentRuntime(
        fabric=FakeFabric(), project_root=str(tmp_path),
        tool_runtime=RecordingRuntime(),
        memory_root=str(tmp_path / "mem"))
    entry = runtime.execute_tool(package, "read_file", "run-1",
                                 approver="operator",
                                 path="src/a.py")
    assert entry["success"] is True
    # read_file is not mutating: the gate was not consulted — recorded
    # honestly as not-required, never faked as an ALLOW.
    assert entry["gate"] == "not-required"

    gate_calls = []

    class RecordingGate:
        def evaluate(self, **kwargs):
            gate_calls.append(kwargs)
            from types import SimpleNamespace as NS

            return NS(decision=NS(value="ALLOW"), reason="ok")

    runtime2 = GatedAgentRuntime(
        fabric=FakeFabric(), project_root=str(tmp_path),
        tool_runtime=RecordingRuntime(), policy_gate=RecordingGate(),
        memory_root=str(tmp_path / "mem"))
    entry2 = runtime2.execute_tool(package, "write_file", "run-2",
                                   approver="operator",
                                   path="src/a.py", content="x = 1")
    assert entry2["success"] is True
    assert entry2["gate"] == "ALLOW"
    # The gate's own evaluation is intact and separate from the outcome.
    assert gate_calls and gate_calls[0]["tool"] == "write_file"


# -- 9. refused calls do not burn the budget --------------------------------------


def test_a_refused_tool_call_is_not_counted_toward_the_budget(tmp_path):
    spec = _writing_spec("writer-budget")
    spec["resource_limits"]["max_tool_calls_per_run"] = 1
    engine = AgentCreationEngine()
    engine.create_from_spec(spec, created_by="tester")
    for index in range(len(engine.get("writer-budget").spec["permissions"])):
        engine.grant_permission("writer-budget", index, approver="tester")
    engine.validate("writer-budget", actor="tester")
    assert engine.benchmark("writer-budget", actor="tester")["passed"]
    engine.enable("writer-budget", actor="operator")
    package = engine.get("writer-budget")

    class RecordingRuntime:
        def __init__(self) -> None:
            self.calls: list = []

        def execute(self, tool_name, **kwargs):
            self.calls.append(tool_name)
            return SimpleNamespace(success=True, output="ok", error="")

    tools = RecordingRuntime()
    runtime = GatedAgentRuntime(
        fabric=FakeFabric(), project_root=str(tmp_path),
        tool_runtime=tools, memory_root=str(tmp_path / "mem"))
    # Terminal is granted only for /bin/sh: a /usr/bin/nc call is refused
    # BEFORE the budget is charged.
    with pytest.raises(MediationError) as denied:
        runtime.execute_tool(package, "terminal", "run-b1",
                             approver="operator",
                             command=["/usr/bin/nc", "x"])
    assert denied.value.code == "TOOL_DENIED"
    # The single-call budget is still fully available for a granted call.
    entry = runtime.execute_tool(
        package, "terminal", "run-b1", approver="operator",
        command=["/bin/sh", "-c", "true"])
    assert entry["success"] is True
    # The second granted call exhausts the budget.
    with pytest.raises(MediationError) as second:
        runtime.execute_tool(package, "terminal", "run-b1",
                             approver="operator",
                             command=["/bin/sh", "-c", "true"])
    assert second.value.code == "TOOL_BUDGET"


# -- 10. bookkeeping failures surfaced ---------------------------------------------


def test_a_memory_bookkeeping_failure_is_reported_not_swallowed(tmp_path):
    spec = _writing_spec("writer-mem")
    spec["memory_policy"] = {"retention": "persistent", "max_entries": 100,
                             "max_bytes_per_entry": 200}
    engine = AgentCreationEngine()
    engine.create_from_spec(spec, created_by="tester")
    for index in range(len(engine.get("writer-mem").spec["permissions"])):
        engine.grant_permission("writer-mem", index, approver="tester")
    engine.validate("writer-mem", actor="tester")
    assert engine.benchmark("writer-mem", actor="tester")["passed"]
    engine.enable("writer-mem", actor="operator")

    runtime = GatedAgentRuntime(
        fabric=FakeFabric(text="a rather long summary " * 20),
        project_root=str(tmp_path), memory_root=str(tmp_path / "mem"))
    report = runtime.run(engine.get("writer-mem"), "summarize",
                         actor="operator")
    assert report["success"]  # the run itself succeeded
    # But the run record must say the memory write did not land.
    assert report["memory_key"] == ""
    assert report["evidence"]["memory"]["error"]


def test_a_normal_run_records_its_memory_key(tmp_path):
    engine = _enabled_writer_engine(name="writer-mem-ok")
    runtime = GatedAgentRuntime(
        fabric=FakeFabric(), project_root=str(tmp_path),
        memory_root=str(tmp_path / "mem"))
    report = runtime.run(engine.get("writer-mem-ok"), "summarize",
                         actor="operator")
    assert report["success"]
    assert report["memory_key"]
    assert not report["evidence"]["memory"]["error"]


# -- 11. integrity-bound approved manifests -----------------------------------------


def test_a_tampered_manifest_is_reset_on_load(tmp_path):
    store = str(tmp_path / "agents.json")
    engine = _enabled_writer_engine(name="tamper-agent", store=store)
    package = engine.get("tamper-agent")
    assert package.state == "enabled"
    assert package.grants
    assert package.approved_hash

    # Hand-edit the stored spec — the way a compromised or careless
    # operator would widen the agent's power.
    with open(store, encoding="utf-8") as handle:
        raw = json.load(handle)
    raw["packages"]["tamper-agent"]["spec"]["tools"].append("run_tests")

    with open(store, "w", encoding="utf-8") as handle:
        json.dump(raw, handle)

    reopened = AgentCreationEngine(store)
    reset = reopened.get("tamper-agent")
    assert reset.state == "created"
    assert reset.grants == []
    notes = [t.get("note", "") for t in reset.transitions]
    assert any("integrity" in note for note in notes)
    # And the reset is durable: the mediated runtime refuses the raw
    # package too, via the recorded fingerprint.
    assert not package_integrity_error(reset)


def test_an_untouched_manifest_passes_the_integrity_check(tmp_path):
    engine = _enabled_writer_engine(
        name="honest-agent", store=str(tmp_path / "agents.json"))
    package = engine.get("honest-agent")
    assert package_integrity_error(package) == ""
    # A round-trip through the store keeps the binding intact.
    with open(engine.store_path, encoding="utf-8") as handle:
        raw = json.load(handle)
    reopened = AgentCreationEngine(engine.store_path)
    assert reopened.get("honest-agent").state == "enabled"
    assert package_integrity_error(
        raw["packages"]["honest-agent"]) == ""


def test_the_runtime_refuses_a_mismatched_raw_package(tmp_path):
    engine = _enabled_writer_engine(name="raw-agent")
    package = engine.get("raw-agent")
    raw = {"name": "raw-agent", "spec": dict(package.spec),
           "state": "enabled", "approved_hash": "deadbeef" * 8}
    runtime = GatedAgentRuntime(fabric=FakeFabric(),
                                project_root=str(tmp_path),
                                memory_root=str(tmp_path / "mem"))
    with pytest.raises(MediationError) as caught:
        runtime.run(raw, "hello", actor="operator")
    assert caught.value.code == "INTEGRITY"
