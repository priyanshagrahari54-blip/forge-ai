"""Thin, additive integration points between long-term memory and Forge.

Every helper here is *optional*: the planner, context engine, debugger,
reviewer, and model router accept a ``memory`` argument that defaults to
``None``, in which case behavior is byte-identical to before. When a
:class:`forge.memory.engine.LongTermMemory` is supplied, relevant knowledge
is recalled into planning/context and lifecycle facts are remembered.

All text that flows into memory passes the engine's own redaction, so
integrations never need to sanitize content themselves.
"""
from __future__ import annotations

from typing import Any, List, Optional

from forge.memory.types import MemoryType

#: Cap on the number of memory items injected into a single prompt/context.
RECALL_LIMIT = 6


def recall_for_planning(memory: Any, project: str, task: str,
                        *, k: int = 5) -> List[Any]:
    """Relevant memories a planner should consider before planning."""
    if memory is None:
        return []
    try:
        return memory.recall(task, project=project,
                             memory_type=None, k=max(1, min(k, RECALL_LIMIT)))
    except Exception:
        return []


def recall_for_context(memory: Any, project: str, task: str,
                       *, k: int = RECALL_LIMIT) -> List[Any]:
    """Relevant memories to inject into agent context building."""
    return recall_for_planning(memory, project, task, k=k)


def recall_for_review(memory: Any, project: str, task: str,
                      *, k: int = RECALL_LIMIT) -> List[Any]:
    """Relevant memories a reviewer should keep in mind."""
    return recall_for_planning(memory, project, task, k=k)


def recall_for_routing(memory: Any, project: str, capability: str,
                       *, k: int = 3) -> List[Any]:
    """Recent model-performance memories relevant to a routing decision."""
    if memory is None:
        return []
    try:
        return memory.recall(capability, project=project,
                             memory_type=MemoryType.MODEL_PERFORMANCE,
                             k=max(1, min(k, 3)))
    except Exception:
        return []


def remember_task(memory: Any, project: str, task_id: str, requirement: str,
                  outcome: str, *, source: str = "supervisor",
                  importance: float = 0.5) -> Any:
    """Record a bounded task outcome as task memory."""
    if memory is None:
        return None
    try:
        return memory.remember(
            MemoryType.TASK,
            f"task {task_id} [{outcome}]: {requirement}",
            project=project, source=source, via="task-lifecycle",
            importance=importance)
    except Exception:
        return None


def remember_failure(memory: Any, project: str, error: str, *,
                     task_id: str = "", source: str = "debugger",
                     importance: float = 0.8) -> Any:
    """Record a bounded, redacted failure signature as failure memory."""
    if memory is None:
        return None
    try:
        label = f"task {task_id}: " if task_id else ""
        return memory.remember(
            MemoryType.FAILURE, f"{label}{error}",
            project=project, source=source, via="test-debug-loop",
            confidence=0.9, importance=importance)
    except Exception:
        return None


def remember_decision(memory: Any, project: str, decision: str, *,
                      source: str = "reviewer", importance: float = 0.7,
                      confidence: float = 0.7) -> Any:
    """Record a durable decision (persistent retention) for later recall."""
    if memory is None:
        return None
    try:
        return memory.remember(
            MemoryType.DECISION, decision, project=project, source=source,
            via="decision-record", confidence=confidence,
            importance=importance)
    except Exception:
        return None


def remember_model_performance(memory: Any, project: str, model: str, *,
                               provider: str = "", capability: str = "",
                               success: bool = True, latency_ms: float = 0.0,
                               tokens: int = 0, error: str = "",
                               source: str = "model-router") -> Any:
    """Record one routed-call outcome as model-performance memory.

    Routine successes are low-importance and task-scoped so they cannot
    flood durable memory; failures carry the (redacted) error and higher
    importance because they inform future routing.
    """
    if memory is None:
        return None
    try:
        outcome = "ok" if success else "failed"
        detail = f"model {model} ({provider or '-'}) {capability or '-'} " \
                 f"-> {outcome}"
        if latency_ms is not None:
            detail += f" latency={latency_ms:.0f}ms"
        if tokens:
            detail += f" tokens={tokens}"
        if not success and error:
            detail += f" error={error}"
        return memory.remember(
            MemoryType.MODEL_PERFORMANCE, detail,
            project=project, source=source, via="router-feedback",
            confidence=0.9, importance=0.6 if not success else 0.25)
    except Exception:
        return None


def memory_context_text(results: List[Any], *, max_chars: int = 1600) -> str:
    """Render recall results as a compact block for prompt injection."""
    if not results:
        return ""
    lines = ["Relevant remembered project knowledge:"]
    for result in results:
        record = getattr(result, "record", None)
        if record is None:
            continue
        body = (record.summary or record.content).replace("\n", " ")
        lines.append(
            f"- [{record.memory_type}] {body[:240]}"
            f" (confidence={record.confidence:.2f}, "
            f"importance={record.importance:.2f})")
    text = "\n".join(lines)
    return text[:max_chars]
