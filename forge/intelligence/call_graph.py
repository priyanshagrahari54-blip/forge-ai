"""Call graphs (A83): real call edges with honest resolution status.

Two tiers, and every edge says which one it came from:

* **``ast``** — Python files are parsed with :mod:`ast`. Each call site is
  resolved against the repository's own function/method names. Resolution is
  reported per edge: ``resolved`` (exactly one candidate), ``ambiguous``
  (several same-named candidates; the candidates are listed, nothing is
  guessed), or ``external`` (no repository symbol with that name).
* **``lexical``** — C/C++/Rust/JS/TS files get a conservative lexical scan
  for ``identifier(`` call sites. These edges are candidates, labelled as
  such, because a lexical scan cannot resolve overloads, macros, or function
  pointers. They are still useful for "what might touch this function".

Nothing here invents an edge it did not see in the source, and nothing
reports a resolution it did not prove.
"""
from __future__ import annotations

import ast
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from forge.intelligence.gitignore import GitIgnoreMatcher

#: Languages handled by the AST tier.
AST_SUFFIXES = (".py",)
#: Languages handled by the lexical tier.
LEXICAL_SUFFIXES = (".c", ".h", ".cc", ".cpp", ".hpp", ".rs", ".js", ".mjs",
                    ".ts", ".java", ".go", ".m", ".swift")
#: Words that look like a call but are not one.
NON_CALL_KEYWORDS = frozenset({
    "if", "for", "while", "switch", "catch", "return", "sizeof", "typeof",
    "defined", "do", "else", "case", "in", "not", "and", "or", "lambda",
    "print", "except", "with", "assert", "yield", "await", "async", "new",
    "delete", "struct", "enum", "union", "typedef", "static", "const",
    "extern", "inline", "register", "volatile", "goto",
})
MAX_FILES = 20_000
MAX_CALL_SITES_PER_FILE = 4_000
#: Global bound. Reaching it is recorded in ``CallGraph.truncated`` so a
#: truncated graph never looks like a complete one.
MAX_CALL_SITES = 100_000
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_DEPTH = 64

CALL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")


@dataclass(frozen=True)
class CallSite:
    """One call edge: who called what, and how much we trust it."""

    caller: str
    callee: str
    file: str
    line: int
    #: ``ast`` or ``lexical`` — which tier produced this edge.
    tier: str
    #: ``resolved``, ``ambiguous``, ``external``, or ``lexical``.
    status: str
    #: Qualified candidates when the callee name matched repository symbols.
    candidates: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, object]:
        return {
            "caller": self.caller,
            "callee": self.callee,
            "file": self.file,
            "line": self.line,
            "tier": self.tier,
            "status": self.status,
            "candidates": list(self.candidates),
        }


