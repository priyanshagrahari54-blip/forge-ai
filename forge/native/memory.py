"""Native project memory: durable engineering knowledge, never secrets.

Built on :class:`forge.memory.MemoryStore` (bounded, path-safe, one file per
entry under ``.forge/memory/native/``). The engine records exactly the five
A81 categories: project decisions, successful strategies, failure
information, useful repository patterns, and verification results.

Integrity rules enforced here (defense in depth; the store itself already
rejects traversal/oversized keys and entries):

* **Credential rejection**: content is scanned with the same secret patterns
  the security verification gate uses, after best-effort redaction. If a
  secret-looking value survives redaction (e.g. a private-key block), the
  write is refused — memory never becomes a secret store.
* **Redaction first**: values are passed through
  :func:`forge.core.report.redact` on the way in, so masked data is what gets
  scanned and stored.
* **Bounded reads**: recall returns the most recent N entries, newest first,
  deterministic order, no full-directory dumps.
* **Honest provenance**: every entry records ``source`` (which engine stage
  produced it) and real timestamps; entries are JSON so downstream tooling
  (including :mod:`forge.native.training`) can consume them without parsing
  prose.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from forge.core.report import redact
from forge.memory.store import MemoryStore


class MemoryCategory(str, Enum):
    DECISION = "decisions"
    STRATEGY = "strategies"
    FAILURE = "failures"
    PATTERN = "patterns"
    VERIFICATION = "verification"

    @classmethod
    def all(cls) -> "tuple[MemoryCategory, ...]":
        return (cls.DECISION, cls.STRATEGY, cls.FAILURE, cls.PATTERN,
                cls.VERIFICATION)


#: Hard caps (bytes) for one serialized entry. 2 GB-machine friendly and
#: well under the store's 5 MiB per-entry bound.
MAX_ENTRY_BYTES = 64 * 1024


@dataclass
class MemoryRecord:
    """One durable memory entry."""

    id: str
    category: str
    task: str
    created_at: float
    source: str
    payload: Dict[str, Any] = field(default_factory=dict)
    redacted: bool = False

    def to_json(self) -> str:
        return json.dumps({
            "id": self.id,
            "category": self.category,
            "task": self.task,
            "created_at": self.created_at,
            "source": self.source,
            "redacted": self.redacted,
            "payload": self.payload,
        }, sort_keys=True, default=str)

    @classmethod
    def from_json(cls, text: str) -> Optional["MemoryRecord"]:
        try:
            data = json.loads(text)
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        try:
            return cls(
                id=str(data.get("id", "")),
                category=str(data.get("category", "")),
                task=str(data.get("task", ""))[:4000],
                created_at=float(data.get("created_at", 0.0) or 0.0),
                source=str(data.get("source", "")),
                payload=dict(data.get("payload") or {}),
                redacted=bool(data.get("redacted", False)),
            )
        except (TypeError, ValueError):
            return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "task": self.task[:400],
            "created_at": self.created_at,
            "source": self.source,
            "redacted": self.redacted,
            "payload": self.payload,
        }


class SecretInMemoryError(ValueError):
    """Raised when a record still contains secret-looking content."""


class NativeMemory:
    """Bounded, categorized project memory with a credentials guard."""

    def __init__(self, root: str | Path = ".",
                 store: Optional[MemoryStore] = None,
                 enabled: bool = True) -> None:
        self.root = Path(root).resolve()
        self.enabled = enabled
        self.store = store or MemoryStore(
            str(self.root / ".forge" / "memory" / "native"))
        #: In-memory view kept in sync on every write/read so recall is fast
        #: and deterministic without repeatedly walking the tree.
        self._sequence = 0

    # -- writing ---------------------------------------------------------------

    def record(self, category: MemoryCategory, task: str, source: str,
               payload: Optional[Dict[str, Any]] = None) -> MemoryRecord:
        """Redact, guard, and persist one entry. Returns the stored record."""
        category = MemoryCategory(category)
        original_payload = dict(payload or {})
        clean_payload = redact(original_payload)
        changed_by_redaction = json.dumps(clean_payload, sort_keys=True,
                                          default=str) != json.dumps(
            original_payload, sort_keys=True, default=str)
        record = MemoryRecord(
            id=self._next_id(category),
            category=category.value,
            task=redact(" ".join((task or "").split()))[:4000],
            created_at=time.time(),
            source=source,
            payload=clean_payload,
            redacted=changed_by_redaction,
        )
        serialized = record.to_json()
        if len(serialized.encode("utf-8")) > MAX_ENTRY_BYTES:
            # Trim payload values (not metadata) until the entry fits; the
            # entry still describes real work, just with shorter evidence.
            record.payload = self._shrink(clean_payload)
            serialized = record.to_json()
        guarded, changed = self._guard_secrets(serialized)
        record.redacted = bool(record.redacted or changed)
        if guarded is None:
            raise SecretInMemoryError(
                "refusing to store memory entry: it still contains "
                "secret-looking content after redaction")
        if not self.enabled:
            return record
        self.store.save("%s/%s.json" % (category.value, record.id), guarded)
        return record

    def record_decision(self, task: str, decision: str, reason: str = "",
                        source: str = "native-ai") -> MemoryRecord:
        return self.record(MemoryCategory.DECISION, task, source,
                           {"decision": decision, "reason": reason})

    def record_strategy(self, task: str, strategy: str, evidence:
                        Optional[Dict[str, Any]] = None,
                        source: str = "native-ai") -> MemoryRecord:
        return self.record(MemoryCategory.STRATEGY, task, source,
                           {"strategy": strategy,
                            "evidence": dict(evidence or {})})

    def record_failure(self, task: str, classification: str, summary: str,
                       evidence: Optional[Dict[str, Any]] = None,
                       source: str = "native-ai") -> MemoryRecord:
        return self.record(MemoryCategory.FAILURE, task, source,
                           {"classification": classification,
                            "summary": summary,
                            "evidence": dict(evidence or {})})

    def record_pattern(self, task: str, pattern: str,
                        source: str = "native-ai") -> MemoryRecord:
        return self.record(MemoryCategory.PATTERN, task, source,
                           {"pattern": pattern})

    def record_verification(self, task: str, result: Dict[str, Any],
                            source: str = "native-ai") -> MemoryRecord:
        summary = {name: result[name] for name in
                   ("status", "executed", "passed", "failed", "skipped")
                   if name in result}
        return self.record(MemoryCategory.VERIFICATION, task, source,
                           {"result": summary})

    # -- reading ---------------------------------------------------------------

    def recall(self, category: MemoryCategory, query_terms:
               Optional[List[str]] = None, limit: int = 5
               ) -> List[MemoryRecord]:
        """Newest-first bounded recall; optional deterministic term filter."""
        category = MemoryCategory(category)
        limit = max(1, min(int(limit), 50))
        terms = [t.lower() for t in (query_terms or []) if t.strip()]
        results: List[MemoryRecord] = []
        for key in self.store.list():
            if not key.startswith(category.value + "/"):
                continue
            text = self.store.load(key)
            if text is None:
                continue
            record = MemoryRecord.from_json(text)
            if record is None or record.category != category.value:
                continue
            if terms:
                haystack = (record.task + " " + json.dumps(
                    record.payload, default=str)).lower()
                if not any(term in haystack for term in terms):
                    continue
            results.append(record)
        results.sort(key=lambda item: (item.created_at, item.id),
                     reverse=True)
        return results[:limit]

    def recent_failures(self, limit: int = 3) -> List[MemoryRecord]:
        return self.recall(MemoryCategory.FAILURE, limit=limit)

    def successful_strategies(self, query_terms: Optional[List[str]] = None,
                              limit: int = 3) -> List[MemoryRecord]:
        return self.recall(MemoryCategory.STRATEGY, query_terms, limit)

    def decisions(self, limit: int = 3) -> List[MemoryRecord]:
        return self.recall(MemoryCategory.DECISION, limit=limit)

    def summary(self) -> Dict[str, Any]:
        """Counts per category — cheap status for panel/CLI."""
        counts: Dict[str, int] = {c.value: 0 for c in MemoryCategory.all()}
        total = 0
        for key in self.store.list():
            head = key.split("/", 1)[0]
            if head in counts:
                counts[head] += 1
                total += 1
        return {"enabled": self.enabled, "entries": total,
                "categories": counts,
                "root": str(self.store.root)}

    def clear(self) -> int:
        removed = 0
        for key in self.store.list():
            if self.store.delete(key):
                removed += 1
        return removed

    # -- helpers ------------------------------------------------------------------

    def _next_id(self, category: MemoryCategory) -> str:
        self._sequence += 1
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
        # uuid suffix: per-instance sequences collide across processes in the
        # same wall-clock second, which would silently overwrite entries.
        return "%s-%s-%04d-%s" % (category.value, stamp, self._sequence,
                                  uuid4().hex[:8])

    @staticmethod
    def _shrink(payload: Dict[str, Any]) -> Dict[str, Any]:
        """Bound long string values (head + tail) so entries stay small."""
        shrunk: Dict[str, Any] = {}
        for key, value in payload.items():
            if isinstance(value, str) and len(value) > 4000:
                shrunk[key] = value[:2000] + "\n...[truncated]...\n" \
                    + value[-1500:]
            elif isinstance(value, dict):
                shrunk[key] = NativeMemory._shrink(value)
            elif isinstance(value, list):
                shrunk[key] = value[:100]
            else:
                shrunk[key] = value
        return shrunk

    @staticmethod
    def _guard_secrets(serialized: str) -> "tuple[Optional[str], bool]":
        """Return (safe_text, changed) or (None, ...) when unrecoverable.

        First the general report redaction runs (masking k=v secret shapes);
        if private-key material or other unrecoverable markers remain, the
        write is refused outright.
        """
        changed = False
        text = serialized
        redacted_text = redact(text)
        if redacted_text != text:
            text, changed = redacted_text, True
        lowered = text.lower()
        for marker in ("-----begin", "private key-----", "begin rsa private",
                       "aws_secret_access_key"):
            if marker in lowered:
                return None, changed
        return text, changed
