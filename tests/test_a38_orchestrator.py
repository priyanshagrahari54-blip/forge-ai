"""Multi-agent orchestration engine tests (A38).

The orchestrator is real coordination infrastructure: validated
dependency DAGs, bounded parallel workers, per-step attempt budgets,
per-step timeouts, policy-gated dispatch (DENY fails closed,
REQUIRE_APPROVAL redeems single-use tokens), structured agent messages,
and cooperative cancellation.
"""
from __future__ import annotations

import threading
import time

import pytest

from forge.agents.execution import CallableAgentExecutor
from forge.agents.registry import AgentRegistration, AgentRegistry
from forge.core.orchestrator import (AgentMessage, MultiAgentOrchestrator,
                                     OrchestrationPlan, OrchestrationStep,
                                     ReportStatus, StepStatus)
from forge.core.run_control import SupervisorControl
from forge.security.approvals import ApprovalRequest, ApprovalStore
from forge.security.policy import (PermissionPolicy, PermissionRule,
                                   Resource)


def _registry(workers=None):
    registry = AgentRegistry()
    for name, role, capabilities in (
            ("coder", "coding", ("coding",)),
            ("tester", "testing", ("testing",)),
            ("debugger", "debugging", ("debugging",)),
            ("reviewer", "reviewing", ("review",)),
            ("researcher", "research", ("research",))):
        worker = (workers or {}).get(
            name, lambda request, _n=name: f"{_n} done")
        registry.register(AgentRegistration(
            name, role, CallableAgentExecutor(name, worker), capabilities))
    return registry


def _events():
    return []


def _run(plan, registry=None, **kwargs):
    events: list[tuple[str, dict]] = []
    orchestrator = MultiAgentOrchestrator(
        registry or _registry(), on_event=lambda n, d: events.append((n, d)),
        **kwargs)
    report = orchestrator.execute(plan)
    return report, events


def _plan(*steps):
    return OrchestrationPlan(requirement="demo requirement", steps=steps)


def test_build_plan_is_deterministic_and_ordered():
    registry = _registry()
    orchestrator = MultiAgentOrchestrator(registry)
    plan = orchestrator.build_plan("implement the code and run the tests")
    assert [step.agent for step in plan.steps] == ["coder", "tester"]
    assert [step.depends_on for step in plan.steps] == [(), ()]
    chained = orchestrator.build_plan(
        "implement the code and run the tests", chain=True)
    assert [step.agent for step in chained.steps] == ["coder", "tester"]
    assert chained.steps[1].depends_on == (chained.steps[0].id,)


def test_empty_plan_is_rejected_not_fabricated():
    orchestrator = MultiAgentOrchestrator(_registry())
    plan = orchestrator.build_plan("zzz qqq nothing matches")
    report, _ = _run(plan)
    assert report.status == ReportStatus.PLAN_REJECTED
    assert report.accepted is False
    assert "rejected" in report.summary


def test_plan_validation_rejects_malformed_plans():
    registry = _registry()
    orchestrator = MultiAgentOrchestrator(registry)
    with pytest.raises(ValueError):
        _plan(OrchestrationStep("s", "nobody", "r", "c", "do it")).validate(
            registry)
    with pytest.raises(ValueError):
        _plan(
            OrchestrationStep("s1", "coder", "coding", "coding", "do"),
            OrchestrationStep("s1", "tester", "testing", "testing", "do"),
        ).validate(registry)
    with pytest.raises(ValueError):
        _plan(OrchestrationStep(
            "s1", "coder", "coding", "coding", "do",
            depends_on=("missing",))).validate(registry)
    with pytest.raises(ValueError):
        _plan(
            OrchestrationStep("a", "coder", "coding", "coding", "do",
                              depends_on=("b",)),
            OrchestrationStep("b", "tester", "testing", "testing", "do",
                              depends_on=("a",)),
        ).validate(registry)


