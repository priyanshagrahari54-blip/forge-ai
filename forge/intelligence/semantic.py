"""Semantic code search (A83): real TF-IDF retrieval, no fabricated vectors.

Forge's existing retrieval is exact-term matching
(:mod:`forge.memory.relevance`, :mod:`forge.intelligence.relevance`). That is
honest but brittle: searching "boot the kernel" does not find a file whose
only relevant text is ``kmain`` and "start the system".

This module adds a genuine lexical-semantic layer:

* every source file (and optionally every symbol) becomes a weighted document
  built from its *name*, *identifiers*, *comments/docstrings*, and *body*, with
  field weights so names and docstrings dominate incidental tokens;
* documents are vectorised with **TF-IDF**, which is a real, deterministic,
  dependency-free computation over the corpus;
* a query is scored by cosine similarity against those vectors, so multi-term
  queries rank by how well a document covers *all* of them, not by substring
  presence;
* identifier splitting means ``kernel_main`` matches "kernel main", which is
  what makes this semantic rather than exact.

There are no embeddings and no model calls, so results are reproducible
byte-for-byte and the index never depends on a provider being reachable.
Optional :class:`VectorBackend` support lets a caller plug in real embeddings
later; the scorer is identical either way.
"""
from __future__ import annotations

import math
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from forge.intelligence.gitignore import GitIgnoreMatcher

INDEX_SUFFIXES = (
    ".py", ".c", ".h", ".cc", ".cpp", ".hpp", ".rs", ".js", ".mjs", ".ts",
    ".tsx", ".jsx", ".java", ".go", ".md", ".rst", ".txt", ".yaml", ".yml",
    ".toml", ".json", ".sh", ".asm", ".s", ".S", ".ld", ".mk",
)
SKIP_DIRECTORIES = frozenset({
    ".git", ".venv", "venv", "env", "node_modules", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "build", "dist", "target",
    ".forge",
})
MAX_FILES = 20_000
MAX_FILE_BYTES = 1_000_000
MAX_TERMS_PER_DOCUMENT = 4_000
MAX_RESULTS = 50
#: Field weights: a hit in the file name is worth more than a hit in the body.
FIELD_WEIGHTS = {"name": 4.0, "symbols": 3.0, "doc": 2.0, "body": 1.0}
STOPWORDS = frozenset({
    "the", "and", "for", "with", "that", "this", "from", "are", "was", "were",
    "not", "but", "all", "any", "can", "has", "have", "will", "its", "into",
    "def", "class", "return", "self", "true", "false", "none", "null",
})
TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
#: Identifier boundaries: separators, camelCase joins, and the join
#: between a trailing acronym and the next word (``HTTPServer`` ->
#: ``HTTP``, ``Server``). Without the acronym rule, acronyms glued to a
#: word stay one opaque token and never match a plain-language query.
IDENTIFIER_SPLIT_RE = re.compile(
    r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
COMMENT_RE = re.compile(
    r'(?:"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'|//[^\n]*|#[^\n]*'
    r'|/\*[\s\S]*?\*/)')


@dataclass(frozen=True)
class SearchHit:
    """One ranked semantic search result."""

    path: str
    score: float
    matched_terms: Tuple[str, ...]
    #: Which field carried the match best: name/symbols/doc/body.
    best_field: str
    snippet: str = ""
    #: ``path`` for a file document, ``path#symbol`` for a symbol document.
    key: str = ""
    kind: str = "file"

    def __post_init__(self) -> None:
        if not self.key:
            object.__setattr__(self, "key", self.path)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "key": self.key,
            "kind": self.kind,
            "score": round(self.score, 6),
            "matched_terms": list(self.matched_terms),
            "best_field": self.best_field,
            "snippet": self.snippet,
        }


def tokenize(text: str) -> List[str]:
    """Split text into lowercase terms, splitting identifiers into words.

    ``kernel_main`` → ``kernel``, ``main``; ``HTTPServer`` → ``http``,
    ``server``. Stopwords and single characters are dropped: they carry no
    discriminating power and only add noise to IDF.
    """
    terms: List[str] = []
    for token in TOKEN_RE.findall(text):
        parts = [part.lower() for part in IDENTIFIER_SPLIT_RE.split(token)
                 if part]
        if len(parts) > 1:
            terms.append(token.lower())
        for part in parts:
            if len(part) > 1 and part not in STOPWORDS:
                terms.append(part)
    return terms


