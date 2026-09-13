"""A82 — hardening regressions.

Each test here pins one defect that was found by driving the real engine
and observing a wrong answer. They are written as the exploit first and
the guarantee second, so a future change that reopens any of them fails
loudly rather than quietly reporting success again.

The three that matter most are the first three: an agent writing through
``terminal`` used to escape verification entirely, a symlink inside the
project used to let a write land outside it, and an unverifiable model
response used to be accepted as a real model's.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from helpers_a82 import (  # noqa: E402
    AgentProvider,
    action_payload,
    make_agent_fabric,
    make_engine,
    make_project,
    ready_agent,
    write_action,
)

from forge.agents.engine import (  # noqa: E402
    AgentExistsError,
    AgentIsolationError,
    AgentPackageError,
    PackageStore,
)
from forge.models.fabric import ModelFabric  # noqa: E402
from forge.security.policy_gate import PolicyDecision  # noqa: E402


@pytest.fixture()
def project(tmp_path):
    return make_project(tmp_path / "demo")


@pytest.fixture()
def engine(project):
    return make_engine(project)


SECRET = 'api_key = "abcdefgh12345678"\n'


def _run(engine, name, task="add CSV export", **kwargs):
    kwargs.setdefault("actor", "alice")
    kwargs.setdefault("approved", True)
    return engine.run(name, task, **kwargs)


# -- the verification bypass --------------------------------------------


def test_a_write_through_terminal_is_verified_and_fails(engine, project):
    """A file written by a shell command must be scanned like any other.

    ``terminal`` takes a command, not a path, so the run record used to
    see no changed files at all: zero gates ran and the run was reported
    as a verified success with a secret sitting in the worktree.
    """
    ready_agent(engine, name="kern", template="os-development")
    assert "terminal" in engine.get("kern").spec.tool_names()
    payload = action_payload(summary="done", actions=[
        {"tool": "terminal",
         "args": {"command": ["sh", "-c",
                              "printf '%s' '%s' > leaked.py"
                              % (SECRET, SECRET)]}}])
    engine.runtime.fabric = make_agent_fabric(AgentProvider(payload))
    result = _run(engine, "kern")
    assert result["success"] is False
    assert any(gate["name"] == "security" and not gate["passed"]
               for gate in result["gates"]), \
        "the security gate must judge what the shell wrote"
    assert "leaked.py" in result["files_changed"]


def test_a_write_through_terminal_is_rolled_back(engine, project):
    """Rollback must cover observed changes, not just declared paths."""
    ready_agent(engine, name="kern", template="os-development")
    payload = action_payload(summary="done", actions=[
        {"tool": "terminal",
         "args": {"command": ["sh", "-c",
                              "printf '%s' '%s' > leaked.py"
                              % (SECRET, SECRET)]}}])
    engine.runtime.fabric = make_agent_fabric(AgentProvider(payload))
    result = _run(engine, "kern")
    assert result["success"] is False
    assert result["rolled_back"] is True
    assert not (Path(project) / "leaked.py").exists(), \
        "a file the shell created must be removed on rollback"


def test_an_unrelated_file_is_not_rolled_back(engine, project):
    """Rollback is scoped: the operator's own work is never discarded."""
    ready_agent(engine, name="kern", template="os-development")
    mine = Path(project) / "mine.py"
    mine.write_text("untouched = True\n", encoding="utf-8")
    payload = action_payload(summary="done", actions=[
        {"tool": "terminal",
         "args": {"command": ["sh", "-c",
                              "printf '%s' '%s' > leaked.py"
                              % (SECRET, SECRET)]}}])
    engine.runtime.fabric = make_agent_fabric(AgentProvider(payload))
    result = _run(engine, "kern")
    assert result["success"] is False
    assert mine.read_text(encoding="utf-8") == "untouched = True\n"


# -- path confinement ---------------------------------------------------


def test_a_symlinked_directory_cannot_escape_the_project_root(engine,
                                                             project):
    """Lexical path checks cannot see symlinks; resolving them can."""
    ready_agent(engine)
    outside = Path(project).parent / "outside"
    outside.mkdir()
    link = Path(project) / "src"
    if link.exists():
        link.rmdir()
    link.symlink_to(outside, target_is_directory=True)
    payload = action_payload(summary="escape", actions=[
        write_action("src/escaped.py", "pwned = True\n")])
    engine.runtime.fabric = make_agent_fabric(AgentProvider(payload))
    result = _run(engine, "worker")
    assert result["success"] is False
    assert not (outside / "escaped.py").exists(), \
        "a write must never land outside the project root"
    assert any("outside the project root" in refusal["reason"]
               for refusal in result["refusals"]), result["refusals"]


# -- model identity -----------------------------------------------------


