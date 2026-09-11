"""Deterministic extractive summarization for memory compaction.

Summaries are *extractive* (sentences chosen from the source items), never
generated prose, so a summary can never invent a fact or leak content that
was not already redacted. The algorithm scores sentences by the frequency
of their content terms, drops near-duplicate sentences, and keeps the
highest-scoring few in original order.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Iterable, List, Tuple

from forge.memory.relevance import content_tokens, jaccard

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
_MIN_SENTENCE_TOKENS = 4
_MAX_SENTENCE_TOKENS = 60


def _sentences(text: str) -> List[str]:
    raw = _SENTENCE_SPLIT.split(text or "")
    cleaned: List[str] = []
    for piece in raw:
        candidate = " ".join(piece.split())
        if candidate and _MIN_SENTENCE_TOKENS <= len(content_tokens(candidate)) <= _MAX_SENTENCE_TOKENS:
            cleaned.append(candidate)
    return cleaned


def extractive_summary(documents: Iterable[str], *, max_sentences: int = 3,
                       max_chars: int = 1200) -> str:
    """Pick the most representative sentences from ``documents``.

    Returns an empty string when the source carries too little signal.
    """
    corpus: List[str] = []
    for document in documents:
        corpus.extend(_sentences(document))
    if not corpus:
        return ""

    # Term frequency across the corpus, weighted by a mild length penalty so
    # a single long sentence cannot dominate the summary.
    frequencies: Counter = Counter()
    for sentence in corpus:
        for term in set(content_tokens(sentence)):
            frequencies[term] += 1

    def score(sentence: str) -> float:
        tokens = content_tokens(sentence)
        if not tokens:
            return 0.0
        total = sum(frequencies[term] for term in tokens)
        return total / len(tokens)

    ranked = sorted(corpus, key=score, reverse=True)

    chosen: List[str] = []
    for sentence in ranked:
        if len(chosen) >= max_sentences:
            break
        if any(jaccard(content_tokens(sentence), content_tokens(kept)) > 0.8
               for kept in chosen):
            continue
        chosen.append(sentence)

    # Restore original order for readability.
    chosen.sort(key=corpus.index)

    result = " ".join(chosen)
    if len(result) > max_chars:
        result = result[:max_chars].rsplit(" ", 1)[0] + " …"
    return result


def summarize_records(records: Iterable, *, max_sentences: int = 3) -> str:
    """Summarize memory records (uses ``content`` plus any prior ``summary``)."""
    documents: List[str] = []
    for record in records:
        if getattr(record, "summary", ""):
            documents.append(record.summary)
        documents.append(getattr(record, "content", ""))
    return extractive_summary(documents, max_sentences=max_sentences)
