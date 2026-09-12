"""Shared types and constants for the long-term memory system.

Long-term memory is *project knowledge*: facts, decisions, failures,
observed model performance, and the running history of agents and tasks.
It is never ephemeral runtime state and never secrets. Every record is
typed, timestamped, project-scoped, sourced, and carries confidence,
importance, and a retention policy so retrieval and eviction are both
deterministic.

This module defines the vocabulary (memory types and retention policies)
and the public record shape used by :mod:`forge.memory.engine`.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional


class MemoryType(str, Enum):
    """The seven memory layers Forge remembers across tasks and sessions."""

    SESSION = "session"
    TASK = "task"
    PROJECT = "project"
    FAILURE = "failure"
    DECISION = "decision"
    AGENT = "agent"
    MODEL_PERFORMANCE = "model_performance"

    @classmethod
    def values(cls) -> tuple:
        return tuple(member.value for member in cls)

    @classmethod
    def parse(cls, value) -> "MemoryType":
        """Coerce a value to a :class:`MemoryType`, raising on unknowns."""
        if isinstance(value, MemoryType):
            return value
        if not isinstance(value, str):
            raise ValueError(f"memory type must be a string: {value!r}")
        candidate = value.strip().lower()
        try:
            return cls(candidate)
        except ValueError:
            raise ValueError(
                f"unknown memory type {value!r}; expected one of "
                f"{', '.join(cls.values())}") from None


class Retention(str, Enum):
    """Retention policy for a memory item (drives expiry, never secret)."""

    EPHEMERAL = "ephemeral"    # one interaction; expires quickly
    SESSION = "session"        # a working session
    TASK = "task"              # a single task's lifetime
    PROJECT = "project"        # durable project knowledge
    PERSISTENT = "persistent"  # never auto-expires (still correctable)

    @classmethod
    def values(cls) -> tuple:
        return tuple(member.value for member in cls)

    @classmethod
    def parse(cls, value: Optional["Retention"]) -> "Retention":
        if value is None:
            return Retention.PROJECT
        if isinstance(value, Retention):
            return value
        candidate = str(value).strip().lower()
        try:
            return cls(candidate)
        except ValueError:
            raise ValueError(
                f"unknown retention policy {value!r}; expected one of "
                f"{', '.join(cls.values())}") from None


#: Default TTL (seconds) per retention policy. ``None`` means "never".
DEFAULT_TTL_SECONDS: Dict[str, Optional[float]] = {
    Retention.EPHEMERAL.value: 3600.0,        # 1 hour
    Retention.SESSION.value: 12 * 3600.0,     # 12 hours
    Retention.TASK.value: 7 * 24 * 3600.0,    # 7 days
    Retention.PROJECT.value: 180 * 24 * 3600.0,  # 180 days
    Retention.PERSISTENT.value: None,         # never
}

#: Default retention policy per memory type.
DEFAULT_RETENTION_BY_TYPE: Dict[str, Retention] = {
    MemoryType.SESSION.value: Retention.SESSION,
    MemoryType.TASK.value: Retention.TASK,
    MemoryType.PROJECT.value: Retention.PROJECT,
    MemoryType.FAILURE.value: Retention.PROJECT,
    MemoryType.DECISION.value: Retention.PERSISTENT,
    MemoryType.AGENT.value: Retention.TASK,
    MemoryType.MODEL_PERFORMANCE.value: Retention.TASK,
}

#: Record statuses.
ACTIVE = "active"
SUPERSEDED = "superseded"
DELETED = "deleted"
EXPIRED = "expired"
SUMMARIZED = "summarized"

#: Provenance actions written to the audit trail.
ACTION_CREATED = "created"
ACTION_CORRECTED = "corrected"
ACTION_SUPERSEDED = "superseded"
ACTION_MERGED = "merged"
ACTION_DELETED = "deleted"
ACTION_PURGED = "purged"
ACTION_EXPIRED = "expired"
ACTION_SUMMARIZED = "summarized"
ACTION_REJECTED = "rejected"


@dataclass(frozen=True)
class MemoryRecord:
    """One durable memory item, always safe to serialize (content is redacted)."""

    id: str
    memory_type: str
    project: str
    content: str
    summary: str
    source: str
    via: str
    confidence: float
    importance: float
    retention: str
    expires_at: Optional[float]
    created_at: float
    updated_at: float
    last_accessed_at: float
    access_count: int
    fingerprint: str
    status: str
    version: int
    supersedes: str
    metadata: Dict[str, Any]
    redaction_count: int

    @property
    def active(self) -> bool:
        return self.status == ACTIVE

    def to_dict(self, *, include_content: bool = True) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "id": self.id,
            "type": self.memory_type,
            "project": self.project,
            "summary": self.summary,
            "source": self.source,
            "via": self.via,
            "confidence": self.confidence,
            "importance": self.importance,
            "retention": self.retention,
            "expires_at": self.expires_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_accessed_at": self.last_accessed_at,
            "access_count": self.access_count,
            "fingerprint": self.fingerprint,
            "status": self.status,
            "version": self.version,
            "supersedes": self.supersedes,
            "redaction_count": self.redaction_count,
            "metadata": dict(self.metadata or {}),
        }
        if include_content:
            payload["content"] = self.content
        return payload


@dataclass(frozen=True)
class RememberResult:
    """Outcome of an ingestion attempt (``stored``/``duplicate``/``rejected``)."""

    status: str
    record: Optional[MemoryRecord] = None
    duplicate_of: str = ""
    reason: str = ""

    @property
    def stored(self) -> bool:
        return self.status == "stored" and self.record is not None

    def to_dict(self, *, include_content: bool = True) -> Dict[str, Any]:
        return {
            "status": self.status,
            "record": self.record.to_dict(include_content=include_content)
            if self.record is not None else None,
            "duplicate_of": self.duplicate_of,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class SearchResult:
    """A ranked retrieval hit: the record plus why it matched."""

    record: MemoryRecord
    score: float
    matched_terms: tuple
    reason: str = ""

    def to_dict(self, *, include_content: bool = True) -> Dict[str, Any]:
        return {
            "record": self.record.to_dict(include_content=include_content),
            "score": round(float(self.score), 6),
            "matched_terms": list(self.matched_terms),
            "reason": self.reason,
        }