def test_an_unverifiable_model_response_fails_closed(engine, project,
                                                     monkeypatch):
    """If model identity cannot be confirmed, the run must not proceed.

    The readiness probe used to swallow its own exception and report "not
    a fallback", which let the offline placeholder satisfy a spec that
    forbids fallback models.
    """
    ready_agent(engine)
    assert engine.get("worker").spec.model.allow_fallback is False
    engine.runtime.fabric = ModelFabric.from_defaults()

    import forge.models.readiness as readiness

    def explode(*args, **kwargs):
        raise RuntimeError("readiness probe exploded")

    monkeypatch.setattr(readiness, "is_fallback_response", explode)
    result = _run(engine, "worker")
    assert result["success"] is False
    assert result["stage"] == "model"
    assert "could not be confirmed" in result["error"]


def test_a_real_fallback_is_still_refused_when_the_probe_works(engine,
                                                              project):
    """The honest path must keep working after the fail-closed change."""
    ready_agent(engine)
    engine.runtime.fabric = ModelFabric.from_defaults()
    result = _run(engine, "worker")
    assert result["success"] is False
    assert result["stage"] == "model"
    assert "allow_fallback=false" in result["error"]


# -- honest success -----------------------------------------------------


def test_a_run_where_every_action_was_refused_is_not_a_success(engine,
                                                              project):
    """An agent that asked for work and was refused did nothing."""
    ready_agent(engine)
    payload = action_payload(summary="done", actions=[
        {"tool": "delete_file", "args": {"path": "app.py"}}])
    engine.runtime.fabric = make_agent_fabric(AgentProvider(payload))
    result = _run(engine, "worker")
    assert result["success"] is False
    assert result["stage"] == "tools"
    assert "Every requested action was refused" in result["error"]


def test_a_run_with_no_requested_actions_can_still_succeed(engine, project):
    """Prose-only agents answer without acting; that is not a refusal."""
    ready_agent(engine, template="research")
    engine.runtime.fabric = make_agent_fabric(
        AgentProvider(action_payload(summary="here is the research")))
    result = _run(engine, "worker")
    assert result["success"] is True
    assert result["output"] == "here is the research"


# -- package store ------------------------------------------------------


def test_history_limit_zero_returns_nothing(project):
    """``runs[-0:]`` is the whole list, so zero must be handled explicitly."""
    store = PackageStore(project)
    directory = store.agents_dir / "probe"
    directory.mkdir(parents=True)
    for index in range(5):
        store.append_history("probe", {"n": index})
    assert len(store.history("probe", 5)) == 5
    assert store.history("probe", 0) == []
    assert store.history("probe", -1) == []


