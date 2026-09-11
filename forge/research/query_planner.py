"""Query planning for the secure research engine.

Turns a free-text research question into a deterministic
:class:`QueryPlan`: a classified intent, normalized search terms,
detected identifiers (error names, library names, dotted paths, URLs),
a set of focused sub-queries, and an ordered list of sources to
consult. Planning is pure (no I/O) so it is fully unit-testable.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

MAX_QUERY = 2000

#: Query intents. They only steer source order and term weighting.
INTENT_ERROR = "error"
INTENT_API = "api"
INTENT_LIBRARY = "library"
INTENT_DOCUMENTATION = "documentation"
INTENT_PROJECT = "project"
INTENT_GENERAL = "general"

#: Source names (must match the ``name`` of the source classes).
SRC_PROJECT_FILES = "project_files"
SRC_LOCAL_DOCS = "local_docs"
SRC_REPO_METADATA = "repository_metadata"
SRC_USER_PROVIDED = "user_provided"
SRC_CONFIGURED_WEB = "configured_web"
SRC_OFFICIAL_DOCS = "official_docs"
SRC_MODEL_KNOWLEDGE = "model_knowledge"

LOCAL_SOURCES: Tuple[str, ...] = (
    SRC_USER_PROVIDED, SRC_PROJECT_FILES, SRC_LOCAL_DOCS, SRC_REPO_METADATA)
WEB_SOURCES: Tuple[str, ...] = (SRC_OFFICIAL_DOCS, SRC_CONFIGURED_WEB)

#: Source order per intent (local first — it is trusted and free).
_SOURCE_ORDER: Dict[str, Tuple[str, ...]] = {
    INTENT_ERROR: (SRC_USER_PROVIDED, SRC_PROJECT_FILES, SRC_LOCAL_DOCS,
                   SRC_OFFICIAL_DOCS, SRC_CONFIGURED_WEB, SRC_REPO_METADATA),
    INTENT_API: (SRC_USER_PROVIDED, SRC_OFFICIAL_DOCS, SRC_LOCAL_DOCS,
                 SRC_PROJECT_FILES, SRC_CONFIGURED_WEB, SRC_REPO_METADATA),
    INTENT_LIBRARY: (SRC_USER_PROVIDED, SRC_REPO_METADATA, SRC_OFFICIAL_DOCS,
                     SRC_PROJECT_FILES, SRC_LOCAL_DOCS, SRC_CONFIGURED_WEB),
    INTENT_DOCUMENTATION: (SRC_USER_PROVIDED, SRC_LOCAL_DOCS,
                           SRC_OFFICIAL_DOCS, SRC_CONFIGURED_WEB,
                           SRC_PROJECT_FILES, SRC_REPO_METADATA),
    INTENT_PROJECT: (SRC_USER_PROVIDED, SRC_PROJECT_FILES, SRC_LOCAL_DOCS,
                     SRC_REPO_METADATA),
    INTENT_GENERAL: (SRC_USER_PROVIDED, SRC_PROJECT_FILES, SRC_LOCAL_DOCS,
                     SRC_REPO_METADATA, SRC_OFFICIAL_DOCS,
                     SRC_CONFIGURED_WEB),
}

_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with",
    "is", "are", "was", "were", "be", "been", "do", "does", "did", "how",
    "what", "why", "when", "where", "which", "who", "can", "i", "we", "you",
    "it", "this", "that", "these", "those", "my", "our", "me", "from",
    "into", "about", "use", "using", "used", "should", "would", "could",
    "get", "got", "fix", "please", "help", "explain", "show", "tell",
    "find", "mean", "means", "there", "here", "any", "some", "not",
    "no", "yes", "at", "by", "as", "if", "so", "up", "out", "vs", "than",
    "then", "them", "they", "their", "its", "have", "has", "had", "work",
    "works", "working", "error", "errors", "issue", "problem", "question",
    "project", "repo", "repository", "code", "file", "files", "function",
    "class", "module", "documentation", "docs", "doc", "api", "library",
})

_ERROR_PATTERN = re.compile(
    r"\b([A-Z][A-Za-z0-9]*(?:Error|Exception|Warning|Fault|Timeout))\b")
_DOTTED = re.compile(r"\b[a-zA-Z_][\w]*(?:\.[a-zA-Z_][\w]*)+\b")
_PATH = re.compile(r"\b[\w./-]+\.(?:py|js|ts|md|rst|toml|yaml|yml|json|txt|cfg|ini)\b")
_URL = re.compile(r"https?://[^\s<>\"']+")
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_\-]{1,}")
_CAMEL = re.compile(r"\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+\b")
_QUOTED = re.compile(r"[\"'`]([^\"'`]{2,80})[\"'`]")

_ERROR_HINTS = ("traceback", "exception", "stack trace", "failed",
                "failing", "crash", "raised", "raises", "error:")
_API_HINTS = ("endpoint", "api", "request", "response", "parameter",
              "signature", "method", "argument", "returns", "call")
_LIBRARY_HINTS = ("library", "package", "pip", "npm", "install", "version",
                  "dependency", "dependencies", "upgrade", "release")
_DOC_HINTS = ("documentation", "docs", "guide", "tutorial", "reference",
              "readme", "how to", "howto", "example")
_PROJECT_HINTS = ("this project", "this repo", "our", "in the repo",
                  "in this repository", "where is", "which file",
                  "defined", "who calls", "imports", "test")


@dataclass
class QueryPlan:
    """Deterministic research plan for one question."""

    question: str
    intent: str
    terms: List[str] = field(default_factory=list)
    identifiers: List[str] = field(default_factory=list)
    error_names: List[str] = field(default_factory=list)
    libraries: List[str] = field(default_factory=list)
    urls: List[str] = field(default_factory=list)
    sub_queries: List[str] = field(default_factory=list)
    source_order: List[str] = field(default_factory=list)
    web_requested: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "question": self.question[:400],
            "intent": self.intent,
            "terms": list(self.terms),
            "identifiers": list(self.identifiers),
            "error_names": list(self.error_names),
            "libraries": list(self.libraries),
            "urls": list(self.urls),
            "sub_queries": list(self.sub_queries),
            "source_order": list(self.source_order),
            "web_requested": self.web_requested,
        }


class QueryPlanner:
    """Pure, deterministic query planner."""

    def __init__(self, known_libraries: Tuple[str, ...] = ()) -> None:
        self.known_libraries = tuple(sorted({lib.lower() for lib in known_libraries}))

    def plan(self, question: str, *, allow_web: bool = True,
             enabled_sources: Tuple[str, ...] = ()) -> QueryPlan:
        if not isinstance(question, str) or not question.strip():
            raise ValueError("question must be a non-empty string")
        if len(question) > MAX_QUERY:
            raise ValueError(f"question exceeds {MAX_QUERY} characters")
        text = question.strip()
        lowered = text.lower()

        urls = _URL.findall(text)[:5]
        stripped = _URL.sub(" ", text)
        error_names = _dedupe(_ERROR_PATTERN.findall(stripped))
        dotted = _dedupe(_DOTTED.findall(stripped))
        paths = _dedupe(_PATH.findall(stripped))
        camel = _dedupe(_CAMEL.findall(stripped))
        quoted = _dedupe(q.strip() for q in _QUOTED.findall(stripped))
        identifiers = _dedupe(error_names + dotted + paths + camel + quoted)[:12]

        words = [w for w in _WORD.findall(stripped)]
        terms: List[str] = []
        for word in words:
            low = word.lower().strip("-_")
            if len(low) < 2 or low in _STOPWORDS or low.isdigit():
                continue
            terms.append(low)
        for ident in identifiers:
            for part in re.split(r"[./]", ident):
                low = part.lower()
                if low and low not in _STOPWORDS and low not in terms:
                    terms.append(low)
        terms = _dedupe(terms)[:16]

        libraries = self._libraries(terms, dotted)
        intent = self._intent(lowered, error_names, libraries, identifiers)
        sub_queries = self._sub_queries(text, intent, error_names,
                                        libraries, identifiers, terms)

        order = [s for s in _SOURCE_ORDER[intent]]
        if not allow_web:
            order = [s for s in order if s not in WEB_SOURCES]
        if enabled_sources:
            order = [s for s in order if s in enabled_sources]
        return QueryPlan(
            question=text, intent=intent, terms=terms,
            identifiers=identifiers, error_names=error_names,
            libraries=libraries, urls=urls, sub_queries=sub_queries,
            source_order=order, web_requested=allow_web)

    # -- helpers ----------------------------------------------------------

    def _libraries(self, terms: List[str], dotted: List[str]) -> List[str]:
        found: List[str] = []
        known = set(self.known_libraries)
        for term in terms:
            if term in known:
                found.append(term)
        for item in dotted:
            head = item.split(".")[0].lower()
            if head in known and head not in found:
                found.append(head)
        return found[:5]

    @staticmethod
    def _intent(lowered: str, error_names: List[str], libraries: List[str],
                identifiers: List[str]) -> str:
        if error_names or any(h in lowered for h in _ERROR_HINTS):
            return INTENT_ERROR
        if any(h in lowered for h in _PROJECT_HINTS):
            return INTENT_PROJECT
        if any(h in lowered for h in _DOC_HINTS):
            return INTENT_DOCUMENTATION
        if any(h in lowered for h in _API_HINTS):
            return INTENT_API
        if libraries or any(h in lowered for h in _LIBRARY_HINTS):
            return INTENT_LIBRARY
        if identifiers:
            return INTENT_PROJECT
        return INTENT_GENERAL

    @staticmethod
    def _sub_queries(text: str, intent: str, error_names: List[str],
                     libraries: List[str], identifiers: List[str],
                     terms: List[str]) -> List[str]:
        queries: List[str] = [text[:200]]
        for name in error_names[:2]:
            queries.append(f"{name} cause and fix")
            for lib in libraries[:1]:
                queries.append(f"{lib} {name}")
        for lib in libraries[:2]:
            queries.append(f"{lib} " + " ".join(
                t for t in terms if t != lib)[:120])
        for ident in identifiers[:3]:
            if ident not in error_names:
                queries.append(ident)
        return _dedupe(q.strip() for q in queries if q.strip())[:6]


def _dedupe(items) -> List[str]:
    seen = set()
    out: List[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out