def test_dependency_order_and_failure_skip():
    order: list[str] = []
    lock = threading.Lock()
    workers = {
        "coder": lambda request: order.append("coder") or "ok",
        "tester": lambda request: order.append("tester") or "ok",
        "debugger": lambda request: order.append("debugger") or "ok",
    }
    registry = _registry(workers)
    plan = _plan(
        OrchestrationStep("a", "coder", "coding", "coding", "do"),
        OrchestrationStep("b", "tester", "testing", "testing", "do",
                          depends_on=("a",)),
        OrchestrationStep("c", "debugger", "debugging", "debugging", "do",
                          depends_on=("b",)),
    )
    report, _ = _run(plan, registry=registry)
    assert report.status == ReportStatus.SUCCEEDED
    assert order == ["coder", "tester", "debugger"]

    failing = _registry({
        "coder": lambda request: (_ for _ in ()).throw(
            RuntimeError("boom")),
        "tester": lambda request: "ok",
    })
    plan2 = _plan(
        OrchestrationStep("a", "coder", "coding", "coding", "do"),
        OrchestrationStep("b", "tester", "testing", "testing", "do",
                          depends_on=("a",)),
    )
    report2, _ = _run(plan2, registry=failing)
    assert report2.status == ReportStatus.FAILED
    by_id = {outcome.step_id: outcome for outcome in report2.outcomes}
    assert by_id["a"].status == StepStatus.FAILED
    assert by_id["b"].status == StepStatus.SKIPPED


def test_independent_steps_run_in_parallel():
    entered = {name: threading.Event() for name in ("coder", "tester")}

    def worker(request, name):
        entered[name].set()
        other = "tester" if name == "coder" else "coder"
        assert entered[other].wait(timeout=5), "steps did not overlap"
        return f"{name} done"

    registry = _registry({
        "coder": lambda request: worker(request, "coder"),
        "tester": lambda request: worker(request, "tester"),
    })
    plan = _plan(
        OrchestrationStep("a", "coder", "coding", "coding", "do"),
        OrchestrationStep("b", "tester", "testing", "testing", "do"),
    )
    report, _ = _run(plan, registry=registry, max_workers=2)
    assert report.status == ReportStatus.SUCCEEDED
    assert all(outcome.status == StepStatus.SUCCEEDED
               for outcome in report.outcomes)


def test_attempt_budget_and_retry():
    calls: list[int] = []

    def flaky(request):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("first attempt fails")
        return "recovered"

    registry = _registry({"coder": flaky})
    plan = _plan(OrchestrationStep(
        "a", "coder", "coding", "coding", "do"))
    report, _ = _run(plan, registry=registry, max_attempts=2)
    assert report.status == ReportStatus.SUCCEEDED
    assert report.outcomes[0].attempts == 2

    def always_fails(request):
        raise RuntimeError("always")

    failing = _registry({"coder": always_fails})
    report2, _ = _run(plan, registry=failing, max_attempts=2)
    assert report2.status == ReportStatus.FAILED
    assert report2.outcomes[0].attempts == 2
    assert "always" in report2.outcomes[0].error


def test_step_timeout_fails_closed():
    registry = _registry({"coder": lambda request: time.sleep(2)})
    plan = _plan(OrchestrationStep(
        "a", "coder", "coding", "coding", "do"))
    report, _ = _run(plan, registry=registry, step_timeout=0.2)
    assert report.status == ReportStatus.FAILED
    assert "timed out" in report.outcomes[0].error


def test_deny_policy_fails_closed():
    policy = PermissionPolicy(rules=[
        PermissionRule(id="deny-all-agents", resource=Resource.AGENT,
                       operation="execute", scope="", effect="DENY"),
    ])
    plan = _plan(OrchestrationStep(
        "a", "coder", "coding", "coding", "do"))
    report, _ = _run(plan, policy=policy)
    assert report.status == ReportStatus.FAILED
    assert report.outcomes[0].status == StepStatus.DENIED