def test_an_oversized_package_file_is_refused_on_read(project):
    """The write path bounds size; the read path must bound it too."""
    from forge.agents.engine.package import MAX_FILE_BYTES

    store = PackageStore(project)
    directory = store.agents_dir / "probe"
    directory.mkdir(parents=True)
    store.write_grants("probe", {"grants": [], "revocations": []})
    payload = json.dumps({"runs": [{"pad": "x" * 64}
                                   for _ in range(MAX_FILE_BYTES // 8)]})
    (directory / "history.json").write_text(payload, encoding="utf-8")
    assert (directory / "history.json").stat().st_size > MAX_FILE_BYTES
    with pytest.raises(AgentPackageError) as caught:
        store.history("probe", 5)
    assert "exceeds" in str(caught.value)


def test_benchmarks_are_returned_newest_first(project):
    """Run ids are random hex, so filename order is not recency."""
    store = PackageStore(project)
    directory = store.agents_dir / "probe" / "benchmarks"
    directory.mkdir(parents=True)
    for index, run_id in enumerate(["ffff", "0000", "8888"]):
        store.record_benchmark("probe", {
            "run_id": run_id, "recorded_at": 1000.0 + index,
            "passed": True})
    ordered = [report["run_id"] for report in store.benchmarks("probe")]
    assert ordered == ["8888", "0000", "ffff"]
    assert store.benchmarks("probe", limit=0) == []


def test_a_backslash_run_id_is_refused(project):
    store = PackageStore(project)
    (store.agents_dir / "probe").mkdir(parents=True)
    with pytest.raises(AgentPackageError):
        store.record_benchmark("probe", {"run_id": "sub\\dir", "ok": True})


def test_remove_reports_failure_when_the_directory_survives(project,
                                                            monkeypatch):
    """A delete that fails part way must not report success."""
    import forge.agents.engine.package as package_module

    store = PackageStore(project)
    (store.agents_dir / "probe").mkdir(parents=True)
    monkeypatch.setattr(package_module.shutil, "rmtree",
                        lambda *a, **k: None)
    with pytest.raises(AgentPackageError) as caught:
        store.remove("probe")
    assert "still exists" in str(caught.value)


def test_a_second_create_of_the_same_agent_is_refused(project):
    """Creation is exclusive, so a race cannot clobber a package."""
    from forge.agents.engine.templates import spec_from_template

    store = PackageStore(project)
    spec = spec_from_template("coding", name="worker")
    store.create(spec, actor="alice")
    with pytest.raises(AgentExistsError):
        store.create(spec, actor="bob")
    # The original package is intact.
    assert store.load("worker").created_by == "alice"


def test_a_failed_create_leaves_no_partial_package(project, monkeypatch):
    """Either the package fully exists or it does not exist at all."""
    from forge.agents.engine.templates import spec_from_template

    store = PackageStore(project)
    spec = spec_from_template("coding", name="worker")

    def explode(*args, **kwargs):
        raise AgentPackageError("version store unavailable")

    monkeypatch.setattr(store, "record_version", explode)
    with pytest.raises(AgentPackageError):
        store.create(spec, actor="alice")
    assert not store.exists("worker")
    assert not (store.agents_dir / "worker").exists()


def test_writes_leave_no_temporary_files_behind(project):
    from forge.agents.engine.templates import spec_from_template

    store = PackageStore(project)
    spec = spec_from_template("coding", name="worker")
    package = store.create(spec, actor="alice")
    store.write_manifest(package)
    store.append_history("worker", {"run_id": "x"})
    leftovers = [path.name for path in store.directory_for("worker").rglob("*")
                 if path.name.endswith(".tmp")]
    assert leftovers == []


# -- manifest integrity -------------------------------------------------


def test_a_tampered_manifest_is_refused_at_run_time(engine, project):
    """The spec is what the operator approved; an edit voids the run."""
    ready_agent(engine)
    manifest = store_manifest(project, "worker")
    manifest["spec"]["permissions"]["operations"].append("delete_file")
    manifest_path = Path(project) / ".forge/agents/worker/agent.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    result = _run(engine, "worker")
    assert result["success"] is False
    assert result["stage"] == "integrity"
    assert "fingerprint" in result["error"]


def test_an_untouched_manifest_passes_the_integrity_check(engine, project):
    ready_agent(engine)
    assert engine.get("worker").integrity_error() == ""


def store_manifest(project, name):
    path = Path(project) / ".forge/agents" / name / "agent.json"
    return json.loads(path.read_text(encoding="utf-8"))


# -- accounting ---------------------------------------------------------


def test_a_refused_tool_call_is_not_counted_as_a_tool_call(engine, project):
    """Budget counters describe work done, not work refused."""
    ready_agent(engine)
    sandbox = engine.runtime.sandbox(engine.get("worker"), actor="alice",
                                     approved=True)
    outcome = sandbox.use_tool("delete_file", path="app.py")
    assert outcome["allowed"] is False
    assert sandbox._budget.tool_calls == 0
    assert sandbox._budget.file_writes == 0


def test_the_action_record_keeps_both_the_gate_and_the_outcome(engine,
                                                               project):
    """The gate can allow a call the handler then fails; show both."""
    ready_agent(engine)
    payload = action_payload(summary="done", actions=[
        write_action("export.py", "def export():\n    return 1\n")])
    engine.runtime.fabric = make_agent_fabric(AgentProvider(payload))
    result = _run(engine, "worker")
    written = [action for action in result["actions"]
               if action["tool"] == "write_file"]
    assert written, result["actions"]
    assert written[0]["gate"] == PolicyDecision.ALLOW.value
    assert written[0]["decision"] == PolicyDecision.ALLOW.value


def test_a_bookkeeping_failure_is_reported_not_swallowed(engine, project,
                                                         monkeypatch):
    """A run that cannot be recorded must say so, not look recorded."""
    ready_agent(engine)
    payload = action_payload(summary="nothing to change")
    engine.runtime.fabric = make_agent_fabric(AgentProvider(payload))

    def explode(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(engine.runtime.store, "append_history", explode)
    result = _run(engine, "worker")
    assert result["recorded"] is False
    assert any("not recorded" in note for note in result["notes"])


def test_a_normal_run_is_marked_as_recorded(engine, project):
    ready_agent(engine)
    payload = action_payload(summary="nothing to change")
    engine.runtime.fabric = make_agent_fabric(AgentProvider(payload))
    result = _run(engine, "worker")
    assert result["recorded"] is True
    assert engine.history("worker")[-1]["run_id"] == result["run_id"]


def test_a_permission_refusal_is_recorded_with_its_reason(engine, project):
    """Refusals are part of the audit trail, not a silent no-op."""
    ready_agent(engine)
    sandbox = engine.runtime.sandbox(engine.get("worker"), actor="alice",
                                     approved=True)
    with pytest.raises(AgentIsolationError):
        sandbox.recall("secret", owner="someone-else")
    assert sandbox.refusals
    assert sandbox.refusals[-1]["tool"] == "memory.recall"
