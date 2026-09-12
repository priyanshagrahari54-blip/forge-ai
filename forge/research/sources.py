"""Research sources: local (trusted) and web (untrusted, SSRF-guarded).

Every source implements :class:`ResearchSource` and returns a
:class:`SourceOutcome` whose ``state`` is explicit. Local sources read
only inside the project root (symlink-safe), skip secrets/binaries and
the ``.forge``/``.git`` runtime directories, and never execute code.
Web sources only contact operator-configured hosts through
``forge.security.ssrf.fetch`` (HTTPS default, redirect revalidation,
timeout, size cap) and label everything they return
``REAL_WEB_RESULT`` — only when bytes were actually downloaded.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from forge.research.config import STDLIB_MODULES, ResearchConfig
from forge.research.provenance import (
    Citation, Provenance, ResearchResult, SourceOutcome,
    STATE_EMPTY, STATE_ERROR, STATE_POLICY_DENIED, STATE_SUCCESS,
    STATE_TIMEOUT, STATE_UNAVAILABLE,
)
from forge.research.query_planner import (
    QueryPlan, SRC_CONFIGURED_WEB, SRC_LOCAL_DOCS, SRC_MODEL_KNOWLEDGE,
    SRC_OFFICIAL_DOCS, SRC_PROJECT_FILES, SRC_REPO_METADATA,
    SRC_USER_PROVIDED,
)
from forge.security.ssrf import (
    FetchOutcome, SSRFError, fetch as ssrf_fetch, ip_blocked_reason,
    parse_and_validate,
)

_SKIP_DIRS = frozenset({
    ".git", ".forge", ".hg", ".svn", "__pycache__", ".venv", "venv", "env",
    "node_modules", ".pytest_cache", ".mypy_cache", ".ruff_cache", "build",
    "dist", ".arena", ".cache", ".idea", ".vscode", "coverage", ".tox",
})
_SKIP_NAMES = frozenset({
    ".env", ".env.local", ".netrc", ".git-credentials", "id_rsa", "id_ed25519",
    "credentials", "credentials.json", "secrets.yaml", "secrets.yml",
})
_SKIP_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".crt", ".der", ".jks",
                  ".sqlite", ".db", ".pyc", ".so", ".dylib", ".dll", ".exe",
                  ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip",
                  ".gz", ".tar", ".whl", ".woff", ".woff2", ".ttf", ".mp3",
                  ".mp4", ".bin")
_SOURCE_SUFFIXES = (".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java",
                    ".rb", ".sh", ".toml", ".yaml", ".yml", ".json", ".cfg",
                    ".ini", ".txt", ".html", ".css", ".sql")
_DOC_SUFFIXES = (".md", ".rst", ".txt", ".adoc")

MAX_FILE_BYTES = 400_000
MAX_FILES_SCANNED = 4000
SNIPPET_RADIUS = 160
_SECRET_LINE = re.compile(
    r"(api[_-]?key|secret|password|token|authorization)\s*[:=]\s*\S{8,}",
    re.IGNORECASE)


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_within(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _iter_files(root: Path, suffixes: Tuple[str, ...], *,
                only_under: Optional[Iterable[str]] = None,
                limit: int = MAX_FILES_SCANNED) -> Iterable[Path]:
    """Walk the tree deterministically, skipping runtime/secret paths."""
    starts: List[Path] = []
    if only_under:
        for rel in only_under:
            candidate = root / rel
            if candidate.exists() and _is_within(root, candidate):
                starts.append(candidate)
    else:
        starts.append(root)
    count = 0
    for start in starts:
        if start.is_file():
            if start.suffix.lower() in suffixes or start.name.upper().startswith("README"):
                yield start
                count += 1
            continue
        stack = [start]
        while stack:
            current = stack.pop()
            try:
                entries = sorted(current.iterdir(), key=lambda p: p.name)
            except OSError:
                continue
            for entry in entries:
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    if entry.name not in _SKIP_DIRS and not entry.name.startswith("."):
                        stack.append(entry)
                    continue
                if entry.name in _SKIP_NAMES or entry.name.startswith(".env"):
                    continue
                low = entry.name.lower()
                if low.endswith(_SKIP_SUFFIXES):
                    continue
                if low.endswith(suffixes) or low.startswith("readme"):
                    yield entry
                    count += 1
                    if count >= limit:
                        return


def _read_text(path: Path) -> str:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return ""
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _score_text(text_lower: str, terms: List[str], identifiers: List[str]
                ) -> Tuple[float, List[str]]:
    matched: List[str] = []
    score = 0.0
    for ident in identifiers:
        if ident.lower() in text_lower:
            matched.append(ident)
            score += 3.0
    for term in terms:
        hits = text_lower.count(term)
        if hits:
            matched.append(term)
            score += min(hits, 5) * 0.6
    return score, matched


def _best_snippet(text: str, needles: List[str]) -> Tuple[str, int]:
    """Return a snippet around the first needle hit plus its 1-based line."""
    lowered = text.lower()
    position = -1
    for needle in needles:
        idx = lowered.find(needle.lower())
        if idx != -1 and (position == -1 or idx < position):
            position = idx
    if position == -1:
        position = 0
    start = max(0, position - SNIPPET_RADIUS)
    end = min(len(text), position + SNIPPET_RADIUS)
    snippet = text[start:end]
    # Never surface a line that looks like a credential assignment.
    snippet = "\n".join(
        line for line in snippet.splitlines()
        if not _SECRET_LINE.search(line))
    line_no = text.count("\n", 0, position) + 1
    return " ".join(snippet.split()), line_no


class ResearchSource:
    """Base source. Subclasses set ``name`` / ``provenance``."""

    name = "abstract"
    provenance = Provenance.LOCAL_SOURCE

    def available(self) -> bool:
        return True

    def search(self, plan: QueryPlan) -> SourceOutcome:  # pragma: no cover
        raise NotImplementedError

    def _outcome(self, results: List[ResearchResult], started: float,
                 *, state: Optional[str] = None, error: str = "") -> SourceOutcome:
        return SourceOutcome(
            source=self.name, provenance=self.provenance,
            state=state or (STATE_SUCCESS if results else STATE_EMPTY),
            results=results, error=error,
            duration_ms=int((time.perf_counter() - started) * 1000))


# ---------------------------------------------------------------------------
# local sources
# ---------------------------------------------------------------------------


class ProjectFilesSource(ResearchSource):
    """Symbols (via the intelligence index when available) + text grep."""

    name = SRC_PROJECT_FILES
    provenance = Provenance.LOCAL_SOURCE

    def __init__(self, root: Path, intelligence: Any = None,
                 max_results: int = 12) -> None:
        self.root = Path(root).resolve()
        self.intelligence = intelligence
        self.max_results = max_results

    def search(self, plan: QueryPlan) -> SourceOutcome:
        started = time.perf_counter()
        results: List[ResearchResult] = []
        seen: set = set()
        if self.intelligence is not None:
            for ident in plan.identifiers + plan.terms[:6]:
                try:
                    symbols = self.intelligence.symbols.find(ident)
                except Exception:
                    symbols = []
                for symbol in symbols[:5]:
                    key = (symbol.file, symbol.line)
                    if key in seen:
                        continue
                    seen.add(key)
                    results.append(ResearchResult(
                        source=self.name, provenance=self.provenance,
                        title=f"{symbol.kind} {symbol.name}",
                        snippet=self._line_at(symbol.file, symbol.line),
                        citation=Citation(locator=symbol.file, line=symbol.line),
                        score=6.0, kind="symbol", matched_terms=[ident]))
        needles = plan.identifiers + plan.terms
        if needles:
            for path in _iter_files(self.root, _SOURCE_SUFFIXES):
                text = _read_text(path)
                if not text:
                    continue
                score, matched = _score_text(text.lower(), plan.terms, plan.identifiers)
                if score <= 0 or not matched:
                    continue
                rel = path.relative_to(self.root).as_posix()
                snippet, line = _best_snippet(text, matched)
                if (rel, line) in seen:
                    continue
                seen.add((rel, line))
                results.append(ResearchResult(
                    source=self.name, provenance=self.provenance,
                    title=rel, snippet=snippet,
                    citation=Citation(locator=rel, line=line),
                    score=score, kind="file", matched_terms=matched[:8]))
        results.sort(key=lambda r: (-r.score, r.citation.locator, r.citation.line or 0))
        return self._outcome(results[:self.max_results], started)

    def _line_at(self, rel: str, line: int) -> str:
        path = self.root / rel
        if not _is_within(self.root, path):
            return ""
        text = _read_text(path)
        lines = text.splitlines()
        if 0 < line <= len(lines):
            window = lines[line - 1:line + 3]
            return " ".join(" ".join(window).split())[:400]
        return ""


class LocalDocsSource(ResearchSource):
    """Markdown/RST docs under the configured documentation paths."""

    name = SRC_LOCAL_DOCS
    provenance = Provenance.LOCAL_SOURCE

    def __init__(self, root: Path, doc_paths: List[str],
                 max_results: int = 8) -> None:
        self.root = Path(root).resolve()
        self.doc_paths = list(doc_paths)
        self.max_results = max_results

    def search(self, plan: QueryPlan) -> SourceOutcome:
        started = time.perf_counter()
        results: List[ResearchResult] = []
        for path in _iter_files(self.root, _DOC_SUFFIXES, only_under=self.doc_paths):
            text = _read_text(path)
            if not text:
                continue
            score, matched = _score_text(text.lower(), plan.terms, plan.identifiers)
            if score <= 0:
                continue
            rel = path.relative_to(self.root).as_posix()
            snippet, line = _best_snippet(text, matched)
            heading = next((ln.lstrip("# ").strip() for ln in text.splitlines()
                            if ln.startswith("#")), rel)
            results.append(ResearchResult(
                source=self.name, provenance=self.provenance,
                title=heading[:120], snippet=snippet,
                citation=Citation(locator=rel, line=line, title=heading[:120]),
                score=score * 1.1, kind="doc", matched_terms=matched[:8]))
        results.sort(key=lambda r: (-r.score, r.citation.locator))
        return self._outcome(results[:self.max_results], started)


class RepositoryMetadataSource(ResearchSource):
    """pyproject/package.json dependencies, entry points, git remotes."""

    name = SRC_REPO_METADATA
    provenance = Provenance.LOCAL_SOURCE

    def __init__(self, root: Path, intelligence: Any = None) -> None:
        self.root = Path(root).resolve()
        self.intelligence = intelligence

    def dependencies(self) -> Dict[str, str]:
        """Declared dependencies -> raw requirement string (no I/O errors)."""
        deps: Dict[str, str] = {}
        pyproject = self.root / "pyproject.toml"
        if pyproject.is_file():
            text = _read_text(pyproject)
            for match in re.finditer(r'"([A-Za-z0-9_.\-]+)\s*([<>=!~][^"]*)?"', text):
                name = match.group(1).lower()
                if name in ("setuptools", "wheel") and "build-system" in text[:match.start()][-300:]:
                    continue
                deps.setdefault(name, (match.group(2) or "").strip())
        for req in ("requirements.txt", "requirements-dev.txt"):
            path = self.root / req
            if path.is_file():
                for line in _read_text(path).splitlines():
                    line = line.strip()
                    if not line or line.startswith(("#", "-")):
                        continue
                    m = re.match(r"([A-Za-z0-9_.\-]+)\s*(.*)", line)
                    if m:
                        deps.setdefault(m.group(1).lower(), m.group(2).strip())
        package_json = self.root / "package.json"
        if package_json.is_file():
            try:
                data = json.loads(_read_text(package_json) or "{}")
            except ValueError:
                data = {}
            for key in ("dependencies", "devDependencies"):
                block = data.get(key) or {}
                if isinstance(block, dict):
                    for name, version in block.items():
                        deps.setdefault(str(name).lower(), str(version))
        return deps

    def facts(self) -> List[Tuple[str, str, str]]:
        """(title, text, locator) triples describing the repository."""
        facts: List[Tuple[str, str, str]] = []
        deps = self.dependencies()
        if deps:
            facts.append(("declared dependencies",
                          ", ".join(f"{k}{v}" for k, v in sorted(deps.items())),
                          "pyproject.toml" if (self.root / "pyproject.toml").is_file()
                          else "requirements.txt"))
        pyproject = self.root / "pyproject.toml"
        if pyproject.is_file():
            text = _read_text(pyproject)
            m = re.search(r'requires-python\s*=\s*"([^"]+)"', text)
            if m:
                facts.append(("requires-python", m.group(1), "pyproject.toml"))
            m = re.search(r'^name\s*=\s*"([^"]+)"', text, re.MULTILINE)
            if m:
                facts.append(("project name", m.group(1), "pyproject.toml"))
            m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
            if m:
                facts.append(("project version", m.group(1), "pyproject.toml"))
            scripts = re.search(r"\[project\.scripts\](.*?)(?:\n\[|\Z)", text, re.DOTALL)
            if scripts:
                facts.append(("console scripts", " ".join(scripts.group(1).split()),
                              "pyproject.toml"))
        git_config = self.root / ".git" / "config"
        if git_config.is_file():
            text = _read_text(git_config)
            for m in re.finditer(r'\[remote "([^"]+)"\]\s*\n\s*url\s*=\s*(\S+)', text):
                url = re.sub(r"//[^@/]+@", "//", m.group(2))  # strip creds
                facts.append((f"git remote {m.group(1)}", url, ".git/config"))
        head = self.root / ".git" / "HEAD"
        if head.is_file():
            ref = _read_text(head).strip()
            if ref.startswith("ref: refs/heads/"):
                facts.append(("git branch", ref[len("ref: refs/heads/"):], ".git/HEAD"))
        if self.intelligence is not None:
            try:
                summary = self.intelligence.summary()
                facts.append(("repository summary",
                              f"{summary.get('source_file_count', 0)} source files, "
                              f"{summary.get('test_file_count', 0)} test files, "
                              f"{summary.get('package_count', 0)} packages; "
                              f"entry points: {', '.join(summary.get('entry_points', [])[:5]) or 'none'}; "
                              f"test commands: {', '.join(summary.get('test_commands', [])[:3]) or 'none'}",
                              "."))
            except Exception as exc:  # summary is optional metadata
                facts.append(("repository summary unavailable",
                              f"{type(exc).__name__}", "."))
        return facts

    def search(self, plan: QueryPlan) -> SourceOutcome:
        started = time.perf_counter()
        results: List[ResearchResult] = []
        for title, text, locator in self.facts():
            haystack = f"{title} {text}".lower()
            score, matched = _score_text(haystack, plan.terms, plan.identifiers)
            if plan.intent == "library" and title == "declared dependencies":
                score += 1.0
            if score <= 0:
                continue
            results.append(ResearchResult(
                source=self.name, provenance=self.provenance, title=title,
                snippet=text[:600], citation=Citation(locator=locator),
                score=score, kind="metadata", matched_terms=matched[:8]))
        results.sort(key=lambda r: (-r.score, r.title))
        return self._outcome(results[:8], started)


class UserProvidedSource(ResearchSource):
    """Notes/files handed in by the operator for this query."""

    name = SRC_USER_PROVIDED
    provenance = Provenance.USER_PROVIDED

    def __init__(self, notes: Iterable[str] = (), files: Iterable[str] = (),
                 root: Optional[Path] = None) -> None:
        self.notes = [n for n in notes if isinstance(n, str) and n.strip()]
        self.files = [f for f in files if isinstance(f, str) and f.strip()]
        self.root = Path(root).resolve() if root else None

    def available(self) -> bool:
        return bool(self.notes or self.files)

    def search(self, plan: QueryPlan) -> SourceOutcome:
        started = time.perf_counter()
        results: List[ResearchResult] = []
        for index, note in enumerate(self.notes[:10], start=1):
            score, matched = _score_text(note.lower(), plan.terms, plan.identifiers)
            results.append(ResearchResult(
                source=self.name, provenance=self.provenance,
                title=f"user note #{index}", snippet=note[:1000],
                citation=Citation(locator=f"user-note:{index}"),
                score=score + 1.0, kind="note", matched_terms=matched[:8]))
        for raw in self.files[:10]:
            path = Path(raw)
            if not path.is_absolute() and self.root is not None:
                path = self.root / raw
            if self.root is not None and not _is_within(self.root, path):
                continue  # never read outside the project when scoped
            if path.name in _SKIP_NAMES or path.suffix.lower() in _SKIP_SUFFIXES:
                continue
            text = _read_text(path) if path.is_file() else ""
            if not text:
                continue
            score, matched = _score_text(text.lower(), plan.terms, plan.identifiers)
            snippet, line = _best_snippet(text, matched or plan.terms)
            results.append(ResearchResult(
                source=self.name, provenance=self.provenance,
                title=path.name, snippet=snippet,
                citation=Citation(locator=str(raw), line=line),
                score=score + 1.0, kind="file", matched_terms=matched[:8]))
        return self._outcome(results, started)


# ---------------------------------------------------------------------------
# web sources
# ---------------------------------------------------------------------------

Fetcher = Callable[..., FetchOutcome]

_TAG_SCRIPT = re.compile(r"<(script|style|noscript|svg)[^>]*>.*?</\1>",
                         re.DOTALL | re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.DOTALL | re.IGNORECASE)
_LINK = re.compile(r'<a\s[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                   re.DOTALL | re.IGNORECASE)


def extract_text(body: bytes, content_type: str) -> Tuple[str, str]:
    """(title, plain text) from an HTML/text/JSON body."""
    raw = body.decode("utf-8", errors="ignore")
    if content_type.startswith("application/json"):
        return "", " ".join(raw.split())[:20000]
    title_match = _TITLE.search(raw)
    title = " ".join(title_match.group(1).split()) if title_match else ""
    text = _TAG_SCRIPT.sub(" ", raw)
    text = _TAG.sub(" ", text)
    return _unescape(title)[:200], " ".join(_unescape(text).split())[:20000]


def _unescape(text: str) -> str:
    import html
    return html.unescape(text.replace("&nbsp;", " "))


def _classify_fetch(outcome: FetchOutcome) -> Tuple[str, str]:
    if outcome.blocked:
        return STATE_POLICY_DENIED, outcome.blocked_reason
    if outcome.ok:
        return STATE_SUCCESS, ""
    error = outcome.error_state or f"http {outcome.status}"
    if "timed out" in error.lower() or "timeout" in error.lower():
        return STATE_TIMEOUT, error
    return STATE_ERROR, error


class _WebSourceBase(ResearchSource):
    provenance = Provenance.REAL_WEB_RESULT

    def __init__(self, config: ResearchConfig, fetcher: Fetcher = ssrf_fetch,
                 audit: Any = None) -> None:
        self.config = config
        self.policy = config.fetch_policy()
        self.fetcher = fetcher
        self.audit = audit

    def available(self) -> bool:
        return bool(self.config.web_enabled)

    def _fetch(self, url: str) -> FetchOutcome:
        """Pre-validate, then fetch through the SSRF-safe chain.

        ``ssrf.fetch`` already performs the full validation (including
        DNS resolution of every address). The pre-check here is defense
        in depth: scheme/host/port/allowlist plus IP-literal
        classification are refused before *any* fetcher — even an
        injected one — sees the URL.
        """
        try:
            _scheme, host, _port, _path = parse_and_validate(url, self.policy)
            literal = host.strip("[]")
            try:
                import ipaddress
                ipaddress.ip_address(literal)
            except ValueError:
                pass
            else:
                reason = ip_blocked_reason(literal)
                if reason:
                    raise SSRFError(f"IP literal {literal} refused ({reason})", url=url)
        except SSRFError as exc:
            return FetchOutcome(ok=False, blocked=True, blocked_reason=exc.reason,
                                final_url=url)
        return self.fetcher(url, policy=self.policy, audit=self.audit)

    def _page_results(self, url: str, plan: QueryPlan, label: str,
                      outcome: FetchOutcome) -> List[ResearchResult]:
        """Turn one *actually fetched* page into results (bytes were read)."""
        title, text = extract_text(outcome.body, outcome.content_type)
        if not text:
            return []
        lowered = text.lower()
        score, matched = _score_text(lowered, plan.terms, plan.identifiers)
        snippet, _ = _best_snippet(text, matched or plan.terms or [text[:20]])
        citation = Citation(locator=url, title=title or label,
                            retrieved_at=_now_iso(),
                            final_url=outcome.final_url or url,
                            status=outcome.status)
        results = [ResearchResult(
            source=self.name, provenance=self.provenance,
            title=title or label, snippet=snippet or text[:400],
            citation=citation, score=score + 0.5, kind="page",
            matched_terms=matched[:8],
            metadata={"bytes_read": outcome.bytes_read,
                      "redirects": outcome.redirects,
                      "content_type": outcome.content_type})]
        # Surface a few relevant outbound links from result pages (not fetched).
        raw = outcome.body.decode("utf-8", errors="ignore")
        for href, inner in _LINK.findall(raw)[:400]:
            text_inner = " ".join(_TAG.sub(" ", inner).split())
            if not text_inner or not href.startswith("https://"):
                continue
            hay = f"{text_inner} {href}".lower()
            link_score, link_matched = _score_text(hay, plan.terms, plan.identifiers)
            if link_score <= 0:
                continue
            results.append(ResearchResult(
                source=self.name, provenance=self.provenance,
                title=text_inner[:120], snippet=f"Linked from {url}: {text_inner[:300]}",
                citation=Citation(locator=href[:500], title=text_inner[:120],
                                  retrieved_at=citation.retrieved_at,
                                  final_url=outcome.final_url or url,
                                  status=outcome.status),
                score=link_score * 0.8, kind="link", matched_terms=link_matched[:8],
                metadata={"listed_on": url, "fetched": False}))
            if len(results) >= 8:
                break
        return results


class ConfiguredWebSource(_WebSourceBase):
    """Operator-configured search templates / pages."""

    name = SRC_CONFIGURED_WEB

    def available(self) -> bool:
        return bool(self.config.web_enabled and self.config.web_sources)

    def search(self, plan: QueryPlan) -> SourceOutcome:
        started = time.perf_counter()
        if not self.config.web_enabled:
            return self._outcome([], started, state=STATE_UNAVAILABLE,
                                 error="web research disabled by configuration")
        if not self.config.web_sources:
            return self._outcome([], started, state=STATE_UNAVAILABLE,
                                 error="no web_sources configured")
        results: List[ResearchResult] = []
        errors: List[str] = []
        worst = STATE_SUCCESS
        attempted = 0
        for source in self.config.web_sources[:6]:
            url = source.render(plan.sub_queries[0] if plan.sub_queries else plan.question)
            attempted += 1
            outcome = self._fetch(url)
            state, error = _classify_fetch(outcome)
            if state != STATE_SUCCESS:
                errors.append(f"{source.name}: {state} {error}".strip())
                worst = _worse(worst, state)
                continue
            results.extend(self._page_results(url, plan, source.name, outcome))
        if results:
            return self._outcome(results, started, error="; ".join(errors)[:400])
        if attempted and errors and worst != STATE_SUCCESS:
            return self._outcome([], started, state=worst, error="; ".join(errors)[:400])
        return self._outcome([], started, state=STATE_EMPTY)


class OfficialDocsSource(_WebSourceBase):
    """Official documentation for libraries detected in the question."""

    name = SRC_OFFICIAL_DOCS

    def _targets(self, plan: QueryPlan) -> List[Tuple[str, str]]:
        targets: List[Tuple[str, str]] = []
        candidates = list(plan.libraries)
        for ident in plan.identifiers:
            head = ident.split(".")[0].lower()
            if head not in candidates:
                candidates.append(head)
        for term in plan.terms:
            if term not in candidates:
                candidates.append(term)
        for name in candidates:
            if name in self.config.official_docs:
                targets.append((name, self.config.official_docs[name]))
            elif name in STDLIB_MODULES:
                targets.append((f"python:{name}",
                                f"https://docs.python.org/3/library/{name}.html"))
            if len(targets) >= 3:
                break
        for url in plan.urls[:2]:
            if url.startswith("https://") and (url.split("/")[2].lower()
                                              in self.policy.host_allowlist):
                targets.append(("question-url", url))
        return targets

    def search(self, plan: QueryPlan) -> SourceOutcome:
        started = time.perf_counter()
        if not self.config.web_enabled:
            return self._outcome([], started, state=STATE_UNAVAILABLE,
                                 error="web research disabled by configuration")
        targets = self._targets(plan)
        if not targets:
            return self._outcome([], started, state=STATE_EMPTY,
                                 error="no known library in question")
        results: List[ResearchResult] = []
        errors: List[str] = []
        worst = STATE_SUCCESS
        for label, url in targets:
            outcome = self._fetch(url)
            state, error = _classify_fetch(outcome)
            if state != STATE_SUCCESS:
                errors.append(f"{label}: {state} {error}".strip())
                worst = _worse(worst, state)
                continue
            for result in self._page_results(url, plan, label, outcome):
                result.metadata["library"] = label
                result.score += 1.0  # official docs outrank generic web
                results.append(result)
        if results:
            return self._outcome(results, started, error="; ".join(errors)[:400])
        if errors:
            return self._outcome([], started, state=worst, error="; ".join(errors)[:400])
        return self._outcome([], started, state=STATE_EMPTY)


_SEVERITY = {STATE_SUCCESS: 0, STATE_EMPTY: 0, STATE_UNAVAILABLE: 1,
             STATE_ERROR: 2, STATE_TIMEOUT: 3, STATE_POLICY_DENIED: 4}


def _worse(a: str, b: str) -> str:
    return b if _SEVERITY.get(b, 2) > _SEVERITY.get(a, 2) else a


# ---------------------------------------------------------------------------
# model knowledge (explicit opt-in only, never a fallback)
# ---------------------------------------------------------------------------


class ModelKnowledgeSource(ResearchSource):
    """Wraps a caller-supplied ``answer(question) -> str`` callable.

    It is consulted only when the operator explicitly asks for model
    knowledge; the engine never runs it as a substitute for failed web
    research. Output is labeled ``MODEL_KNOWLEDGE`` and unverified.
    """

    name = SRC_MODEL_KNOWLEDGE
    provenance = Provenance.MODEL_KNOWLEDGE

    def __init__(self, answer: Optional[Callable[[str], str]] = None,
                 model_name: str = "model") -> None:
        self.answer = answer
        self.model_name = model_name

    def available(self) -> bool:
        return self.answer is not None

    def search(self, plan: QueryPlan) -> SourceOutcome:
        started = time.perf_counter()
        if self.answer is None:
            return self._outcome([], started, state=STATE_UNAVAILABLE,
                                 error="no model configured")
        try:
            text = str(self.answer(plan.question) or "").strip()
        except Exception as exc:
            return self._outcome([], started, state=STATE_ERROR, error=str(exc)[:200])
        if not text:
            return self._outcome([], started, state=STATE_EMPTY)
        return self._outcome([ResearchResult(
            source=self.name, provenance=self.provenance,
            title=f"Model knowledge ({self.model_name}) — unverified",
            snippet=text[:1200],
            citation=Citation(locator=f"model:{self.model_name}"),
            score=0.1, kind="model", metadata={"verified": False})], started)
