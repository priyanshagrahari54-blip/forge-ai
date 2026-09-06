from __future__ import annotations

from forge.agents.debugger import DebuggerAgent, TestDebugLoop
from forge.core.task_engine import Task, TaskStatus
from forge.runtime.runtime import ToolResult


def test_debug_loop_succeeds_first_try():
    task = Task(id="t1", description="Implement test")
    loop = TestDebugLoop(max_retries=3)

    res = loop.run(task, test_runner=lambda: ToolResult.ok("pytest", output="1 passed"))

    assert res.success
    assert res.final_state == "PASSED"
    assert len(res.attempts) == 1
    assert res.attempts[0].test_passed


def test_debug_loop_retries_and_succeeds():
    task = Task(id="t2", description="Fix bug")
    loop = TestDebugLoop(max_retries=3)

    call_count = 0

    def mock_test():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return ToolResult.fail("pytest", error="AssertionError: expected 5 got 4")
        return ToolResult.ok("pytest", output="1 passed")

    res = loop.run(task, test_runner=mock_test)

    assert res.success
    assert len(res.attempts) == 2
    assert not res.attempts[0].test_passed
    assert res.attempts[1].test_passed


def test_debug_loop_exhausts_retries_and_fails():
    task = Task(id="t3", description="Unfixable bug")
    loop = TestDebugLoop(max_retries=2)

    res = loop.run(
        task,
        test_runner=lambda: ToolResult.fail("pytest", error="SyntaxError: invalid syntax"),
    )

    assert not res.success
    assert res.final_state == "FAILED"
    assert task.status == TaskStatus.FAILED
    assert len(res.attempts) == 2
    assert "Exhausted" in res.error
