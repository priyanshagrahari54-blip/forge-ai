"""A81 security: isolation and permission boundaries.

These tests are the security contract of the Agent Creation Engine.
They drive a real :class:`BoundAgent` against a real ``PolicyGate``,
``ToolRuntime``, ``CheckpointManager``, and ``VerificationPipeline`` in
a temporary repository, and assert that an agent:

1. cannot grant itself anything,
2. cannot touch a path outside its declared scope,
3. cannot use a tool its package never declared,
4. cannot write without approval when its spec requires approval,
5. cannot read another agent's memory,
6. cannot store secrets,
7. cannot exceed its resource limits,
8. cannot run while not enabled,
9. cannot widen its envelope through a revision without a MAJOR bump
   that resets the lifecycle,
10. is restored exactly by its checkpoint when a run fails.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from forge.agent_engine.engine import AgentCreationEngine, EngineError
from forge.agent_engine.factory import AgentFactoryEngine
from forge.agent_engine.runtime import (
    AgentRuntimeError,
    BoundAgent,
    contains_secret,
    path_allowed,
    redact,
)
from forge.agent_engine.spec import AgentSpec
from forge.runtime.defaults import create_default_runtime
from forge.security.permissions import OperationMode, PermissionManager
from forge.security.policy_gate import PolicyGate
from forge.tools.checkpoint import CheckpointManager

WRITER_SPEC = {
    "name": "writer-agent",
    "purpose": "Write only inside its own sandbox directory.",
    "capabilities": ["coding"],
    "tools": ["read_file", "write_file"],
    "permissions": {"read_paths": ["sandbox/**"],
                    "write_paths": ["sandbox/**"],
                    "require_approval_for_writes": True},
    "resource_limits": {"max_files_touched": 2, "max_bytes_written": 64,
                        "max_runs_per_hour": 3, "max_concurrent_runs": 1},
}

READER_SPEC = {
    "name": "reader-agent",
    "purpose": "Read repository files and never write.",
    "capabilities": ["research"],
    "tools": ["read_file"],
    "permissions": {"read_paths": ["**"]},
    "model_requirements": {"capability": "research"},
}


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "sandbox").mkdir(parents=True)
    (root / "secrets").mkdir()
    (root / "sandbox" / "notes.txt").write_text("hello\n")
    (root / "secrets" / "key.txt").write_text("super secret\n")
    return root


def _wire(spec, repo, *, mode=OperationMode.AUTONOMOUS,
          state="enabled"):
    package = AgentFactoryEngine().build(spec)
    package.state = state
    manager = PermissionManager(mode=mode)
    return BoundAgent(
        package,
        policy_gate=PolicyGate(manager),
        tool_runtime=create_default_runtime(manager, str(repo)),
        checkpoints=CheckpointManager(repo),
    ), package


# -- 1. self-grant ------------------------------------------------------

def test_agent_cannot_grant_itself_permissions(repo):
    agent, _ = _wire(WRITER_SPEC, repo)
    with pytest.raises(AgentRuntimeError) as excinfo:
        agent.request_grant("write_file", "**")
    assert "cannot grant permissions to themselves" in str(excinfo.value)


def test_no_engine_api_grants_permissions():
    engine = AgentCreationEngine()
    for forbidden in ("grant", "grant_permission", "escalate",
                      "add_permission", "widen_scope"):
        assert not hasattr(engine, forbidden)


# -- 2. path scope ------------------------------------------------------

@pytest.mark.parametrize("path", [
    "secrets/key.txt", "../outside.txt", "/etc/passwd", ".git/config",
    ".forge/state.json", "sandbox/../secrets/key.txt",
])
def test_agent_cannot_touch_paths_outside_its_scope(repo, path):
    agent, _ = _wire(WRITER_SPEC, repo)
    decision = agent.authorize("write_file", path=path, approved=True)
    assert not decision["allowed"]
    with pytest.raises(AgentRuntimeError):
        agent.use_tool("write_file", path=path, approved=True,
                       content="x")
    assert not (repo / "outside.txt").exists()
    assert (repo / "secrets" / "key.txt").read_text() == "super secret\n"


def test_agent_writes_inside_its_scope(repo):
    agent, _ = _wire(WRITER_SPEC, repo)
    run = agent.begin_run()
    result = agent.use_tool("write_file", path="sandbox/out.txt",
                            approved=True, content="ok", run=run)
    assert result.success, result.error
    assert (repo / "sandbox" / "out.txt").read_text() == "ok"
    assert run.files_touched == ["sandbox/out.txt"]


def test_path_scope_helper_rejects_traversal_and_protected_paths():
    assert path_allowed("docs/index.md", ["docs/**"])
    assert path_allowed("docs/a/b.md", ["docs/**"])
    assert path_allowed("anything.py", ["**"])
    assert not path_allowed("../x", ["**"])
    assert not path_allowed(".git/config", ["**"])
    assert not path_allowed(".forge/state.json", ["**"])
    assert not path_allowed("/etc/passwd", ["**"])
    assert not path_allowed("src/x.py", ["docs/**"])


# -- 3. undeclared tools ------------------------------------------------

def test_agent_cannot_use_undeclared_tools(repo):
    agent, _ = _wire(READER_SPEC, repo)
    for tool in ("write_file", "terminal", "git_commit", "web_fetch"):
        decision = agent.authorize(tool, path="sandbox/notes.txt",
                                   approved=True)
        assert not decision["allowed"]
        assert "not part of this agent's package" in decision["reason"]
        with pytest.raises(AgentRuntimeError):
            agent.use_tool(tool, path="sandbox/notes.txt", approved=True,
                           content="nope")
    assert (repo / "sandbox" / "notes.txt").read_text() == "hello\n"


# -- 4. approvals -------------------------------------------------------

def test_writes_require_approval_when_the_spec_says_so(repo):
    agent, _ = _wire(WRITER_SPEC, repo)
    decision = agent.authorize("write_file", path="sandbox/x.txt")
    assert decision["decision"] == "REQUIRE_APPROVAL"
    with pytest.raises(AgentRuntimeError):
        agent.use_tool("write_file", path="sandbox/x.txt", content="x")
    assert not (repo / "sandbox" / "x.txt").exists()


def test_locked_mode_denies_even_approved_writes(repo):
    agent, _ = _wire(WRITER_SPEC, repo, mode=OperationMode.LOCKED)
    decision = agent.authorize("write_file", path="sandbox/x.txt",
                               approved=True)
    assert not decision["allowed"]
    assert decision["decision"] == "DENY"


def test_safe_mode_allows_reads_and_denies_writes(repo):
    agent, _ = _wire(WRITER_SPEC, repo, mode=OperationMode.SAFE)
    assert agent.authorize("read_file", path="sandbox/notes.txt")["allowed"]
    assert not agent.authorize("write_file", path="sandbox/x.txt",
                               approved=True)["allowed"]


# -- 5/6. memory isolation ----------------------------------------------

def test_agents_cannot_read_each_others_memory(repo):
    first, _ = _wire(dict(WRITER_SPEC, memory_policy={"scope": "session"}),
                     repo)
    second, _ = _wire(dict(READER_SPEC, memory_policy={"scope": "session"}),
                      repo)
    first.memory.save("plan", "step one")
    assert second.memory.load("plan") is None
    assert first.memory.namespace != second.memory.namespace
    assert first.memory.keys() == ["agents/writer-agent/plan"]


def test_memory_keys_cannot_escape_the_namespace(repo):
    agent, _ = _wire(dict(WRITER_SPEC, memory_policy={"scope": "session"}),
                     repo)
    for key in ("../other/plan", "a/b", "", "..\\win"):
        with pytest.raises(AgentRuntimeError):
            agent.memory.save(key, "x")


def test_memory_refuses_secrets_and_enforces_budgets(repo):
    agent, _ = _wire(dict(WRITER_SPEC,
                          memory_policy={"scope": "session",
                                         "max_entries": 1,
                                         "max_bytes": 16}), repo)
    with pytest.raises(AgentRuntimeError):
        agent.memory.save("leak", "api_key = sk-abcdefghij0123456789")
    agent.memory.save("ok", "short")
    with pytest.raises(AgentRuntimeError):
        agent.memory.save("second", "another")
    with pytest.raises(AgentRuntimeError):
        agent.memory.save("ok", "x" * 100)


def test_secret_detection_and_redaction():
    assert contains_secret("password: hunter2")
    assert contains_secret("-----BEGIN RSA PRIVATE KEY-----")
    assert not contains_secret("just some ordinary prose")
    assert "sk-" not in redact("token = sk-abcdefghijklmnop1234")


# -- 7. resource limits -------------------------------------------------

def test_file_and_byte_budgets_are_enforced_before_the_write(repo):
    agent, _ = _wire(WRITER_SPEC, repo)
    run = agent.begin_run()
    agent.use_tool("write_file", path="sandbox/a.txt", approved=True,
                   content="a", run=run)
    agent.use_tool("write_file", path="sandbox/b.txt", approved=True,
                   content="b", run=run)
    decision = agent.authorize("write_file", path="sandbox/c.txt",
                               approved=True, run=run)
    assert not decision["allowed"]
    assert "File budget" in decision["reason"]
    assert not (repo / "sandbox" / "c.txt").exists()

    with pytest.raises(AgentRuntimeError) as excinfo:
        agent.use_tool("write_file", path="sandbox/a.txt", approved=True,
                       content="x" * 200, run=run)
    assert "Write budget" in str(excinfo.value)


def test_run_limits_are_enforced(repo):
    agent, _ = _wire(WRITER_SPEC, repo)
    for _ in range(3):
        run = agent.begin_run()
        agent.end_run(run)
    with pytest.raises(AgentRuntimeError) as excinfo:
        agent.begin_run()
    assert "hourly run limit" in str(excinfo.value)


def test_concurrency_limit_is_enforced(repo):
    agent, _ = _wire(WRITER_SPEC, repo)
    agent.begin_run()
    with pytest.raises(AgentRuntimeError) as excinfo:
        agent.begin_run()
    assert "concurrency limit" in str(excinfo.value)


# -- 8. lifecycle gating ------------------------------------------------

@pytest.mark.parametrize("state", ["created", "validated", "tested",
                                   "paused", "disabled", "retired"])
def test_only_enabled_agents_may_act(repo, state):
    agent, _ = _wire(WRITER_SPEC, repo, state=state)
    with pytest.raises(AgentRuntimeError) as excinfo:
        agent.begin_run()
    assert state in str(excinfo.value)
    with pytest.raises(AgentRuntimeError):
        agent.use_tool("write_file", path="sandbox/x.txt", approved=True,
                       content="x")
    assert not (repo / "sandbox" / "x.txt").exists()


def test_engine_refuses_to_bind_a_non_enabled_agent():
    engine = AgentCreationEngine()
    package = engine.create(template="documentation")
    with pytest.raises(EngineError):
        engine.bind(package.name)


# -- 9. revisions cannot silently widen ---------------------------------

def test_widening_a_revision_forces_retesting():
    engine = AgentCreationEngine()
    package = engine.create(AgentSpec.from_dict(READER_SPEC))
    engine.validate(package.name)
    engine.test(package.name, include_behavioural=False)
    engine.enable(package.name)
    assert engine.lifecycle.can_run(package.name)

    engine.revise(package.name, {
        "tools": ["read_file", "web_fetch"],
        "permissions": dict(package.spec.permissions.to_dict(),
                            allow_network=True,
                            domains=["example.com"])})
    assert not engine.lifecycle.can_run(package.name)
    with pytest.raises(EngineError):
        engine.enable(package.name)


# -- 10. checkpoints ----------------------------------------------------

def test_failed_runs_restore_the_exact_pre_change_bytes(repo):
    agent, _ = _wire(WRITER_SPEC, repo)
    original = (repo / "sandbox" / "notes.txt").read_text()
    run = agent.begin_run()
    assert run.checkpoint_id
    agent.use_tool("write_file", path="sandbox/notes.txt", approved=True,
                   content="corrupted", run=run)
    assert (repo / "sandbox" / "notes.txt").read_text() == "corrupted"
    assert agent.rollback(run)
    assert (repo / "sandbox" / "notes.txt").read_text() == original
    agent.end_run(run, "failed")
    assert agent.runs()[-1]["status"] == "failed"


def test_rollback_leaves_unrelated_files_alone(repo):
    agent, _ = _wire(WRITER_SPEC, repo)
    run = agent.begin_run()
    agent.use_tool("write_file", path="sandbox/new.txt", approved=True,
                   content="x", run=run)
    (repo / "sandbox" / "unrelated.txt").write_text("mine\n")
    agent.rollback(run)
    assert (repo / "sandbox" / "unrelated.txt").read_text() == "mine\n"


# -- denials are recorded ------------------------------------------------

def test_every_denial_is_recorded_on_the_run(repo):
    agent, _ = _wire(WRITER_SPEC, repo)
    run = agent.begin_run()
    agent.authorize("write_file", path="secrets/key.txt", approved=True,
                    run=run)
    agent.authorize("terminal", path="", run=run)
    assert len(run.denials) == 2
    assert all(not item["allowed"] for item in run.denials)
