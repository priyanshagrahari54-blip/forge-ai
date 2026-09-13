"""Provenance vocabulary and result model for the secure research engine.

Every research result Forge produces carries an explicit, machine-readable
provenance class. Consumers (planner, context engine, CLI, cockpit) can
therefore always tell *where* a fact came from and never confuse
verified local evidence or a real fetched page with model recollection.

The four classes are mutually exclusive:

``LOCAL_SOURCE``
    Read from the project on disk (source files, local documentation,
    repository metadata). Citations are repo-relative paths (+ line).
``REAL_WEB_RESULT``
    Bytes actually downloaded from a remote host through the SSRF-safe
    chain. Citations are the final fetched URL. Untrusted input.
``USER_PROVIDED``
    Text or files handed to the engine by the operator for this query.
``MODEL_KNOWLEDGE``
    Produced by a language model without a verifiable source. Never
    used as a silent substitute for failed web research; only present
    when explicitly requested, and always labeled.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class Provenance(str, Enum):
    """Origin class of a research result."""

    MODEL_KNOWLEDGE = "MODEL_KNOWLEDGE"
    LOCAL_SOURCE = "LOCAL_SOURCE"
    REAL_WEB_RESULT = "REAL_WEB_RESULT"
    USER_PROVIDED = "USER_PROVIDED"

    @property
    def verified(self) -> bool:
        """True when the content was read from a concrete source."""
        return self in (Provenance.LOCAL_SOURCE,
                        Provenance.REAL_WEB_RESULT,
                        Provenance.USER_PROVIDED)

    @property
    def trusted(self) -> bool:
        """Only local project data is treated as trusted input."""
        return self == Provenance.LOCAL_SOURCE


#: Machine-readable states of one source run.
STATE_SUCCESS = "SUCCESS"
STATE_EMPTY = "EMPTY"            # ran fine, nothing matched
STATE_SKIPPED = "SKIPPED"        # not selected / disabled for this query
STATE_UNAVAILABLE = "UNAVAILABLE"  # not configured
STATE_POLICY_DENIED = "POLICY_DENIED"  # SSRF / network policy refusal
STATE_TIMEOUT = "TIMEOUT"
STATE_ERROR = "ERROR"            # provider / parse / HTTP failure
STATE_CACHED = "CACHED"          # served from cache (was SUCCESS)

FAILURE_STATES = frozenset({STATE_POLICY_DENIED, STATE_TIMEOUT, STATE_ERROR})

_WS = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """Collapse whitespace and lowercase, for fingerprints and matching."""
    return _WS.sub(" ", (text or "")).strip().lower()


def content_fingerprint(text: str) -> str:
    """Stable fingerprint of normalized content (dedup key)."""
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()[:16]


@dataclass
class Citation:
    """Where exactly a result came from."""

    locator: str                     # repo path or URL
    line: Optional[int] = None
    title: str = ""
    retrieved_at: str = ""           # ISO timestamp for web fetches
    final_url: str = ""              # after redirects (web only)
    status: int = 0                  # HTTP status (web only)

    def render(self) -> str:
        if self.line is not None:
            return f"{self.locator}:{self.line}"
        return self.locator

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {"locator": self.locator}
        if self.line is not None:
            data["line"] = self.line
        if self.title:
            data["title"] = self.title
        if self.retrieved_at:
            data["retrieved_at"] = self.retrieved_at
        if self.final_url and self.final_url != self.locator:
            data["final_url"] = self.final_url
        if self.status:
            data["status"] = self.status
        return data


@dataclass
class ResearchResult:
    """One evidence item with mandatory provenance."""

    source: str                      # source name (e.g. "project_files")
    provenance: Provenance
    title: str
    snippet: str
    citation: Citation
    score: float = 0.0
    kind: str = "text"               # symbol / file / doc / metadata / page / note
    matched_terms: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.provenance, Provenance):
            self.provenance = Provenance(str(self.provenance))
        self.title = (self.title or "")[:200]
        self.snippet = (self.snippet or "")[:1200]

    @property
    def fingerprint(self) -> str:
        return content_fingerprint(self.snippet or self.title)

    @property
    def untrusted(self) -> bool:
        return not self.provenance.trusted

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "provenance": self.provenance.value,
            "verified": self.provenance.verified,
            "untrusted": self.untrusted,
            "title": self.title,
            "snippet": self.snippet,
            "citation": self.citation.to_dict(),
            "cite": self.citation.render(),
            "score": round(self.score, 4),
            "kind": self.kind,
            "matched_terms": list(self.matched_terms),
            "metadata": dict(self.metadata),
        }


@dataclass
class SourceOutcome:
    """Result of running one source for one query. Failures are explicit."""

    source: str
    provenance: Provenance
    state: str
    results: List[ResearchResult] = field(default_factory=list)
    error: str = ""
    attempted: bool = True
    from_cache: bool = False
    duration_ms: int = 0

    @property
    def failed(self) -> bool:
        return self.state in FAILURE_STATES

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "provenance": self.provenance.value,
            "state": self.state,
            "attempted": self.attempted,
            "failed": self.failed,
            "from_cache": self.from_cache,
            "result_count": len(self.results),
            "error": self.error[:400],
            "duration_ms": self.duration_ms,
        }
