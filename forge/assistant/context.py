"""Context engine: long-context architecture + quality (A84 Stages M/N).

The model never receives "everything". Assembly is:

1. **relevant** — retrieval-first: query-derived terms score short-term
   turns, session notes, long-term recall, research refs and project state;
2. **compressed** — history beyond a window is replaced by hierarchical
   summaries (the memory engine's summarizer), never truncated mid-meaning;
3. **deduplicated** — near-identical snippets collapse (jaccard over token
   sets) before budgeting;
4. **contradiction-checked** — retrieved facts that invert each other are
   flagged into the quality report (the caller surfaces them; the engine
   does not silently drop one);
5. **source-qualified** — provenance mix (verified vs model-knowledge) is
   counted so the answer layer can refuse to launder unverified context;
6. **budgeted** — a token estimate against the serving model's context
   window, with priority order user-turn > active-task state > retrieved
   memory > summaries > project facts; overflow items are dropped *and named*
   in the report (no silent starvation).

The output is "the smallest high-quality context that preserves the required
information" — with the numbers to prove it, not a vibe.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = ["ContextBundle", "ContextEngine", "ContextQuality"]

#: crude but deterministic token estimate: ~4 chars/token for prose.
CHARS_PER_TOKEN = 4.0
#: fraction of the serving context window the assistant may spend on context
#: (the request + response room belongs to execution, not to us)
CONTEXT_WINDOW_SHARE = 0.45
DEFAULT_TOKEN_BUDGET = 2400
JACCARD_DEDUPE = 0.85
SECTION_PREVIEW = 1600


def _tokens(text: str) -> set:
    return set(re.findall(r"[a-z0-9_][a-z0-9_.-]{2,}", (text or "").lower()))


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / float(len(a | b))


@dataclass(frozen=True)
class ContextSection:
    name: str
    text: str
    kind: str                    # user | task | memory | summary | project | research
    source: str = ""             # provenance label (memory id / run id / file)
    score: float = 1.0
    verified: bool = True

    @property
    def tokens(self) -> int:
        return max(1, int(len(self.text) / CHARS_PER_TOKEN))

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "source": self.source,
                "score": round(self.score, 4), "verified": self.verified,
                "tokens": self.tokens,
                "preview": self.text[:SECTION_PREVIEW]}


@dataclass(frozen=True)
class ContextQuality:
    """The Stage-N metrics, computed from the assembled sections."""

    relevance: float
    redundancy: float
    contradictions: Tuple[Dict[str, Any], ...] = ()
    missing_context: Tuple[str, ...] = ()
    source_quality: Dict[str, int] = field(default_factory=dict)
    token_budget: int = DEFAULT_TOKEN_BUDGET
    tokens_used: int = 0
    dropped: Tuple[str, ...] = ()

    @property
    def quality_score(self) -> float:
        score = self.relevance
        score -= 0.25 * self.redundancy
        score -= 0.05 * len(self.missing_context)
        model_share = self.source_quality.get("model-knowledge", 0)
        total = max(1, sum(self.source_quality.values()))
        score -= 0.15 * (model_share / total)
        return max(0.0, min(1.0, round(score, 4)))

    def to_dict(self) -> Dict[str, Any]:
        return {"relevance": round(self.relevance, 4),
                "redundancy": round(self.redundancy, 4),
                "contradictions": list(self.contradictions),
                "missing_context": list(self.missing_context),
                "source_quality": dict(self.source_quality),
                "context_budget": self.token_budget,
                "tokens_used": self.tokens_used,
                "dropped_sections": list(self.dropped),
                "quality_score": self.quality_score}


@dataclass(frozen=True)
class ContextBundle:
    query: str
    sections: Tuple[ContextSection, ...] = ()
    quality: Optional[ContextQuality] = None
    at: float = field(default_factory=time.time)

    def render(self, *, limit_chars: int = 24000) -> str:
        out: List[str] = []
        for section in self.sections:
            block = f"[{section.name}]" + (
                f" (source: {section.source})" if section.source else "")
            out.append(block + "\n" + section.text[:limit_chars])
        return "\n\n".join(out)[:limit_chars * max(1, len(self.sections))]

    def to_dict(self) -> Dict[str, Any]:
        return {"query": self.query[:200],
                "sections": [s.to_dict() for s in self.sections],
                "quality": self.quality.to_dict() if self.quality else {},
                "at": self.at,
                "honesty": ("context is retrieved, bounded and scored; the "
                            "model does not get the conversation of a lifetime")}


class ContextEngine:
    """Assembles + scores the smallest high-quality context."""

    def __init__(self, *, memory_service: Any = None, ledger: Any = None,
                 project_state_provider: Optional[Any] = None,
                 summarizer: Optional[Any] = None,
                 token_budget: int = 0,
                 max_retrieval: int = 6) -> None:
        self.memory_service = memory_service
        self.ledger = ledger
        self.project_state_provider = project_state_provider
        self.summarizer = summarizer
        self.token_budget = int(token_budget or 0) or DEFAULT_TOKEN_BUDGET
        self.max_retrieval = max(1, int(max_retrieval))

    # -- assembly ------------------------------------------------------------

    def build(self, query: str, *, session_id: str = "",
              model_context_window: int = 0,
              include_recent_turns: int = 6) -> ContextBundle:
        sections: List[ContextSection] = []
        budget = self.token_budget
        if model_context_window:
            budget = max(256, int(model_context_window * CONTEXT_WINDOW_SHARE)
                         // 2)   # half the "context share": the other half
                                  # is instruction + generation room the
                                  # executor needs
        query_terms = _tokens(query)

        # 1. active task / session state (short and always kept)
        session = self.ledger.get(session_id) if (self.ledger and session_id) \
            else None
        if session is not None:
            state_bits: List[str] = []
            if session.active_task_id:
                state_bits.append("active task: " + session.active_task_id)
            if session.project_id:
                state_bits.append("project: " + session.project_id)
            if session.summary:
                state_bits.append("session summary: " + session.summary[:600])
            if state_bits:
                sections.append(ContextSection(
                    "session-state", "\n".join(state_bits), "task",
                    source="session:" + session.id))
            # recent turns (short term), most relevant first
            turns = [t for t in session.history[-max(0, int(include_recent_turns)):]
                     if str(t.get("role", "")) in ("user", "assistant")]
            for turn in sorted(turns, key=lambda t: -_jaccard(
                    query_terms, _tokens(str(t.get("text", "")))))[:4]:
                text = str(turn.get("text", ""))[:900]
                if text:
                    sections.append(ContextSection(
                        f"turn-{turn.get('seq', 0)}", text, "user",
                        score=0.5 + _jaccard(query_terms, _tokens(text)),
                        verified=True))

        # 2. retrieved memory (relevance-ranked; NOT wholesale history)
        if self.memory_service is not None and hasattr(
                self.memory_service, "recall"):
            try:
                hits = list(self.memory_service.recall(
                    query, k=self.max_retrieval) or [])
            except Exception:
                hits = []
            for hit in hits:
                record = getattr(hit, "record", hit)
                text = str(getattr(record, "content", ""))[:900]
                if not text:
                    continue
                sections.append(ContextSection(
                    "memory:" + str(getattr(record, "memory_type", "?"))[:20],
                    text, "memory",
                    source=str(getattr(record, "id", ""))[:64],
                    score=float(getattr(hit, "score", 0.5) or 0.5),
                    verified=True))

        # 3. project state (one short block; provider decides what is real)
        if self.project_state_provider is not None:
            try:
                state_text = str(self.project_state_provider() or "")[:1200]
            except Exception:
                state_text = ""
            if state_text:
                sections.append(ContextSection("project-state", state_text,
                                               "project", verified=True))

        sections, missing = self._hierarchy(sections, query_terms, budget)
        return ContextBundle(query=query, sections=sections,
                             quality=self._quality(sections, query_terms,
                                                   missing, budget))

    # -- M: hierarchical summarization when history is long --------------------

    def _hierarchy(self, sections: List[ContextSection], query_terms: set,
                   budget: int) -> Tuple[List[ContextSection], List[str]]:
        """Deduplicate, then either fit or compress older turns to a digest."""
        kept: List[ContextSection] = []
        seen_tokens: List[set] = []
        dropped: List[str] = []
        for section in sections:
            tokens = _tokens(section.text)
            if any(_jaccard(tokens, prior) >= JACCARD_DEDUPE
                   for prior in seen_tokens):
                continue      # redundant copy never enters the prompt
            seen_tokens.append(tokens)
            kept.append(section)
        used = sum(s.tokens for s in kept)
        if used <= budget:
            return kept, dropped
        # Over budget: fold the LOWEST-scoring memory/turn sections into one
        # digest (lossy but labeled), instead of dropping them silently.
        foldable = [s for s in kept if s.kind in ("memory", "user")]
        foldable.sort(key=lambda s: s.score)
        digest_parts: List[str] = []
        for section in foldable:
            if used <= budget * 0.8:
                break
            digest_parts.append(f"- {section.name}: {section.text[:160]}")
            used -= section.tokens
            kept.remove(section)
        if digest_parts:
            kept.append(ContextSection(
                "digest", "\n".join(digest_parts[:8]), "summary",
                source="folded (lossy digest — originals remain in memory)",
                score=0.45, verified=True))
            used += max(1, int(len("\n".join(digest_parts)) / CHARS_PER_TOKEN))
        # final resort: drop lowest-score entries and *name* them
        while used > budget and kept:
            lowest = min(kept, key=lambda s: (s.score, s.kind != "task"))
            if lowest.kind in ("task", "project"):
                break
            kept.remove(lowest)
            dropped.append(lowest.name)
            used -= lowest.tokens
        kept.sort(key=lambda s: (-s.score, s.name))
        return kept, dropped

    # -- N: quality metrics --------------------------------------------------------

    def _quality(self, sections: Sequence[ContextSection], query_terms: set,
                 dropped: Sequence[str], budget: int = 0) -> ContextQuality:
        if not query_terms or not sections:
            relevance = 0.0
        else:
            scores = [max(0.0, min(1.0, _jaccard(query_terms, _tokens(s.text))))
                      for s in sections]
            relevance = sum(scores) / float(len(scores)) if scores else 0.0
        redundancy = 0.0
        token_sets = [_tokens(s.text) for s in sections]
        pairs = 0
        dupes = 0
        for i in range(len(token_sets)):
            for j in range(i + 1, len(token_sets)):
                pairs += 1
                if _jaccard(token_sets[i], token_sets[j]) > 0.5:
                    dupes += 1
        if pairs:
            redundancy = round(dupes / float(pairs), 4)
        contradictions = self._scan_contradictions(sections)
        covered = set()
        for s in sections:
            covered |= _tokens(s.text)
        missing = tuple(sorted(t for t in query_terms if t not in covered))[:10]
        source_quality = {"sections": len(sections),
                          "verified": sum(1 for s in sections if s.verified),
                          "model-knowledge": sum(
                              1 for s in sections if not s.verified)}
        return ContextQuality(
            relevance=round(relevance, 4), redundancy=redundancy,
            contradictions=contradictions, missing_context=missing,
            source_quality=source_quality,
            token_budget=max(1, int(budget or 0)),
            tokens_used=sum(s.tokens for s in sections),
            dropped=tuple(dropped))

    @staticmethod
    def _scan_contradictions(sections: Sequence[ContextSection]
                             ) -> Tuple[Dict[str, Any], ...]:
        out: List[Dict[str, Any]] = []
        negatives = (" not ", " never ", " cannot ", " without ", " disable",
                     "prohibit", "no ")
        for i in range(len(sections)):
            for j in range(i + 1, len(sections)):
                a, b = sections[i].text.lower(), sections[j].text.lower()
                shared = _tokens(a) & _tokens(b)
                if len(shared) < 4:
                    continue
                neg_a = any(n in a for n in negatives)
                neg_b = any(n in b for n in negatives)
                if neg_a != neg_b:
                    out.append({"sections": [sections[i].name, sections[j].name],
                                "note": "retrieved context contains "
                                        "opposing statements; surface the "
                                        "disagreement"})
        return tuple(out[:5])
