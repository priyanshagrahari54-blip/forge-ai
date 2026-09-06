from __future__ import annotations

import tempfile
from pathlib import Path
from forge.agents.coder import CoderAgent
from forge.agents.execution import AgentRequest
from forge.core.task_engine import Task, TaskStatus
from forge.runtime.defaults import create_default_runtime
from forge.security.permissions import PermissionManager


def test_coder_agent_file_writing_with_approval():
    with tempfile.TemporaryDirectory() as tmpdir:
        agent = CoderAgent(root=tmpdir)

        test_file = "test.txt"
        res = agent.write_file(test_file, "hello coder", approved=True)
        assert res.success
        assert (Path(tmpdir) / test_file).read_text() == "hello coder"


def test_coder_agent_file_writing_requires_approval():
    with tempfile.TemporaryDirectory() as tmpdir:
        agent = CoderAgent(root=tmpdir)

        test_file = "unapproved.txt"
        res = agent.write_file(test_file, "content", approved=False)
        assert not res.success
        assert "Approval required" in res.error
        assert not (Path(tmpdir) / test_file).exists()


def test_coder_agent_execute_request():
    with tempfile.TemporaryDirectory() as tmpdir:
        agent = CoderAgent(root=tmpdir)
        task = Task(id="t1", description="Add math feature")
        req = AgentRequest(
            task=task,
            stage=TaskStatus.CODING,
            metadata={
                "changes": {"math_lib.py": "def add(a, b): return a + b"},
                "approved": "true",
            },
        )

        resp = agent.execute(req)
        assert resp.success
        assert (Path(tmpdir) / "math_lib.py").read_text() == "def add(a, b): return a + b"
