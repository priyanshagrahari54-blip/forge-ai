"""Secure research engine: plan → sources → rank → dedupe → summarize.

The engine coordinates the sources in :mod:`forge.research.sources`
under an explicit provenance contract:

* every result is labeled ``LOCAL_SOURCE`` / ``REAL_WEB_RESULT`` /
  ``USER_PROVIDED`` / ``MODEL_KNOWLEDGE``;
* a failed web source is reported as failed (state + reason) — the
  engine **never** substitutes model knowledge for a failed or empty
  web lookup. Model knowledge appears only when the caller explicitly
  passes ``allow_model_knowledge=True`` *and* a model callable, and it
  is always labeled and ranked below verified evidence;
* summaries are extractive — built only from returned snippets — and
  every sentence maps to a citation index, so nothing in the summary
  lacks a source.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from forge.research.cache import ResearchCache, cache_key
from forge.research.config import (
    CACHE_RELATIVE_PATH, ResearchConfig, load_research_config,
)
from forge.research.provenance import (
    Provenance, ResearchResult, SourceOutcome, STATE_CACHED, STATE_SKIPPED,
    STATE_SUCCESS, STATE_UNAVAILABLE, normalize_text,
)
from forge.research.query_planner import (
    QueryPlan, QueryPlanner, SRC_MODEL_KNOWLEDGE, WEB_SOURCES,
)
from forge.research.sources import (
    ConfiguredWebSource, LocalDocsSource, ModelKnowledgeSource,
    OfficialDocsSource, ProjectFilesSource, RepositoryMetadataSource,
    ResearchSource, UserProvidedSource,
)

#: Provenance weight used by the ranker (trusted local evidence first).
_PROVENANCE_WEIGHT = {
    Provenance.LOCAL_SOURCE: 1.0,
    Provenance.USER_PROVIDED: 0.95,
    Provenance.REAL_WEB_RESULT: 0.8,
    Provenance.MODEL_KNOWLEDGE: 0.2,
}
_SOURCE_BONUS = {
    "official_docs": 0.6,
    "local_docs": 0.3,
    "repository_metadata": 0.2,
}
_KIND_BONUS = {"symbol": 0.5, "doc": 0.2, "page": 0.3, "link": -0.4,
               "model": -1.0}

DEFAULT_MAX_RESULTS = 12
MAX_SUMMARY_SENTENCES = 6


class SecureResearchEngine:
    """Provenance-tracked, SSRF-guarded, cached research over many sources."""

    def __init__(self, root: str | Path = ".", *,
                 config: Optional[ResearchConfig] = None,
                 intelligence: Any = None,
                 fetcher: Optional[Callable[..., Any]] = None,
                 cache: Optional[ResearchCache] = None,
                 cache_dir: Optional[str | Path] = None,
                 model_answer: Optional[Callable[[str], str]] = None,
                 model_name: str = "model",
                 audit: Any = None,
                 build_intelligence: bool = True) -> None:
        self.root = Path(root).resolve()
        self.config = config or load_research_config(self.root)
        self.audit = audit
        if intelligence is None and build_intelligence:
            try:
                from forge.intelligence.repository import RepositoryIntelligence
                intelligence = RepositoryIntelligence.build(self.root)
            except Exception:
                intelligence = None
        self.intelligence = intelligence
        if cache is None:
            directory = cache_dir
            if directory is None and self.config.cache_ttl_seconds > 0:
                directory = self.root / CACHE_RELATIVE_PATH
            cache = ResearchCache(directory,
                                  ttl_seconds=self.config.cache_ttl_seconds,
                                  max_entries=self.config.cache_max_entries)
        self.cache = cache
        self._fetcher = fetcher
        self._metadata = RepositoryMetadataSource(self.root, intelligence)
        known = tuple(self._metadata.dependencies().keys()) + tuple(
            self.config.official_docs.keys())
        self.planner = QueryPlanner(known_libraries=known)
        self.model_source = ModelKnowledgeSource(model_answer, model_name)
        self._sources: Dict[str, ResearchSource] = self._build_sources()

    # -- construction -----------------------------------------------------

    def _build_sources(self) -> Dict[str, ResearchSource]:
        from forge.security.ssrf import fetch as ssrf_fetch
        fetcher = self._fetcher or ssrf_fetch
        sources: List[ResearchSource] = [
            ProjectFilesSource(self.root, self.intelligence,
                               max_results=self.config.max_results),
            LocalDocsSource(self.root, self.config.doc_paths),
            self._metadata,
            OfficialDocsSource(self.config, fetcher=fetcher, audit=self.audit),
            ConfiguredWebSource(self.config, fetcher=fetcher, audit=self.audit),
        ]
        return {source.name: source for source in sources}

    def sources(self) -> Dict[str, ResearchSource]:
        return dict(self._sources)

    # -- planning -----------------------------------------------------------

    def plan(self, question: str, *, allow_web: Optional[bool] = None,
             sources: Iterable[str] = ()) -> QueryPlan:
        web = self.config.web_enabled if allow_web is None else (
            allow_web and self.config.web_enabled)
        return self.planner.plan(question, allow_web=web,
                                 enabled_sources=tuple(sources))

    # -- main entry -----------------------------------------------------------

    def research(self, question: str, *,
                 allow_web: Optional[bool] = None,
                 sources: Iterable[str] = (),
                 user_notes: Iterable[str] = (),
                 user_files: Iterable[str] = (),
                 allow_model_knowledge: Optional[bool] = None,
                 max_results: Optional[int] = None,
                 use_cache: bool = True) -> Dict[str, Any]:
        started = time.perf_counter()
        plan = self.plan(question, allow_web=allow_web, sources=sources)
        limit = max(1, min(int(max_results or self.config.max_results), 50))
        user_source = UserProvidedSource(user_notes, user_files, root=self.root)
        outcomes: List[SourceOutcome] = []

        for name in plan.source_order:
            if name == user_source.name:
                if user_source.available():
                    outcomes.append(user_source.search(plan))
                continue
            source = self._sources.get(name)
            if source is None:
                continue
            if not source.available():
                outcomes.append(SourceOutcome(
                    source=name, provenance=source.provenance,
                    state=STATE_UNAVAILABLE, attempted=False,
                    error="source not available/configured"))
                continue
            outcomes.append(self._run_source(source, plan, use_cache))

        # Model knowledge: explicit opt-in only, never a fallback for web.
        want_model = (self.config.allow_model_knowledge
                      if allow_model_knowledge is None else allow_model_knowledge)
        if want_model and self.model_source.available():
            outcomes.append(self.model_source.search(plan))
        elif want_model:
            outcomes.append(SourceOutcome(
                source=SRC_MODEL_KNOWLEDGE, provenance=Provenance.MODEL_KNOWLEDGE,
                state=STATE_UNAVAILABLE, attempted=False,
                error="model knowledge requested but no model configured"))
        else:
            outcomes.append(SourceOutcome(
                source=SRC_MODEL_KNOWLEDGE, provenance=Provenance.MODEL_KNOWLEDGE,
                state=STATE_SKIPPED, attempted=False,
                error="not requested (never used as a fallback)"))

        merged = [r for o in outcomes for r in o.results]
        ranked = rank_results(merged, plan)
        deduped = deduplicate(ranked)[:limit]
        summary, citations = summarize(deduped, plan)
        web_failures = [o for o in outcomes
                        if o.source in WEB_SOURCES and o.failed]
        web_attempted = [o for o in outcomes
                         if o.source in WEB_SOURCES and o.attempted]
        provenance_counts = {p.value: 0 for p in Provenance}
        for result in deduped:
            provenance_counts[result.provenance.value] += 1

        answer = self._answer_text(deduped, summary, web_failures, web_attempted, plan)
        return {
            "question": plan.question[:400],
            "plan": plan.to_dict(),
            "answer": answer,
            "summary": summary,
            "citations": citations,
            "results": [r.to_dict() for r in deduped],
            "sources": [o.to_dict() for o in outcomes],
            "provenance_counts": provenance_counts,
            "web_failed": bool(web_failures),
            "web_failures": [
                {"source": o.source, "state": o.state, "error": o.error[:300]}
                for o in web_failures],
            "model_knowledge_used": provenance_counts[Provenance.MODEL_KNOWLEDGE.value] > 0,
            "confidence": _confidence(deduped),
            "honest": True,
            "cache": self.cache.stats(),
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "note": ("Every result carries explicit provenance. Web and model "
                     "content is untrusted; failed web research is reported, "
                     "never replaced by model knowledge."),
        }

    # -- per-source run with cache ------------------------------------------

    def _run_source(self, source: ResearchSource, plan: QueryPlan,
                    use_cache: bool) -> SourceOutcome:
        cacheable = source.name in WEB_SOURCES
        key = cache_key(source.name, plan.question,
                        extra="|".join(plan.identifiers + plan.libraries))
        if cacheable and use_cache:
            cached = self.cache.get(key)
            if cached is not None:
                cached.state = STATE_CACHED
                return cached
        try:
            outcome = source.search(plan)
        except Exception as exc:  # a broken source must not sink the query
            return SourceOutcome(source=source.name, provenance=source.provenance,
                                 state="ERROR", error=f"{type(exc).__name__}: {exc}"[:300])
        if cacheable and use_cache and outcome.state == STATE_SUCCESS:
            self.cache.put(key, outcome)
        return outcome

    # -- rendering ------------------------------------------------------------

    @staticmethod
    def _answer_text(results: List[ResearchResult], summary: List[Dict[str, Any]],
                     web_failures: List[SourceOutcome],
                     web_attempted: List[SourceOutcome], plan: QueryPlan) -> str:
        parts: List[str] = []
        if not results:
            parts.append("No evidence found for this question in the consulted sources. "
                         "I will not guess.")
        else:
            verified = [r for r in results if r.provenance.verified]
            parts.append(f"Found {len(results)} result(s), {len(verified)} verified "
                         f"(local/web/user); top citation: {results[0].citation.render()}.")
            if summary:
                parts.append(" ".join(f"{s['text']} [{s['cite']}]" for s in summary[:3]))
        if web_failures:
            names = ", ".join(f"{o.source}={o.state}" for o in web_failures)
            parts.append(f"Web research failed ({names}); results above exclude "
                         "live web evidence and were NOT backfilled with model knowledge.")
        elif not web_attempted:
            parts.append("Web research was not attempted (disabled, unconfigured, "
                         "or local-only query).")
        return " ".join(parts)[:2000]

    # -- introspection ------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        return {
            "root": str(self.root),
            "config": self.config.to_dict(),
            "sources": {
                name: {"available": src.available(),
                       "provenance": src.provenance.value}
                for name, src in self._sources.items()},
            "model_knowledge": {
                "configured": self.model_source.available(),
                "allowed_by_config": self.config.allow_model_knowledge,
                "policy": "explicit opt-in only; never a fallback for web failures",
            },
            "cache": self.cache.stats(),
            "intelligence_indexed": self.intelligence is not None,
            "known_libraries": list(self.planner.known_libraries)[:50],
            "security": {
                "https_only": not self.config.allow_http,
                "host_allowlist": list(self.config.fetch_policy().host_allowlist),
                "timeout_seconds": self.config.timeout_seconds,
                "max_bytes": self.config.max_bytes,
                "max_redirects": self.config.max_redirects,
                "blocked": ["localhost", "loopback", "private", "link-local",
                            "cloud-metadata", "internal-suffixes",
                            "local-service-ports", "embedded-credentials"],
            },
        }


# ---------------------------------------------------------------------------
# ranking / dedup / summarization (pure functions)
# ---------------------------------------------------------------------------


def rank_results(results: List[ResearchResult], plan: QueryPlan) -> List[ResearchResult]:
    """Deterministic ranking: relevance × provenance weight + bonuses."""
    scored: List[Tuple[float, int, ResearchResult]] = []
    for index, result in enumerate(results):
        relevance = max(result.score, 0.0)
        weight = _PROVENANCE_WEIGHT.get(result.provenance, 0.5)
        bonus = _SOURCE_BONUS.get(result.source, 0.0) + _KIND_BONUS.get(result.kind, 0.0)
        if plan.intent == "error" and any(
                e.lower() in (result.snippet + result.title).lower()
                for e in plan.error_names):
            bonus += 1.0
        if plan.intent == "project" and result.provenance == Provenance.LOCAL_SOURCE:
            bonus += 0.5
        final = relevance * weight + bonus
        result.metadata["rank_score"] = round(final, 4)
        scored.append((final, index, result))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [r for _, _, r in scored]


def deduplicate(results: List[ResearchResult]) -> List[ResearchResult]:
    """Drop duplicate locators and near-identical snippets (keeps best).

    Deduplication is scoped per provenance class: the same fact seen in
    a local file *and* in a user note (or a web page) is corroboration,
    not duplication, and both citations are kept.
    """
    seen_locators: set = set()
    seen_fingerprints: set = set()
    seen_norm: Dict[Provenance, List[str]] = {}
    out: List[ResearchResult] = []
    for result in results:
        prov = result.provenance
        locator_key = (prov, result.citation.locator, result.citation.line)
        if locator_key in seen_locators:
            continue
        fp = (prov, result.fingerprint)
        if fp in seen_fingerprints:
            continue
        norm = normalize_text(result.snippet)[:200]
        previous = seen_norm.setdefault(prov, [])
        if norm and any(norm == prev or (len(norm) > 60 and norm in prev)
                        for prev in previous):
            continue
        seen_locators.add(locator_key)
        seen_fingerprints.add(fp)
        previous.append(norm)
        out.append(result)
    return out


def summarize(results: List[ResearchResult], plan: QueryPlan,
              max_sentences: int = MAX_SUMMARY_SENTENCES
              ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Extractive summary: sentences quoted from results, each cited.

    Returns ``(summary, citations)`` where each summary entry is
    ``{"text", "cite", "citation_index", "provenance"}`` and citations
    are numbered ``[1]``..``[n]`` in result order.
    """
    citations: List[Dict[str, Any]] = []
    for index, result in enumerate(results, start=1):
        entry = result.citation.to_dict()
        entry.update({"index": index, "provenance": result.provenance.value,
                      "source": result.source, "cite": result.citation.render()})
        citations.append(entry)
    summary: List[Dict[str, Any]] = []
    needles = [t.lower() for t in plan.identifiers + plan.terms]
    for index, result in enumerate(results, start=1):
        if len(summary) >= max_sentences:
            break
        sentence = _pick_sentence(result.snippet, needles)
        if not sentence:
            continue
        summary.append({
            "text": sentence[:300],
            "cite": f"[{index}] {result.citation.render()}",
            "citation_index": index,
            "provenance": result.provenance.value,
            "verified": result.provenance.verified,
        })
    return summary, citations


def _pick_sentence(text: str, needles: List[str]) -> str:
    import re
    pieces = [p.strip() for p in re.split(r"(?<=[.!?])\s+|\n+", text or "") if p.strip()]
    if not pieces:
        return ""
    for piece in pieces:
        low = piece.lower()
        if any(n in low for n in needles) and len(piece) >= 20:
            return piece
    return pieces[0]


def _confidence(results: List[ResearchResult]) -> float:
    """Coverage heuristic (documented, not a probability)."""
    if not results:
        return 0.0
    verified = sum(1 for r in results if r.provenance.verified)
    local = sum(1 for r in results if r.provenance.trusted)
    return round(min(1.0, 0.12 * verified + 0.05 * local), 2)