@dataclass
class CallGraph:
    """Directed call graph over repository symbols."""

    call_sites: List[CallSite] = field(default_factory=list)
    #: Every function-like symbol the resolver knew about, by bare name.
    known_symbols: Dict[str, Tuple[str, ...]] = field(default_factory=dict)
    files_scanned: int = 0
    files_parsed: int = 0
    #: True when a bound was hit and the graph is therefore incomplete.
    truncated: bool = False
    parse_errors: List[Dict[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._by_caller: Dict[str, List[CallSite]] = defaultdict(list)
        self._by_callee: Dict[str, List[CallSite]] = defaultdict(list)
        # Index the seed sites directly. Calling ``add`` here would append to
        # the very list being iterated and never terminate.
        for site in self.call_sites:
            self._by_caller[site.caller].append(site)
            for target in self._index_keys(site):
                self._by_callee[target].append(site)

    @staticmethod
    def _index_keys(site: "CallSite") -> Tuple[str, ...]:
        """Every key a call site must be reachable by.

        A site is indexed under the bare callee name (so ``callers_of(
        "validate_profile")`` works), under its qualified form when the call
        was qualified (``module.func``), and under each resolved candidate.
        Indexing only the resolved target would make the common bare-name
        lookup miss every resolved edge.
        """
        # Performance optimization (Bolt ⚡): Fast path for call sites with no candidates
        # to avoid list allocation and linear deduplication loops on common single-callee sites.
        if not site.candidates:
            return (site.callee,) if site.callee else ()

        keys = [site.callee]
        keys.extend(site.candidates)
        seen: List[str] = []
        for key in keys:
            if key and key not in seen:
                seen.append(key)
        return tuple(seen)

    def add(self, site: CallSite) -> None:
        self.call_sites.append(site)
        self._by_caller[site.caller].append(site)
        for target in self._index_keys(site):
            self._by_callee[target].append(site)

    def calls_from(self, caller: str) -> List[CallSite]:
        return list(self._by_caller.get(caller, ()))

    def calls_to(self, callee: str) -> List[CallSite]:
        """Call sites whose callee *name or qualified name* matches."""
        direct = list(self._by_callee.get(callee, ()))
        bare = callee.rsplit(".", 1)[-1]
        if bare != callee:
            direct.extend(self._by_callee.get(bare, ()))
        return direct

    def callers_of(self, callee: str) -> List[str]:
        return sorted({site.caller for site in self.calls_to(callee)})

    def callees_of(self, caller: str) -> List[str]:
        return sorted({site.callee for site in self.calls_from(caller)})

    def transitive_callers(self, callee: str, *,
                           max_depth: int = MAX_DEPTH) -> List[str]:
        """Every symbol that reaches *callee*, directly or through others."""
        depth = min(max(1, max_depth), MAX_DEPTH)
        seen: Set[str] = set()
        frontier = [callee]
        for _ in range(depth):
            next_frontier: List[str] = []
            for current in frontier:
                for caller in self.callers_of(current):
                    if caller not in seen and caller != callee:
                        seen.add(caller)
                        next_frontier.append(caller)
            if not next_frontier:
                break
            frontier = next_frontier
        return sorted(seen)

    def transitive_callees(self, caller: str, *,
                           max_depth: int = MAX_DEPTH) -> List[str]:
        depth = min(max(1, max_depth), MAX_DEPTH)
        seen: Set[str] = set()
        frontier = [caller]
        for _ in range(depth):
            next_frontier = []
            for current in frontier:
                for callee in self.callees_of(current):
                    if callee not in seen and callee != caller:
                        seen.add(callee)
                        next_frontier.append(callee)
            if not next_frontier:
                break
            frontier = next_frontier
        return sorted(seen)

    def summary(self) -> Dict[str, object]:
        by_status: Dict[str, int] = defaultdict(int)
        by_tier: Dict[str, int] = defaultdict(int)
        for site in self.call_sites:
            by_status[site.status] += 1
            by_tier[site.tier] += 1
        return {
            "files_scanned": self.files_scanned,
            "files_parsed": self.files_parsed,
            "call_sites": len(self.call_sites),
            "known_symbols": len(self.known_symbols),
            "by_status": {key: by_status[key] for key in sorted(by_status)},
            "by_tier": {key: by_tier[key] for key in sorted(by_tier)},
            "parse_errors": len(self.parse_errors),
            "truncated": self.truncated,
        }

    def to_dict(self) -> Dict[str, object]:
        return {
            "summary": self.summary(),
            "call_sites": [site.to_dict() for site in self.call_sites],
            "known_symbols": {key: list(self.known_symbols[key])
                              for key in sorted(self.known_symbols)},
            "parse_errors": list(self.parse_errors),
            "truncated": self.truncated,
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "CallGraph":
        """Rebuild a graph from :meth:`to_dict` output (no re-parsing)."""
        sites = [
            CallSite(
                caller=str(item.get("caller", "")),
                callee=str(item.get("callee", "")),
                file=str(item.get("file", "")),
                line=int(item.get("line", 0) or 0),
                tier=str(item.get("tier", "lexical")),
                status=str(item.get("status", "lexical")),
                candidates=tuple(item.get("candidates") or ()),
            )
            for item in (payload.get("call_sites") or [])
            if isinstance(item, dict)
        ]
        known = {
            str(key): tuple(str(value) for value in values)
            for key, values in (payload.get("known_symbols") or {}).items()
            if isinstance(values, (list, tuple))
        }
        errors = [item for item in (payload.get("parse_errors") or [])
                  if isinstance(item, dict)]
        summary = payload.get("summary") or {}
        return cls(
            call_sites=sites,
            known_symbols=known,
            files_scanned=int(summary.get("files_scanned", 0) or 0),
            files_parsed=int(summary.get("files_parsed", 0) or 0),
            truncated=bool(summary.get("truncated", False)),
            parse_errors=errors,
        )


IGNORED_DIRECTORIES = frozenset({
    ".git", ".venv", "venv", "env", "node_modules", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "build", "dist", "target",
    ".forge",
})


def _iter_source_files(root: Path,
                       suffixes: Sequence[str]) -> Iterable[Path]:
    """Yield source files under *root*, honouring .gitignore and skip dirs."""
    matcher = GitIgnoreMatcher(root)
    for dirpath, dirnames, filenames in os.walk(str(root)):
        current = Path(dirpath)
        dirnames[:] = [
            name for name in sorted(dirnames)
            if name not in IGNORED_DIRECTORIES
            and not matcher.is_ignored(current.relative_to(root) / name)
        ]
        for filename in sorted(filenames):
            if Path(filename).suffix.lower() not in suffixes:
                continue
            path = current / filename
            if matcher.is_ignored(path.relative_to(root)):
                continue
            yield path


class CallGraphIndexer:
    """Build a :class:`CallGraph` for a repository."""

    def __init__(self, root: str | Path, *,
                 include_lexical: bool = True,
                 max_files: int = MAX_FILES) -> None:
        self.root = Path(root).resolve()
        self.include_lexical = include_lexical
        self.max_files = max(1, max_files)

    def build(self) -> CallGraph:
        # Performance optimization (Bolt ⚡): Cache file reads and parsed AST trees
        # in a single pass to eliminate 100% of duplicate filesystem I/O syscalls,
        # relative path operations, and ast.parse overhead between Pass 1 and Pass 2.
        graph = CallGraph()
        suffixes = AST_SUFFIXES + (LEXICAL_SUFFIXES if self.include_lexical
                                   else ())
        files: List[Path] = []
        for path in _iter_source_files(self.root, suffixes):
            files.append(path)
            if len(files) >= self.max_files:
                graph.truncated = True
                break

        definitions: Dict[str, Set[str]] = defaultdict(set)
        cached_files: List[Tuple[str, Path, Optional[str], Optional[ast.AST], Optional[str]]] = []

        # Pass 1: Read files, parse ASTs, and collect symbol definitions.
        for path in files:
            rel = path.relative_to(self.root).as_posix()
            try:
                source = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                cached_files.append((rel, path, None, None, str(exc)))
                continue

            encoded_len = len(source.encode("utf-8", "replace"))
            if encoded_len > MAX_FILE_BYTES:
                cached_files.append(
                    (rel, path, None, None, "file exceeds the size bound")
                )
                continue

            suffix = path.suffix.lower()
            if suffix in AST_SUFFIXES:
                try:
                    tree = ast.parse(source, filename=rel)
                except (SyntaxError, ValueError) as exc:
                    cached_files.append((rel, path, None, None, str(exc)))
                    continue

                self._collect_py_defs(tree, rel, definitions)
                cached_files.append((rel, path, source, tree, None))
            else:
                for name in _lexical_definitions(source):
                    definitions[name].add("%s:%s" % (rel, name))
                cached_files.append((rel, path, source, None, None))

        graph.known_symbols = {
            name: tuple(sorted(qualified))
            for name, qualified in sorted(definitions.items())
        }

        # Pass 2: Extract call sites and resolve them using cached ASTs/sources.
        for rel, path, source, tree, error in cached_files:
            graph.files_scanned += 1
            if error:
                graph.parse_errors.append({"file": rel, "error": error})
                continue

            if tree is not None:
                graph.files_parsed += 1
                self._index_python_tree(rel, tree, graph)
            elif source is not None:
                self._index_lexical(rel, source, graph)

        return graph

    # -- pass 1 ----------------------------------------------------------

    def _collect_py_defs(
        self, tree: ast.AST, rel: str, definitions: Dict[str, Set[str]]
    ) -> None:
        """Performance optimization (Bolt ⚡): Direct recursive definition collector

        avoiding ast.walk deque allocation and iter_child_nodes inspection overhead.
        """
        def visit(node: ast.AST) -> None:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualified = self._python_qualifier(rel, node.name)
                definitions[node.name].add(qualified)
            for field in node._fields:
                val = getattr(node, field, None)
                if isinstance(val, ast.AST):
                    visit(val)
                elif isinstance(val, list):
                    for item in val:
                        if isinstance(item, ast.AST):
                            visit(item)

        visit(tree)

    def _collect_definitions(self, files: Sequence[Path]
                             ) -> Dict[str, Set[str]]:
        """Legacy helper maintained for backward compatibility."""
        definitions: Dict[str, Set[str]] = defaultdict(set)
        for path in files:
            rel = path.relative_to(self.root).as_posix()
            suffix = path.suffix.lower()
            try:
                source = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if suffix in AST_SUFFIXES:
                try:
                    tree = ast.parse(source, filename=rel)
                except (SyntaxError, ValueError):
                    continue
                self._collect_py_defs(tree, rel, definitions)
            else:
                for name in _lexical_definitions(source):
                    definitions[name].add("%s:%s" % (rel, name))
        return definitions

    @staticmethod
    def _python_qualifier(rel: str, name: str) -> str:
        """Qualified name for a top-level definition in a Python file."""
        module = rel[:-3].replace("/", ".")
        if module.endswith(".__init__"):
            module = module[: -len(".__init__")]
        return "%s:%s" % (module, name)

    # -- pass 2 ----------------------------------------------------------

    def _index_python(self, rel: str, source: str, graph: CallGraph) -> None:
        """Parse source string and extract Python call sites."""
        try:
            tree = ast.parse(source, filename=rel)
        except (SyntaxError, ValueError) as exc:
            graph.parse_errors.append({"file": rel, "error": str(exc)})
            return
        graph.files_parsed += 1
        self._index_python_tree(rel, tree, graph)

    def _index_python_tree(self, rel: str, tree: ast.AST, graph: CallGraph) -> None:
        """Extract Python call sites from pre-parsed AST tree.

        Performance optimization (Bolt ⚡): Maintain current_caller string on scope
        push/pop rather than re-joining scope stack strings on every call site.
        """
        module = rel[:-3].replace("/", ".")
        if module.endswith(".__init__"):
            module = module[: -len(".__init__")]

        # Walk with an explicit scope stack so the caller is always known.
        stack: List[str] = []
        scope_prefix = "%s:" % module
        current_caller = "%s<module>" % scope_prefix
        budget = [MAX_CALL_SITES_PER_FILE]

        def add_site(callee: str, line: int) -> None:
            if budget[0] <= 0:
                graph.truncated = True
                return
            if len(graph.call_sites) >= MAX_CALL_SITES:
                graph.truncated = True
                return
            budget[0] -= 1
            graph.add(self._resolve(rel, callee, current_caller, line, "ast", graph))

        def visit(node: ast.AST) -> None:
            nonlocal current_caller
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                prev_caller = current_caller
                stack.append(node.name)
                current_caller = "%s%s" % (scope_prefix, ".".join(stack))
                for child in ast.iter_child_nodes(node):
                    visit(child)
                stack.pop()
                current_caller = prev_caller
                return
            if isinstance(node, ast.ClassDef):
                prev_caller = current_caller
                stack.append(node.name)
                current_caller = "%s%s" % (scope_prefix, ".".join(stack))
                for child in ast.iter_child_nodes(node):
                    visit(child)
                stack.pop()
                current_caller = prev_caller
                return
            if isinstance(node, ast.Call):
                callee = _python_callee_name(node.func)
                if callee:
                    add_site(callee, node.lineno)
                for child in ast.iter_child_nodes(node):
                    visit(child)
                return
            for child in ast.iter_child_nodes(node):
                visit(child)

        for child in ast.iter_child_nodes(tree):
            visit(child)

    def _resolve(self, rel: str, callee: str, caller: str, line: int,
                 tier: str, graph: CallGraph) -> CallSite:
        candidates = graph.known_symbols.get(callee, ())
        if not candidates:
            return CallSite(caller=caller, callee=callee, file=rel, line=line,
                            tier=tier, status="external")
        if len(candidates) == 1:
            return CallSite(caller=caller, callee=callee, file=rel, line=line,
                            tier=tier, status="resolved",
                            candidates=candidates)
        return CallSite(caller=caller, callee=callee, file=rel, line=line,
                        tier=tier, status="ambiguous",
                        candidates=tuple(sorted(candidates)))

    def _index_lexical(self, rel: str, source: str, graph: CallGraph) -> None:
        graph.files_parsed += 1
        scope = "%s:<file>" % rel
        count = 0
        for lineno, line in enumerate(source.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith(("//", "/*", "*", "#")):
                continue
            definition = _lexical_definition_on_line(line)
            if definition:
                scope = "%s:%s" % (rel, definition)
            for match in CALL_RE.finditer(line):
                name = match.group(1)
                if name in NON_CALL_KEYWORDS or name == definition:
                    continue
                if count >= MAX_CALL_SITES_PER_FILE or len(
                        graph.call_sites) >= MAX_CALL_SITES:
                    graph.truncated = True
                    return
                count += 1
                graph.add(CallSite(
                    caller=scope, callee=name, file=rel, line=lineno,
                    tier="lexical", status="lexical",
                    candidates=graph.known_symbols.get(name, ())))


def _python_callee_name(func: ast.AST) -> str:
    """Best-effort callee name: ``f``, ``obj.f``, and ``self.f`` all give ``f``."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Call):
        return _python_callee_name(func.func)
    return ""


def _lexical_definition_on_line(line: str) -> str:
    """Detect ``type name(...) {`` style definitions conservatively."""
    match = re.match(
        r"^\s*(?:static\s+|inline\s+|extern\s+|pub\s+(?:\(crate\)\s+)?"
        r"|async\s+|export\s+|function\s+)*"
        r"[A-Za-z_][A-Za-z0-9_:<>,\s\*&]*?\b([A-Za-z_][A-Za-z0-9_]*)\s*"
        r"(?:\([^;{]*\))\s*(?:->[^{]*)?\{", line)
    if not match:
        return ""
    name = match.group(1)
    if name in NON_CALL_KEYWORDS:
        return ""
    return name


def _lexical_definitions(source: str) -> Set[str]:
    return {name for name in
            (_lexical_definition_on_line(line)
             for line in source.splitlines()) if name}
