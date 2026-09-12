"""Parallel task and multi-agent orchestration (A81).

A production-quality orchestration layer for Forge Server:

* :mod:`~forge.orchestration.roles` — the ten agent roles, each with
  capability, permissions, model requirement, task scope, resource
  limits, timeout, and a validated result schema;
* :mod:`~forge.orchestration.graph` — the dependency-aware task graph
  (sequential / parallel / dependent / blocked tasks);
* :mod:`~forge.orchestration.locking` — shared/exclusive file and
  resource locking with deadlock detection;
* :mod:`~forge.orchestration.messages` — structured, validated
  inter-agent messages (no uncontrolled shared state);
* :mod:`~forge.orchestration.scheduler` — the bounded, conflict-aware
  parallel scheduler with retries, cancellation, and failure isolation;
* :mod:`~forge.orchestration.state` — persistent execution state
  (durable, resumable, inspectable);
* :mod:`~forge.orchestration.activity` — the agent-activity model the
  desktop views render.

The supervisor (``forge.core.supervisor.Supervisor.execute_parallel``)
wraps the scheduler with the A33 permission gate and A32 checkpoint
atomicity; the control plane exposes executions to the cockpit and
desktop with live agent-activity snapshots.
"""
from forge.orchestration.activity import AgentActivityTracker
from forge.orchestration.graph import (TaskGraph, TaskKind, TaskNode,
                                       TaskStatus)
from forge.orchestration.locking import (DeadlockError, ResourceLockManager,
                                         LockWaitTimeout)
from forge.orchestration.messages import (AgentMessage, MessageBus)
from forge.orchestration.roles import (AGENT_ROLES, AgentRoleSpec,
                                       RoleResourceLimits, get_role_spec,
                                       model_satisfies, validate_result)
from forge.orchestration.scheduler import (ParallelTaskScheduler,
                                           TaskDeniedError,
                                           TaskTimeoutError,
                                           ExecutionReport,
                                           TaskWorkItem)
from forge.orchestration.state import ExecutionStateStore

__all__ = [
    "AGENT_ROLES", "AgentActivityTracker", "AgentMessage",
    "AgentRoleSpec", "DeadlockError", "ExecutionReport",
    "ExecutionStateStore", "LockWaitTimeout", "MessageBus",
    "ParallelTaskScheduler", "ResourceLockManager", "RoleResourceLimits",
    "TaskDeniedError", "TaskGraph", "TaskKind", "TaskNode", "TaskStatus",
    "TaskTimeoutError", "TaskWorkItem", "get_role_spec", "model_satisfies",
    "validate_result",
]