def test_approval_round_trip_with_single_use_tokens():
    store = ApprovalStore()
    policy = PermissionPolicy(rules=[
        PermissionRule(id="agents-need-approval", resource=Resource.AGENT,
                       operation="execute", scope="",
                       effect="REQUIRE_APPROVAL"),
    ])
    approved: list[str] = []

    def operator(query):
        request = ApprovalRequest(
            agent="forge-orchestrator", resource=Resource.AGENT,
            operation="execute", scopes=(query.agent,),
            task_id=query.task_id, reason=query.reason)
        store.submit(request)
        if query.agent in approved:
            store.decide(request.id, True, "human-operator")
            return store.issue(request.id, "human-operator").id
        return ""

    plan = _plan(
        OrchestrationStep("a", "coder", "coding", "coding", "do"),
        OrchestrationStep("b", "tester", "testing", "testing", "do"),
    )
    report, _ = _run(plan, policy=policy, approval_store=store,
                     approval_callback=operator)
    assert report.status == ReportStatus.FAILED
    by_id = {outcome.step_id: outcome for outcome in report.outcomes}
    assert by_id["a"].status == StepStatus.DENIED
    assert "approval not granted" in by_id["a"].error

    approved.extend(["coder", "tester"])
    report2, _ = _run(plan, policy=policy, approval_store=store,
                      approval_callback=operator)
    assert report2.status == ReportStatus.SUCCEEDED
    assert all(outcome.status == StepStatus.SUCCEEDED
               for outcome in report2.outcomes)


def test_stale_token_never_dispaches_another_agent():
    store = ApprovalStore()
    policy = PermissionPolicy(rules=[
        PermissionRule(id="agents-need-approval", resource=Resource.AGENT,
                       operation="execute", scope="",
                       effect="REQUIRE_APPROVAL"),
    ])
    minted: dict[str, str] = {}

    def operator(query):
        request = ApprovalRequest(
            agent="forge-orchestrator", resource=Resource.AGENT,
            operation="execute", scopes=(query.agent,),
            task_id=query.task_id, reason=query.reason)
        store.submit(request)
        store.decide(request.id, True, "human-operator")
        token = store.issue(request.id, "human-operator")
        if query.agent == "coder":
            minted[query.agent] = token.id
            return token.id
        # Stale token: the coder's grant cannot cover another agent.
        return minted.get("coder", "")

    plan = _plan(
        OrchestrationStep("a", "coder", "coding", "coding", "do"),
        OrchestrationStep("b", "tester", "testing", "testing", "do"),
    )
    report, _ = _run(plan, policy=policy, approval_store=store,
                     approval_callback=operator)
    # The coder token cannot cover the tester dispatch (scope mismatch),
    # so the second step fails closed.
    assert report.status == ReportStatus.FAILED
    by_id = {outcome.step_id: outcome for outcome in report.outcomes}
    assert by_id["a"].status == StepStatus.SUCCEEDED
    assert by_id["b"].status == StepStatus.DENIED


def test_structured_messages_recorded():
    report, _ = _run(_plan(OrchestrationStep(
        "a", "coder", "coding", "coding", "do")))
    messages = report.outcomes[0].messages
    assert messages
    message = messages[0]
    assert isinstance(message, AgentMessage)
    assert message.sender == "coder"
    assert message.message_type == "result"
    assert message.evidence
    assert 0.0 <= message.confidence <= 1.0


def test_cancellation_before_start():
    control = SupervisorControl()
    control.request_cancel()
    plan = _plan(OrchestrationStep(
        "a", "coder", "coding", "coding", "do"))
    report, _ = _run(plan, control=control)
    assert report.status == ReportStatus.CANCELLED
    assert all(outcome.status == StepStatus.CANCELLED
               for outcome in report.outcomes)


def test_cancellation_during_execution():
    control = SupervisorControl()
    registry = _registry({"coder": lambda request: time.sleep(0.4)})
    plan = _plan(OrchestrationStep(
        "a", "coder", "coding", "coding", "do"))
    timer = threading.Timer(0.05, control.request_cancel)
    timer.start()
    report, _ = _run(plan, registry=registry, control=control)
    assert report.status == ReportStatus.CANCELLED


def test_events_stream_every_transition():
    plan = _plan(OrchestrationStep(
        "a", "coder", "coding", "coding", "do"))
    names: list[str] = []
    orchestrator = MultiAgentOrchestrator(
        _registry(), on_event=lambda n, d: names.append(n))
    orchestrator.execute(plan)
    assert names[0] == "orchestration_started"
    assert "step_started" in names
    assert "step_attempt" in names
    assert "step_finished" in names
    assert names[-1] == "orchestration_finished"
