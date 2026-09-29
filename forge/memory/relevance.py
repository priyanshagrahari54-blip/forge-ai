"""Lexical relevance for long-term memory retrieval.

Retrieval is deterministic and dependency-free: no embeddings and no model
calls. Documents are tokenized into a small vocabulary, query terms are
weighted with inverse document frequency over the candidate set, and the
final score blends lexical overlap with recency, importance, confidence,
and a per-type prior — so relevant-but-old knowledge still competes, and
unrelated items are naturally suppressed.
"""
from __future__ import annotations

import hashlib
import math
import re
import time
from typing import Dict, Iterable, List, Optional, Set, Tuple

from forge.memory.types import MemoryRecord, SearchResult

#: A compact English stop-word set. Memory is largely technical prose, so
#: this stays small on purpose: project terms like "cache" or "route" must
#: remain indexable.
STOPWORDS: Set[str] = frozenset(
    """
    a an and are as at be but by for from has have he her his i if in into
    is it its me my no not of on or our she so that the their them then there
    these they this to was we were what when where which who will with you your
    """.split()
)

_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-]*")

#: Half-life (seconds) for recency decay. Older than ~3 months fades fast.
RECENCY_HALF_LIFE_SECONDS = 30 * 24 * 3600.0

#: Per-type retrieval prior: some layers are more valuable than others when
#: a task asks "what do we know?". Applied as a multiplicative boost.
TYPE_PRIOR: Dict[str, float] = {
    "decision": 1.0,
    "project": 1.0,
    "failure": 0.95,
    "model_performance": 0.9,
    "task": 0.85,
    "agent": 0.85,
    "session": 0.7,
}


def tokenize(text: str) -> List[str]:
    """Lowercase alphanumeric tokens from arbitrary prose."""
    if not text:
        return []
    return _WORD_RE.findall(text.lower())


def content_tokens(text: str) -> List[str]:
    """Non-stopword tokens (the signal a memory item actually carries)."""
    return [tok for tok in tokenize(text) if tok not in STOPWORDS]


def normalize(text: str) -> str:
    """Deterministic normalized form used for exact-duplicate fingerprinting."""
    return " ".join(content_tokens(text))


def fingerprint(content: str) -> str:
    """Stable fingerprint of the normalized content (dedupe key)."""
    return hashlib.sha256(normalize(content).encode("utf-8")).hexdigest()


def jaccard(left: List[str], right: List[str]) -> float:
    """Token-set Jaccard similarity (0.0..1.0), dedup-safe."""
    a = set(left)
    b = set(right)
    if not a or not b:
        return 0.0
    union = len(a | b)
    return len(a & b) / union if union else 0.0


def signal_ratio(content: str) -> float:
    """Ratio of content (non-stopword) tokens to all tokens in a string."""
    toks = tokenize(content)
    if not toks:
        return 0.0
    return len(content_tokens(content)) / len(toks)


def recency_factor(created_at: float, now: Optional[float] = None) -> float:
    """Exponential decay factor in (0, 1]; recent records score ~1."""
    moment = time.time() if now is None else now
    age = max(0.0, moment - created_at)
    return math.exp(-age / RECENCY_HALF_LIFE_SECONDS)


class RelevanceRanker:
    """Ranks a bounded candidate set against a free-text query."""

    def __init__(self, records: Iterable[MemoryRecord],
                 now: Optional[float] = None) -> None:
        self.records: List[MemoryRecord] = list(records)
        self.now = time.time() if now is None else now
        self._df: Dict[str, int] = {}
        # Performance optimization (Bolt ⚡): Store doc tokens as set for O(1) membership lookups
        # and precompute static per-record factors to avoid recalculating in search hot loops.
        self._doc_tokens: Dict[str, Set[str]] = {}
        self._record_factors: Dict[str, float] = {}

        for record in self.records:
            tokens = set(content_tokens(record.content))
            tokens.update(content_tokens(record.summary))
            self._doc_tokens[record.id] = tokens
            for term in tokens:
                self._df[term] = self._df.get(term, 0) + 1

            importance = 0.5 + 0.5 * float(record.importance)
            confidence = 0.5 + 0.5 * float(record.confidence)
            type_prior = TYPE_PRIOR.get(record.memory_type, 0.8)
            rf = recency_factor(record.created_at, self.now)
            self._record_factors[record.id] = rf * importance * confidence * type_prior

        self._n = max(1, len(self.records))

    def idf(self, term: str) -> float:
        return math.log((self._n + 1.0) / (self._df.get(term, 0) + 1.0)) + 1.0

    def _lexical(self, query_terms: List[str], doc_terms: Iterable[str]) -> float:
        """Weighted query-coverage score in [0, 1]."""
        if not query_terms:
            return 0.0
        doc_set = doc_terms if isinstance(doc_terms, set) else set(doc_terms)
        matched = [term for term in query_terms if term in doc_set]
        if not matched:
            return 0.0
        matched_weight = sum(self.idf(term) for term in set(matched))
        total_weight = sum(self.idf(term) for term in set(query_terms))
        return matched_weight / total_weight if total_weight else 0.0

    def rank(self, query: str, *, k: int = 10,
             min_score: float = 0.0) -> List[SearchResult]:
        query_terms = sorted(set(content_tokens(query)))
        if not query_terms:
            return []

        # Performance optimization (Bolt ⚡): Precompute IDF weights for query terms once
        # to avoid repeated math.log and dict lookups for every candidate record.
        query_weights = {term: self.idf(term) for term in query_terms}
        total_weight = sum(query_weights.values())
        if not total_weight:
            return []

        scored: List[SearchResult] = []
        for record in self.records:
            doc_terms = self._doc_tokens.get(record.id)
            if not doc_terms:
                continue

            # Fast direct list filter using precomputed set lookups
            matched_list = [term for term in query_terms if term in doc_terms]
            if not matched_list:
                continue

            matched_weight = sum(query_weights[term] for term in matched_list)
            lexical = matched_weight / total_weight

            score = lexical * self._record_factors[record.id]
            if score < min_score:
                continue

            matched = tuple(matched_list)
            scored.append(SearchResult(
                record=record,
                score=score,
                matched_terms=matched,
                reason="matched: " + ", ".join(matched[:6])
                if matched else "type/recency match",
            ))
        scored.sort(key=lambda item: (-item.score, -item.record.importance,
                                      -item.record.created_at, item.record.id))
        return scored[:max(0, k)]