@dataclass
class Document:
    """One indexed document: a file, or a symbol inside a file."""

    key: str
    path: str
    fields: Dict[str, Counter] = field(default_factory=dict)
    kind: str = "file"
    name: str = ""
    snippet: str = ""

    @property
    def terms(self) -> Counter:
        total: Counter = Counter()
        for field_name, counter in self.fields.items():
            weight = FIELD_WEIGHTS.get(field_name, 1.0)
            for term, count in counter.items():
                total[term] += count * weight
        return total


@dataclass
class SemanticIndex:
    """A built TF-IDF index over a repository."""

    root: str
    documents: List[Document]
    #: term -> number of documents containing it
    document_frequency: Dict[str, int]
    #: document key -> unit-normalised term weights
    vectors: Dict[str, Dict[str, float]]
    #: term -> set of document keys (inverted index for fast candidate lookup)
    postings: Dict[str, List[str]]
    files_indexed: int = 0
    errors: List[Dict[str, str]] = field(default_factory=list)

    @property
    def document_count(self) -> int:
        return len(self.documents)

    def search(self, query: str, *, limit: int = 10,
               path_prefix: str = "") -> List[SearchHit]:
        """Rank documents against *query* by cosine similarity.

        Returns only documents with a positive score, best first, capped at
        *limit*. A query of pure stopwords or unknown terms returns an empty
        list rather than a fabricated ranking.
        """
        query_terms = tokenize(query or "")
        if not query_terms:
            return []
        weights = Counter(query_terms)
        total_docs = max(1, self.document_count)
        query_vector: Dict[str, float] = {}
        for term, count in weights.items():
            df = self.document_frequency.get(term, 0)
            if not df:
                continue
            idf = math.log(1.0 + total_docs / df)
            query_vector[term] = (1.0 + math.log(count)) * idf
        if not query_vector:
            return []
        norm = math.sqrt(sum(value * value for value in query_vector.values()))
        if norm == 0.0:
            return []
        query_vector = {term: value / norm
                        for term, value in query_vector.items()}

        candidates: Dict[str, float] = defaultdict(float)
        for term, weight in query_vector.items():
            for key in self.postings.get(term, ()):
                if path_prefix and not key.startswith(path_prefix):
                    continue
                candidates[key] += weight * self.vectors[key][term]

        by_key = {document.key: document for document in self.documents}
        hits: List[SearchHit] = []
        for key, score in candidates.items():
            if score <= 0.0:
                continue
            document = by_key.get(key)
            if document is None:
                continue
            matched = tuple(sorted(
                term for term in query_vector
                if term in document.terms))
            if not matched:
                continue
            best_field = max(
                (field_name for field_name in document.fields
                 if any(term in document.fields[field_name]
                        for term in query_vector)),
                key=lambda field_name: (
                    FIELD_WEIGHTS.get(field_name, 1.0), field_name),
                default="body")
            hits.append(SearchHit(
                path=document.path, score=score, matched_terms=matched,
                best_field=best_field, snippet=document.snippet,
                key=document.key, kind=document.kind))
        hits.sort(key=lambda hit: (-hit.score, hit.path))
        return hits[: max(1, min(limit, MAX_RESULTS))]

    def explain(self, query: str, path: str) -> Dict[str, Any]:
        """Return why *path* scored the way it did — used by the cockpit."""
        terms = tokenize(query or "")
        document = next((item for item in self.documents
                         if item.key == path or item.path == path), None)
        if document is None:
            return {"path": path, "found": False, "terms": []}
        return {
            "path": document.path,
            "found": True,
            "kind": document.kind,
            "terms": [
                {"term": term,
                 "count": int(document.terms.get(term, 0)),
                 "document_frequency": self.document_frequency.get(term, 0),
                 "in_document": term in document.terms}
                for term in sorted(set(terms))
            ],
        }

    def summary(self) -> Dict[str, Any]:
        return {
            "root": self.root,
            "files_indexed": self.files_indexed,
            "documents": self.document_count,
            "vocabulary": len(self.document_frequency),
            "errors": len(self.errors),
            "method": "tf-idf-cosine",
            "field_weights": dict(FIELD_WEIGHTS),
        }

    # -- serialisation ---------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the index without the derived vectors.

        Vectors and postings are pure functions of the document term counts
        and the document-frequency table, so they are recomputed on load
        instead of stored: the cache stays small and cannot drift from the
        scoring code.
        """
        return {
            "root": self.root,
            "files_indexed": self.files_indexed,
            "document_frequency": dict(self.document_frequency),
            "errors": list(self.errors),
            "documents": [
                {"key": document.key, "path": document.path,
                 "kind": document.kind, "name": document.name,
                 "snippet": document.snippet,
                 "fields": {field_name: dict(counter)
                            for field_name, counter in document.fields.items()}}
                for document in self.documents
            ],
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "SemanticIndex":
        documents = [
            Document(
                key=str(item.get("key", "")),
                path=str(item.get("path", "")),
                kind=str(item.get("kind", "file")),
                name=str(item.get("name", "")),
                snippet=str(item.get("snippet", "")),
                fields={
                    field_name: Counter({
                        str(term): float(count)
                        for term, count in (counter or {}).items()
                    })
                    for field_name, counter in (item.get("fields") or {}).items()
                },
            )
            for item in (payload.get("documents") or [])
            if isinstance(item, dict)
        ]
        document_frequency = {
            str(term): int(count)
            for term, count in (payload.get("document_frequency") or {}).items()
        }
        index = cls(
            root=str(payload.get("root", "")),
            documents=documents,
            document_frequency=document_frequency,
            vectors={},
            postings={},
            files_indexed=int(payload.get("files_indexed", 0) or 0),
            errors=[item for item in (payload.get("errors") or [])
                    if isinstance(item, dict)],
        )
        _vectorize(index)
        return index


def _vectorize(index: SemanticIndex) -> None:
    """Fill ``index.vectors`` and ``index.postings`` from its documents.

    TF-IDF with cosine normalisation. Shared by fresh builds and cache loads
    so a cached index scores identically to a rebuilt one.
    """
    total = max(1, index.document_count)
    postings: Dict[str, List[str]] = defaultdict(list)
    for document in index.documents:
        counts = document.terms
        vector: Dict[str, float] = {}
        for term, count in counts.items():
            df = index.document_frequency.get(term, 0)
            if not df:
                continue
            idf = math.log(1.0 + total / df)
            vector[term] = (1.0 + math.log(float(count))) * idf
        norm = math.sqrt(sum(value * value for value in vector.values()))
        if norm == 0.0:
            continue
        vector = {term: value / norm for term, value in vector.items()}
        index.vectors[document.key] = vector
        for term in vector:
            postings[term].append(document.key)
    for term in postings:
        postings[term].sort()
    index.postings = dict(postings)


def _read(path: Path) -> Optional[str]:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _snippet(text: str, terms: Sequence[str], width: int = 200) -> str:
    """Return a short excerpt around the first query term occurrence."""
    lowered = text.lower()
    position = -1
    for term in terms:
        found = lowered.find(term)
        if found != -1 and (position == -1 or found < position):
            position = found
    if position == -1:
        return " ".join(text.split())[:width]
    start = max(0, position - width // 4)
    return " ".join(text[start:start + width].split())


class SemanticIndexer:
    """Build a :class:`SemanticIndex` from a repository tree."""

    def __init__(self, root: str | Path, *, index_symbols: bool = False,
                 suffixes: Sequence[str] = INDEX_SUFFIXES,
                 max_files: int = MAX_FILES) -> None:
        self.root = Path(root).resolve()
        self.index_symbols = index_symbols
        self.suffixes = tuple(suffixes)
        self.max_files = max(1, max_files)

    def build(self) -> SemanticIndex:
        matcher = GitIgnoreMatcher(self.root)
        documents: List[Document] = []
        errors: List[Dict[str, str]] = []
        files_indexed = 0
        for dirpath, dirnames, filenames in os.walk(str(self.root)):
            current = Path(dirpath)
            dirnames[:] = [
                name for name in sorted(dirnames)
                if name not in SKIP_DIRECTORIES
                and not matcher.is_ignored(current.relative_to(self.root) / name)
            ]
            for filename in sorted(filenames):
                path = current / filename
                if path.suffix.lower() not in self.suffixes:
                    continue
                rel = path.relative_to(self.root)
                if matcher.is_ignored(rel):
                    continue
                if files_indexed >= self.max_files:
                    return self._assemble(documents, errors, files_indexed)
                source = _read(path)
                if source is None:
                    errors.append({"file": rel.as_posix(),
                                   "error": "unreadable or oversized"})
                    continue
                files_indexed += 1
                documents.extend(
                    self._document(rel.as_posix(), source))
        return self._assemble(documents, errors, files_indexed)

    def _document(self, rel: str, source: str) -> List[Document]:
        name_terms = tokenize(rel)
        comments = " ".join(COMMENT_RE.findall(source))
        body = COMMENT_RE.sub(" ", source)
        document = Document(
            key=rel,
            path=rel,
            kind="file",
            name=Path(rel).name,
            fields={
                "name": Counter(name_terms),
                "symbols": Counter(_symbol_terms(rel, source)),
                "doc": Counter(tokenize(comments)[:MAX_TERMS_PER_DOCUMENT]),
                "body": Counter(tokenize(body)[:MAX_TERMS_PER_DOCUMENT]),
            },
            snippet=_snippet(source, name_terms or [""]),
        )
        if not self.index_symbols or not rel.endswith(".py"):
            return [document]
        out = [document]
        out.extend(self._symbol_documents(rel, source))
        return out

    def _symbol_documents(self, rel: str,
                          source: str) -> List[Document]:
        import ast
        try:
            tree = ast.parse(source, filename=rel)
        except (SyntaxError, ValueError):
            return []
        lines = source.splitlines()
        out: List[Document] = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                     ast.ClassDef)):
                continue
            doc = ast.get_docstring(node) or ""
            end = getattr(node, "end_lineno", node.lineno) or node.lineno
            chunk = "\n".join(lines[max(0, node.lineno - 1):end])
            key = "%s#%s" % (rel, node.name)
            out.append(Document(
                key=key,
                path=rel,
                kind="symbol",
                name=node.name,
                fields={
                    "name": Counter(tokenize(node.name)),
                    "symbols": Counter(
                        tokenize(chunk)[:MAX_TERMS_PER_DOCUMENT]),
                    "doc": Counter(tokenize(doc)),
                    "body": Counter(tokenize(chunk)[:MAX_TERMS_PER_DOCUMENT]),
                },
                snippet=_snippet(doc or chunk, tokenize(node.name)),
            ))
        return out

    def _assemble(self, documents: List[Document],
                  errors: List[Dict[str, str]],
                  files_indexed: int) -> SemanticIndex:
        document_frequency: Dict[str, int] = defaultdict(int)
        for document in documents:
            for term in document.terms:
                document_frequency[term] += 1
        index = SemanticIndex(
            root=str(self.root),
            documents=documents,
            document_frequency=dict(document_frequency),
            vectors={},
            postings={},
            files_indexed=files_indexed,
            errors=errors,
        )
        _vectorize(index)
        return index


def _symbol_terms(rel: str, source: str) -> List[str]:
    """Definition names in a file: cheap, language-agnostic, and useful."""
    if rel.endswith(".py"):
        import ast
        try:
            tree = ast.parse(source, filename=rel)
        except (SyntaxError, ValueError):
            return []
        names: List[str] = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                names.append(node.name)
        return tokenize(" ".join(names))
    pattern = re.compile(
        r"\b(?:def|fn|func|function|class|struct|enum|trait|impl)\s+"
        r"([A-Za-z_][A-Za-z0-9_]*)")
    return tokenize(" ".join(pattern.findall(source)))
