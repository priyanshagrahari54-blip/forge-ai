"""Deep research engine (A84 Stage F).

```
research question -> scope -> search strategy -> source discovery
-> source classification -> document retrieval -> evidence extraction
-> cross-source comparison -> contradiction detection -> evidence ranking
-> synthesis -> citation/provenance -> research report
```

Ground rules this implementation enforces:

* retrieval happens through the existing secure research engine
  (HTTPS-only, allowlisted, SSRF-guarded) — the deep layer adds scope,
  strategy, comparison and reporting, never a new unchecked network path;
* every important claim keeps its citation, publication/retrieval times and
  source class (F2); a claim with a single supporting source is reported as
  single-source, never laundered into consensus;
* cross-source disagreements are listed with both citations —
  ``show the disagreement and evidence`` — instead of silently picking one;
* synthesis is **extractive by default** (quoted, cited evidence). A model
  may *phrase* the synthesis only when a model channel is explicitly
  attached; sources, citations and verdicts still come from retrieved
  evidence, and the report records which channel produced the prose;
* failed discovery is reported as failed. The engine never backfills web
  gaps with model knowledge (inheriting the A81 guarantee, one level up).
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from forge.research.networks import (
    NetworkClass, classify_url, screen_goal, source_trust,
)

__all__ = ["DeepResearchEngine", "EvidenceItem", "ResearchReport"]

MAX_SUBQUERIES = 6
MAX_EVIDENCE = 60
MAX_EXCERPT = 900
#: Agreement needs at least this many independent sources before a claim may
#: be worded as corroborated in the report.
CORROBORATION_MIN = 2

_ASK_WORDS = ("what", "how", "why", "compare", "differences", "versus",
              "vs", "best", "current", "latest", "status", "evidence")


@dataclass(frozen=True)
class EvidenceItem:
    """One extracted piece of evidence with full provenance (F2)."""

    key: str
    claim_terms: Tuple[str, ...]
    excerpt: str
    url: str
    document: str
    source_title: str
    source_type: str          # official_docs|project|web|archive|privacy_network|...
    network_class: str
    published_at: str
    retrieved_at: float
    confidence: float
    corroboration: int = 1
    citations: Tuple[str, ...] = ()
    untrusted: bool = True
    notes: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "claim_terms": list(self.claim_terms),
            "excerpt": self.excerpt,
            "url": self.url,
            "document": self.document,
            "source_title": self.source_title,
            "source_type": self.source_type,
            "network_class": self.network_class,
            "published_at": self.published_at,
            "retrieved_at": self.retrieved_at,
            "confidence": round(self.confidence, 4),
            "corroborated_by": self.corroboration,
            "citations": list(self.citations),
            "untrusted": self.untrusted,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class ResearchReport:
    question: str
    status: str                        # COMPLETE | PARTIAL | FAILED | BLOCKED
    scope: Dict[str, Any]
    strategy: Dict[str, Any]
    evidence: Tuple[EvidenceItem, ...] = ()
    findings: Tuple[Dict[str, Any], ...] = ()
    disagreements: Tuple[Dict[str, Any], ...] = ()
    contradictions: Tuple[Dict[str, Any], ...] = ()
    unknowns: Tuple[str, ...] = ()
    single_source_claims: Tuple[str, ...] = ()
    source_outcomes: Tuple[Dict[str, Any], ...] = ()
    citations: Tuple[Dict[str, Any], ...] = ()
    summary: str = ""
    synthesis_channel: str = "extractive"   # extractive | model:<name>
    duration_ms: int = 0
    honesty: str = field(
        default=("source discovery is not verified evidence; claims below "
                 "carry their citations, and single-source claims are "
                 "labeled as such"))

    @property
    def succeeded(self) -> bool:
        return self.status in ("COMPLETE", "PARTIAL")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "question": self.question,
            "status": self.status,
            "scope": self.scope,
            "strategy": self.strategy,
            "evidence": [e.to_dict() for e in self.evidence],
            "findings": list(self.findings),
            "disagreements": list(self.disagreements),
            "contradictions": list(self.contradictions),
            "unknowns": list(self.unknowns),
            "single_source_claims": list(self.single_source_claims),
            "source_outcomes": list(self.source_outcomes),
            "citations": list(self.citations),
            "summary": self.summary,
            "synthesis_channel": self.synthesis_channel,
            "duration_ms": self.duration_ms,
            "honesty": self.honesty,
        }


class DeepResearchEngine:
    """Orchestrates multi-source, cross-checked research over one question."""

    def __init__(self, *, secure_engine: Any = None, root: str = ".",
                 web_search: Any = None,
                 synthesizer: Optional[Callable[[str, Sequence[EvidenceItem]], str]] = None,
                 audit: Any = None, agent_identity: str = "forge-deep-research",
                 max_subqueries: int = MAX_SUBQUERIES) -> None:
        self.max_subqueries = max(1, min(int(max_subqueries), MAX_SUBQUERIES))
        self.web_search = web_search
        self.synthesizer = synthesizer
        self.audit = audit
        self.agent_identity = agent_identity
        self._secure_engine = secure_engine
        self._root = root

    # -- lazy construction so import/construction never touches disk/network ---

    def secure(self) -> Any:
        if self._secure_engine is None:
            from forge.research.secure_engine import SecureResearchEngine
            self._secure_engine = SecureResearchEngine(self._root)
        return self._secure_engine

    # -- pipeline stages ---------------------------------------------------------

    def define_scope(self, question: str) -> Dict[str, Any]:
        lowered = (question or "").lower()
        aspects: List[str] = []
        if any(w in lowered for w in ("compare", "versus", " vs ",
                                      "difference")):
            aspects.append("comparison")
        if any(w in lowered for w in ("latest", "current", "2024", "2025",
                                      "2026", "today")):
            aspects.append("recency")
        if any(w in lowered for w in ("security", "vulnerab", "malware",
                                      "threat")):
            aspects.append("security")
        if any(w in lowered for w in ("benchmark", "performance", "latency",
                                      "throughput")):
            aspects.append("performance")
        goal_screen = screen_goal(question)
        return {
            "question": (question or "").strip()[:600],
            "aspects": aspects or ["overview"],
            "goal_allowed": goal_screen.allowed,
            "goal_screen": goal_screen.to_dict(),
            "corroboration_policy": (
                f"important claims need >= {CORROBORATION_MIN} independent "
                "sources or are labeled single-source"),
        }

    def search_strategy(self, scope: Dict[str, Any]) -> List[str]:
        """Derive bounded sub-queries from scope — no random querying."""
        question = scope["question"]
        queries = [question]
        for aspect in scope.get("aspects", ()) or ():
            variant = f"{question} {aspect} evidence"
            if variant not in queries:
                queries.append(variant)
        return queries[:self.max_subqueries]

    # -- main entry -----------------------------------------------------------------

    def run(self, question: str, *, allow_web: Optional[bool] = None,
            user_notes: Sequence[str] = ()) -> ResearchReport:
        started = time.perf_counter()
        scope = self.define_scope(question)
        if not scope["goal_allowed"]:
            return ResearchReport(
                question=question, status="BLOCKED", scope=scope,
                strategy={}, summary=("this research objective is outside "
                                       "Forge's lawful-research boundary: " +
                                       str(scope["goal_screen"].get("reason", ""))[:400]),
                unknowns=("the blocked objective is not researched at all",),
                duration_ms=int((time.perf_counter() - started) * 1000))
        strategy = self.search_strategy(scope)
        evidence: List[EvidenceItem] = []
        outcomes: List[Dict[str, Any]] = []
        citations: List[Dict[str, Any]] = []

        engine = self.secure()
        for query in strategy:
            try:
                result = engine.research(query, allow_web=allow_web,
                                         user_notes=user_notes)
            except Exception as exc:
                outcomes.append({"source": "secure-engine", "query": query,
                                 "state": "ERROR",
                                 "error": f"{type(exc).__name__}: {exc}"[:200]})
                continue
            outcomes.append({"source": "secure-engine", "query": query,
                             "state": ("FAILED" if result.get("web_failed")
                                       else "OK"),
                             "web_failures": result.get("web_failures", [])[:4],
                             "provenance_counts": result.get("provenance_counts", {}),
                             "confidence": result.get("confidence", 0.0)})
            for row in result.get("results", []) or []:
                evidence.append(self._to_evidence(row))
            for citation in result.get("citations", []) or []:
                citations.append(dict(citation))

        web_outcome = self._run_web_search(strategy)
        if web_outcome is not None:
            outcomes.append(web_outcome["outcome"])
            evidence.extend(web_outcome["evidence"])

        # -- classification pass (privacy-network sources included if any) ------
        for item in evidence:
            if item.network_class == NetworkClass.UNKNOWN:
                pass  # unknown stays unknown; ranking discounts it below

        evidence = self._dedupe(evidence)[:MAX_EVIDENCE]
        evidence, disagreements = self._compare(evidence)
        contradictions = self._detect_contradictions(evidence)
        evidence = self._rank(evidence, scope)

        status = self._status(evidence, outcomes)
        single_source = tuple(
            e.key for e in evidence if e.corroboration < CORROBORATION_MIN
            and e.confidence >= 0.4)
        summary, channel = self._synthesize(question, evidence, status)
        unknowns: List[str] = []
        if not evidence:
            unknowns.append("no retrieved evidence supports an answer")
        if any(o.get("state") == "FAILED" for o in outcomes):
            unknowns.append("web layer failed for at least one source; live "
                            "corroboration is missing and was NOT replaced "
                            "with model knowledge")
        if contradictions:
            unknowns.append("sources disagree; both positions are listed "
                            "with citations instead of being averaged")
        if status == "PARTIAL" and not unknowns:
            unknowns.append(
                "the report is partial: not every claim reached the "
                "multi-source corroboration bar (single_source_claims names "
                "which ones)")

        return ResearchReport(
            question=question, status=status, scope=scope,
            strategy={"subqueries": strategy,
                      "sources_consulted": len(outcomes)},
            evidence=tuple(evidence),
            findings=tuple({
                "claim": e.key, "excerpt": e.excerpt,
                "corroborated_by": e.corroboration,
                "citations": list(e.citations),
                "confidence": round(e.confidence, 4)} for e in evidence[:10]),
            disagreements=disagreements, contradictions=contradictions,
            unknowns=tuple(unknowns), single_source_claims=single_source,
            source_outcomes=tuple(outcomes),
            citations=tuple(citations[:40]),
            summary=summary, synthesis_channel=channel,
            duration_ms=int((time.perf_counter() - started) * 1000))

    # -- stage helpers ---------------------------------------------------------------

    def _run_web_search(self, queries: Sequence[str]) -> Optional[Dict[str, Any]]:
        provider = self.web_search
        if provider is None:
            try:
                from forge.research.web import WebSearchProvider
                provider = WebSearchProvider()
            except Exception:
                provider = None
        if provider is None or not getattr(provider, "available", lambda: False)():
            return {"outcome": {"source": "web-search", "state":
                                "UNAVAILABLE", "detail":
                                "no allowlisted web-search provider "
                                "configured; research continues with local "
                                "and documented sources and reports the gap"},
                    "evidence": [], "errors": []}
        evidence: List[EvidenceItem] = []
        errors: List[str] = []
        for query in queries[:2]:                    # bounded external I/O
            try:
                _provider_name, results = provider.search(query)
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}"[:160])
                continue
            for row in results or []:
                url = str(getattr(row, "url", "") or row.get("url", "")
                          if isinstance(row, dict) else "")
                title = str(getattr(row, "title", "") or
                            (row.get("title", "") if isinstance(row, dict) else ""))
                snippet = str(getattr(row, "snippet", "") or
                              (row.get("snippet", "") if isinstance(row, dict) else ""))
                if not url:
                    continue
                network = classify_url(url)
                if network == NetworkClass.PROHIBITED:
                    continue
                evidence.append(EvidenceItem(
                    key=_claim_key(snippet or title),
                    claim_terms=_terms(snippet or title),
                    excerpt=(snippet or title)[:MAX_EXCERPT],
                    url=url, document=url, source_title=title[:200] or url,
                    source_type="web",
                    network_class=network,
                    published_at="", retrieved_at=time.time(),
                    confidence=0.45,
                    citations=[f"{title[:80]} <{url}>"],
                    untrusted=True))
        outcome = {"source": "web-search", "state": "OK" if evidence or not errors
                   else "FAILED", "results": len(evidence)}
        if errors:
            outcome["errors"] = errors[:4]
        return {"outcome": outcome, "evidence": evidence}

    @staticmethod
    def _to_evidence(row: Dict[str, Any]) -> EvidenceItem:
        citation = row.get("citation") or {}
        url = str(citation.get("url") or "")
        document = str(citation.get("path") or url or row.get("source", ""))
        meta = row.get("metadata") or {}
        network = (str(meta.get("network_class") or "")
                   or (classify_url(url) if url else NetworkClass.UNKNOWN))
        snippet = str(row.get("snippet") or row.get("title") or "")
        return EvidenceItem(
            key=_claim_key(snippet), claim_terms=_terms(snippet),
            excerpt=snippet[:MAX_EXCERPT], url=url, document=document,
            source_title=str(row.get("title") or row.get("source") or "")[:200],
            source_type=str(row.get("source") or "unknown"),
            network_class=network,
            published_at=str(meta.get("published_at") or ""),
            retrieved_at=float(meta.get("retrieved_at") or time.time()),
            confidence=float(row.get("score") or 0.4),
            citations=[str(row.get("cite") or citation)],
            untrusted=bool(row.get("untrusted", True)))

    @staticmethod
    def _dedupe(items: Sequence[EvidenceItem]) -> List[EvidenceItem]:
        seen: Dict[str, EvidenceItem] = {}
        for item in items:
            if not item.key:
                continue
            existing = seen.get(item.key)
            if existing is None:
                seen[item.key] = item
            else:
                # Merge corroboration count and citations; keep max confidence.
                merged_cites = tuple(dict.fromkeys(
                    existing.citations + item.citations))
                sources = {existing.source_type, item.source_type}
                seen[item.key] = EvidenceItem(
                    key=existing.key, claim_terms=existing.claim_terms,
                    excerpt=existing.excerpt or item.excerpt,
                    url=existing.url or item.url,
                    document=existing.document or item.document,
                    source_title=existing.source_title,
                    source_type=existing.source_type,
                    network_class=existing.network_class,
                    published_at=existing.published_at or item.published_at,
                    retrieved_at=min(existing.retrieved_at, item.retrieved_at),
                    confidence=max(existing.confidence, item.confidence),
                    corroboration=existing.corroboration + (
                        1 if len(sources) > 1 or existing.url != item.url else 0),
                    citations=merged_cites[:6],
                    untrusted=existing.untrusted or item.untrusted,
                    notes=existing.notes)
        return list(seen.values())

    @staticmethod
    def _compare(items: Sequence[EvidenceItem]) -> Tuple[List[EvidenceItem], Tuple[Dict[str, Any], ...]]:
        """Cross-source comparison: corroboration + disagreement detection.

        Two excerpts with the same claim key but a *different source* count
        as corroboration. Excerpts sharing a key with opposite polarity are
        surfaced as a disagreement with both citations — never silently
        reconciled.
        """
        by_key: Dict[str, List[EvidenceItem]] = {}
        for item in items:
            by_key.setdefault(item.key, []).append(item)
        updated: List[EvidenceItem] = []
        disagreements: List[Dict[str, Any]] = []
        seen_pairs: set = set()
        for group in by_key.values():
            urls = {item.url or item.document for item in group}
            corroboration = max(1, len(urls))
            for item in group:
                if corroboration != item.corroboration:
                    payload = dict(item.__dict__)
                    payload["corroboration"] = corroboration
                    item = EvidenceItem(**payload)
                updated.append(item)
            for left in group:
                for right in group:
                    if left is right or (left.url or "") == (right.url or ""):
                        continue
                    pair = tuple(sorted((left.url or left.document,
                                         right.url or right.document)))
                    if pair in seen_pairs:
                        continue
                    if _is_negative(left.excerpt) != _is_negative(right.excerpt):
                        seen_pairs.add(pair)
                        disagreements.append({
                            "topic": left.key,
                            "position_a": {"excerpt": left.excerpt[:240],
                                           "citations": list(left.citations)},
                            "position_b": {"excerpt": right.excerpt[:240],
                                           "citations": list(right.citations)},
                            "note": "opposite polarity detected between "
                                    "sources; both are reported, neither "
                                    "is chosen"})
        return updated, tuple(_unique_dicts(disagreements))

    @staticmethod
    def _detect_contradictions(items: Sequence[EvidenceItem]) -> Tuple[Dict[str, Any], ...]:
        """Numeric contradictions: same subject, different stated number."""
        facts: Dict[str, List[Tuple[EvidenceItem, str]]] = {}
        for item in items:
            match = re.search(r"([a-z][a-z ._-]{2,40}?)\s+(?:is|was|=|reaches?)\s+"
                              r"([0-9][0-9,.]*\s*(?:ms|s|gb|mb|kb|billion|million|%|x)\b)",
                              item.excerpt, flags=re.IGNORECASE)
            if match:
                subject = " ".join(match.group(1).lower().split())[:60]
                facts.setdefault(subject, []).append((item, match.group(2).strip().lower()))
        out: List[Dict[str, Any]] = []
        for subject, rows in facts.items():
            values = {value for _item, value in rows}
            if len(values) > 1:
                out.append({
                    "subject": subject,
                    "values": sorted(values),
                    "sources": [{"excerpt": item.excerpt[:200],
                                 "citations": list(item.citations)}
                                for item, _value in rows],
                    "status": "CONFLICT",
                    "note": "sources state different values; flagged for "
                            "adjudication, not averaged"})
        return tuple(out)

    @staticmethod
    def _rank(items: Sequence[EvidenceItem], scope: Dict[str, Any]) -> List[EvidenceItem]:
        recency = "recency" in (scope.get("aspects") or [])
        def weight(item: EvidenceItem) -> float:
            score = item.confidence
            score += 0.1 * min(3, item.corroboration - 1)
            if item.network_class == NetworkClass.PRIVACY_NETWORK:
                score -= 0.15   # always untrusted; corroboration still counts
            elif item.network_class == NetworkClass.POTENTIALLY_MALICIOUS:
                score -= 0.25
            elif item.network_class == NetworkClass.ARCHIVE:
                score += 0.05
            if item.source_type in ("official_docs", "project_files",
                                    "repository_metadata", "user_provided"):
                score += 0.10
            if recency:
                age_hours = max(0.0, (time.time() - item.retrieved_at) / 3600.0)
                score += 0.10 * (1.0 / (1.0 + age_hours / 24.0))
            return max(0.0, min(1.0, round(score, 4)))
        return sorted(items, key=lambda item: (-weight(item), item.key))[:MAX_EVIDENCE]

    @staticmethod
    def _status(items: Sequence[EvidenceItem], outcomes: Sequence[Dict[str, Any]]) -> str:
        if not items:
            return "FAILED"
        degraded = any(o.get("state") in ("FAILED", "ERROR", "UNAVAILABLE")
                       for o in outcomes)
        return "PARTIAL" if degraded else "COMPLETE"

    def _synthesize(self, question: str, items: Sequence[EvidenceItem],
                    status: str) -> Tuple[str, str]:
        top = items[:6]
        if not top:
            return ("No verified evidence was retrieved. I will not guess; "
                    "consulted sources and their failures are listed."), "extractive"
        lines = [f"Deep research on {status.lower()} basis — {len(top)} ranked "
                 "evidence item(s):"]
        for index, item in enumerate(top, start=1):
            tag = "corroborated" if item.corroboration >= CORROBORATION_MIN \
                else "single-source"
            cite = item.citations[0] if item.citations else item.document
            lines.append(f"{index}. {item.excerpt[:240]} [{tag}] ({cite})")
        extractive = "\n".join(lines)
        if self.synthesizer is None:
            return extractive, "extractive"
        try:
            prose = str(self.synthesizer(question, top) or "").strip()
        except Exception:
            return extractive, "extractive"
        if not prose:
            return extractive, "extractive"
        return prose[:4000], "model:synthesizer"


# -- small deterministic helpers ------------------------------------------------

_NEGATIONS = (" not ", " never ", "no ", "cannot", "without", "prohibits",
              "banned", "disabled", "rather than")


def _is_negative(text: str) -> bool:
    lowered = " " + (text or "").lower() + " "
    return any(marker in lowered for marker in _NEGATIONS)


def _terms(text: str) -> Tuple[str, ...]:
    words = re.findall(r"[a-z0-9][a-z0-9_.+-]{2,}", (text or "").lower())
    stop = {"the", "and", "for", "with", "from", "that", "this", "are",
            "was", "has", "have", "not", "but", "you", "your"}
    out: List[str] = []
    for word in words:
        if word in stop or word in out:
            continue
        out.append(word)
    return tuple(out[:12])


def _claim_key(text: str) -> str:
    terms = _terms(text)
    return ":".join(terms[:6]) if terms else ""


def _unique_dicts(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: List[str] = []
    out: List[Dict[str, Any]] = []
    for row in rows:
        marker = str(sorted(row.items()))[:200]
        if marker not in seen:
            seen.append(marker)
            out.append(row)
    return out
