"""Long-term memory for Forge (project knowledge across tasks and sessions).

Seven durable memory layers — session, task, project, failure, decision,
agent, and model-performance — stored in SQLite with secret redaction,
deduplication, flood control, summarization, correction, deletion,
provenance, and relevance-based retrieval.

Public surface:

* :class:`LongTermMemory` (``forge.memory.engine``) — the SQLite engine.
* :class:`MemoryType` / :class:`Retention` / :class:`MemoryRecord`
  (``forge.memory.types``) — the record vocabulary.
* :mod:`forge.memory.redaction` — secret detection/redaction.
* :mod:`forge.memory.relevance` — lexical relevance ranking.
* :mod:`forge.memory.integrations` — optional hooks for the planner,
  context engine, debugger, reviewer, and model router.

The legacy file-backed :class:`forge.memory.store.MemoryStore` remains
available for the A37 control-plane surface and is independent of the
SQLite long-term store.
"""
from __future__ import annotations

from forge.memory.engine import LongTermMemory, MemoryConfig
from forge.memory.types import (
    MemoryRecord,
    MemoryType,
    RememberResult,
    Retention,
    SearchResult,
)

__all__ = [
    "LongTermMemory",
    "MemoryConfig",
    "MemoryRecord",
    "MemoryType",
    "RememberResult",
    "Retention",
    "SearchResult",
]
