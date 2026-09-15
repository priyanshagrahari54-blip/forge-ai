"""Long-term project memory (A83): durable, attributed, searchable facts.

Twelve kinds of entry — decisions, requirements, constraints, designs, known
bugs, failed approaches, solutions, benchmarks, dependencies, APIs,
conventions, testing requirements. A benchmark entry without measured numbers
is rejected: an unmeasured claim is not a benchmark.
"""
from __future__ import annotations

from forge.knowledge.memory import (
    API,
    BENCHMARK,
    BUG,
    CONSTRAINT,
    CONVENTION,
    DECISION,
    DEPENDENCY,
    DESIGN,
    FAILED_APPROACH,
    KINDS,
    OPEN,
    REQUIREMENT,
    RESOLVED,
    SOLUTION,
    SUPERSEDED,
    TESTING,
    MemoryEntry,
    MemoryError,
    ProjectMemory,
    memory_prompt,
    render,
)

__all__ = [
    "API", "BENCHMARK", "BUG", "CONSTRAINT", "CONVENTION", "DECISION",
    "DEPENDENCY", "DESIGN", "FAILED_APPROACH", "KINDS", "MemoryEntry",
    "MemoryError", "OPEN", "ProjectMemory", "REQUIREMENT", "RESOLVED",
    "SOLUTION", "SUPERSEDED", "TESTING", "memory_prompt", "render",
]
