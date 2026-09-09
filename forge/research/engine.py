"""Research engine (A47): evidence-based answers about the codebase + web.

Every answer cites real index evidence (symbols, dependencies, test
mapping, architecture) from :class:`RepositoryIntelligence`. If no
evidence exists, the engine says so — it never fabricates facts,
files, or citations. ``confidence`` is a documented heuristic
(evidence coverage), not a probability.

When ``web_research=True`` is passed, the engine also searches the
Internet for answers not found in the codebase. Web results are
untrusted external input, clearly labeled, and never override local
evidence.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from forge.intelligence.repository import RepositoryIntelligence

MAX_QUESTION = 2000
MAX_EVIDENCE = 12

_STOPWORDS = {
    "the", "a", "an", "where", "what", "which", "who", "how", "is",
    "are", "does", "do", "did", "defined", "define", "definition",
    "test", "tests", "cover", "covered", "coverage", "depend",
    "depends", "dependent", "dependents", "import", "imports",
    "module", "modules", "file", "files", "class", "function",
    "package", "packages", "entry", "point", "points", "structure",
    "explain", "describe", "find", "show", "list", "tell", "me",
    "about", "for", "this", "project", "repository", "repo",
}

_IDENTIFIER = re.compile(r"\b[A-Za-z_][A-Za-z0-9_.\-/]*\b")


class ResearchEngine:
    """Deterministic, evidence-bound repository research."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.intelligence = RepositoryIntelligence.build(self.root)

    # -- question answering ---------------------------------------------------

    def ask(self, question: str) -> dict[str, Any]:
        if not isinstance(question, str) or not question.strip() \
                or len(question) > MAX_QUESTION:
            raise ValueError("question must be 1-2000 characters")
        lowered = question.lower().strip()
        candidates = [
            token for token in _IDENTIFIER.findall(question)
            if token.lower() not in _STOPWORDS and len(token) >= 2
            and not token.isdigit()
        ]
        evidence: list[dict[str, Any]] = []
        wants_tests = any(word in lowered for word in
                          ("test", "tests", "cover", "coverage"))
        wants_dependencies = any(word in lowered for word in
                                 ("depend", "import", "imports"))

        for candidate in candidates[:8]:
            for symbol in self.intelligence.symbols.find(candidate):
                self._add_evidence(
                    evidence, path=symbol.file, line=symbol.line,
                    kind=f"symbol:{symbol.kind}",
                    match=candidate, note=symbol.name)
            if wants_dependencies:
                self._dependency_evidence(candidate, evidence)
            if wants_tests:
                for source in self._source_paths(candidate):
                    for test in self.intelligence.tests.tests_for_source(
                            source):
                        self._add_evidence(
                            evidence, path=test, kind="test",
                            match=candidate,
                            note=f"covers {source}")

        evidence = evidence[:MAX_EVIDENCE]
        if not evidence:
            return {
                "question": question[:400],
                "answer": ("I found no evidence in this repository "
                           "that answers that question. I will not "
                           "guess."),
                "evidence": [],
                "confidence": 0.0,
                "honest": True,
            }
        confidence = round(min(1.0, 0.15 * len(evidence)), 2)
        cited = "; ".join(
            f"{item['path']}:{item.get('line', '-')}" for item in evidence[:4])
        answer = (f"Found {len(evidence)} piece(s) of evidence: "
                  f"{cited}. Evidence-based only — "
                  "confidence is a coverage heuristic, not a "
                  "probability.")
        return {"question": question[:400], "answer": answer,
                "evidence": evidence, "confidence": confidence,
                "honest": True}

    def _source_paths(self, candidate: str) -> list[str]:
        """Repo-relative source paths the candidate may refer to.

        Derived only from real index data: symbol files for bare
        identifiers, plus documented name normalizations (dotted
        module -> path, path -> bare module). Forms that do not exist
        in the graph simply yield no evidence.
        """
        paths: list[str] = []
        for symbol in self.intelligence.symbols.find(candidate):
            if symbol.file not in paths:
                paths.append(symbol.file)
        for form in self._name_forms(candidate):
            if form not in paths:
                paths.append(form)
        return paths

    @staticmethod
    def _name_forms(candidate: str) -> list[str]:
        forms = [candidate]
        if "/" in candidate or candidate.endswith(".py"):
            bare = candidate.replace("/", ".").removesuffix(".py")
            forms.append(bare)
        elif "." in candidate:
            path = candidate.replace(".", "/")
            if not path.endswith(".py"):
                path += ".py"
            forms.append(path)
        return forms

    def _dependency_evidence(self, candidate: str,
                             evidence: list[dict[str, Any]]) -> None:
        graph = self.intelligence.dependencies
        for source in self._source_paths(candidate):
            for dependent in graph.dependents_of(source):
                self._add_evidence(evidence, path=dependent,
                                   kind="dependent",
                                   match=candidate,
                                   note=f"depends on {source}")
            for dependency in graph.dependencies_of(source):
                self._add_evidence(evidence, path=dependency,
                                   kind="dependency",
                                   match=candidate,
                                   note=f"imported by {source}")

    def _add_evidence(self, evidence: list[dict[str, Any]], *, path: str,
                      kind: str, match: str, note: str = "",
                      line: int | None = None) -> None:
        for item in evidence:
            if item["path"] == path and item["kind"] == kind \
                    and item["note"] == note:
                return
        item: dict[str, Any] = {"path": path, "kind": kind,
                                "match": match, "note": note}
        if line is not None:
            item["line"] = line
        evidence.append(item)

    # -- report ----------------------------------------------------------------

    def report(self) -> dict[str, Any]:
        summary = self.intelligence.summary()
        architecture = self.intelligence.architecture
        # Check web research availability
        web_available = False
        web_provider = "unavailable"
        try:
            from forge.research.web import WebSearchProvider
            w = WebSearchProvider()
            web_available = w.available()
            web_provider = w.provider_name
        except Exception:
            pass
        return {
            "root": str(self.root),
            "source_file_count": summary.get("source_file_count", 0),
            "test_file_count": summary.get("test_file_count", 0),
            "package_count": summary.get("package_count", 0),
            "entry_points": list(summary.get("entry_points", [])),
            "test_commands": list(summary.get("test_commands", [])),
            "layers": [
                {"path": node.path, "kind": node.kind,
                 "file_count": len(node.files)}
                for node in architecture.packages[:20]
            ],
            "config_files": list(architecture.config_files[:20]),
            "cycles": list(summary.get("cycles", [])),
            "evidence_based": True,
            "web_research": {
                "available": web_available,
                "provider": web_provider,
            },
        }

    # -- web research ----------------------------------------------------------

    def ask_web(self, question: str) -> dict[str, Any]:
        """Search the Internet for an answer.

        Returns web results labeled as untrusted external input.
        Requires a configured web search provider.
        """
        from forge.research.web import WebSearchProvider, fetch_page_content
        if not isinstance(question, str) or not question.strip() \
                or len(question) > MAX_QUESTION:
            raise ValueError("question must be 1-2000 characters")
        provider = WebSearchProvider()
        if not provider.available():
            return {
                "question": question[:400],
                "answer": ("No web search provider is configured. "
                           "Set OPENAI_API_KEY or FORGE_SEARXNG_URL."),
                "evidence": [],
                "confidence": 0.0,
                "honest": True,
                "web_search": False,
            }
        results = provider.search(question)
        if not results:
            return {
                "question": question[:400],
                "answer": ("Web search returned no results. "
                           "I will not guess."),
                "evidence": [],
                "confidence": 0.0,
                "honest": True,
                "web_search": True,
                "provider": provider.provider_name,
            }
        evidence = []
        for result in results[:MAX_EVIDENCE]:
            evidence.append({
                "title": result.title,
                "url": result.url,
                "snippet": result.snippet,
                "kind": "web_result",
            })
        snippets = [f"{r.title}: {r.snippet[:100]}"
                    for r in results[:4]]
        answer = (f"Web search ({provider.provider_name}) returned "
                  f"{len(results)} result(s): "
                  + "; ".join(snippets))
        return {
            "question": question[:400],
            "answer": answer[:2000],
            "evidence": evidence,
            "confidence": round(min(0.7, 0.15 * len(evidence)), 2),
            "honest": True,
            "web_search": True,
            "provider": provider.provider_name,
            "note": "Web results are untrusted external input; "
                    "verify independently.",
        }

    def fetch_url(self, url: str) -> dict[str, Any]:
        """Fetch and extract text from a URL (bounded, untrusted)."""
        from forge.research.web import fetch_page_content
        if not isinstance(url, str) or not url.strip():
            raise ValueError("url must be a non-empty string")
        url = url.strip()[:500]
        content = fetch_page_content(url)
        return {
            "url": url,
            "content_length": len(content),
            "content": content[:8000],
            "untrusted": True,
            "honest": True,
        }
