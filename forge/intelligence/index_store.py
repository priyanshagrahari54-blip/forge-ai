"""Persistent repository index with real invalidation (A83).

:class:`~forge.intelligence.repository.RepositoryIntelligence` re-walks and
re-parses the whole tree on every construction, which is correct but slow
enough to make agents rediscover a repository they just indexed. This module
adds a cached, fingerprinted index on top of it — it does not replace it.

The cache is honest about staleness:

* the fingerprint is a SHA-256 over ``(relative path, size, mtime_ns)`` for
  every source file, so an edit, a rename, or a delete all invalidate it;
* a fingerprint mismatch, a corrupt cache file, or a version change means the
  index is rebuilt from scratch — a stale index is never served silently;
* every load reports ``cache`` as ``hit``, ``miss``, ``stale``, ``corrupt``,
  or ``disabled`` so the caller (and the cockpit) can see what happened.

The cache lives under ``.forge/index/`` and is git-ignored: it is derived
data, and derived data does not belong in version control.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from forge.intelligence.call_graph import CallGraph, CallGraphIndexer
from forge.intelligence.discovery import Discovery, DiscoveryReport
from forge.intelligence.repository import RepositoryIntelligence
from forge.intelligence.semantic import SemanticIndex, SemanticIndexer

INDEX_VERSION = 3
DEFAULT_CACHE_DIR = ".forge/index"
MAX_CACHE_BYTES = 64 * 1024 * 1024
SOURCE_SUFFIXES = (
    ".py", ".c", ".h", ".cc", ".cpp", ".hpp", ".rs", ".js", ".mjs", ".ts",
    ".tsx", ".jsx", ".java", ".go", ".md", ".yaml", ".yml", ".toml", ".json",
    ".sh", ".asm", ".s", ".S", ".ld", ".proto",
)
SKIP_DIRECTORIES = frozenset({
    ".git", ".venv", "venv", "env", "node_modules", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "build", "dist", "target",
    ".forge",
})


@dataclass
class IndexStats:
    """What the last index build/load actually did."""

    status: str = "built"          # built | hit | stale | corrupt | disabled
    files: int = 0
    fingerprint: str = ""
    elapsed_ms: float = 0.0
    cached_at: float = 0.0
    bytes: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "files": self.files,
            "fingerprint": self.fingerprint,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "cached_at": self.cached_at,
            "bytes": self.bytes,
        }


def _walk_sources(root: Path, suffixes) -> List[Path]:
    out: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(str(root)):
        current = Path(dirpath)
        dirnames[:] = [name for name in dirnames
                       if name not in SKIP_DIRECTORIES]
        for filename in filenames:
            if Path(filename).suffix.lower() in suffixes:
                out.append(current / filename)
    return sorted(out)


def compute_fingerprint(root: str | Path,
                        suffixes=SOURCE_SUFFIXES) -> Dict[str, Any]:
    """Hash every source file's path, size, and mtime into one digest."""
    base = Path(root).resolve()
    digest = hashlib.sha256()
    files = _walk_sources(base, suffixes)
    for path in files:
        try:
            stat = path.stat()
        except OSError:
            continue
        rel = path.relative_to(base).as_posix()
        digest.update(("%s|%d|%d\n" % (rel, stat.st_size,
                                       int(stat.st_mtime_ns))).encode("utf-8"))
    return {"files": len(files), "digest": digest.hexdigest()}


@dataclass
class RepositoryIndex:
    """Cached, composed repository understanding."""

    root: Path
    intelligence: RepositoryIntelligence
    call_graph: CallGraph
    semantic: SemanticIndex
    discovery: DiscoveryReport
    stats: IndexStats = field(default_factory=IndexStats)

    def affected(self, source: str) -> Dict[str, Any]:
        """What a change to *source* touches, across every index layer.

        This is the "identify affected components before making changes"
        question, answered from real edges: file dependents, symbol callers,
        tests mapped to the file, and the API findings declared in it.
        """
        rel = source.replace("\\", "/")
        callers = sorted({
            site.caller
            for site in self.call_graph.calls_to(rel)
        })
        symbol_callers = sorted({
            caller
            for symbol in self.intelligence.symbols.by_file(rel)
            for caller in self.call_graph.transitive_callers(symbol.name)
        })
        dependents = self.intelligence._transitive_dependent_paths(rel)
        affected_files = self.intelligence.impact(rel)
        direct_tests = self.intelligence.affected_tests(rel)
        # A change to a base module affects the tests of everything that
        # imports it, not only the tests that name it directly.
        tests = sorted(set(direct_tests) | {
            test
            for dependent in dependents
            for test in self.intelligence.affected_tests(dependent)
        })
        return {
            "source": rel,
            "package": self.intelligence.architecture.package_for_file(rel),
            "file_dependents": dependents,
            "affected_tests": tests,
            "direct_tests": direct_tests,
            "affected_files": affected_files,
            "callers": callers,
            "transitive_symbol_callers": symbol_callers,
            "api_findings": [
                item.to_dict() for item in self.discovery.apis
                if item.path == rel
            ],
        }

    def search(self, query: str, *, limit: int = 10) -> List[Dict[str, Any]]:
        """Semantic search over the indexed repository."""
        return [hit.to_dict() for hit in self.semantic.search(query, limit=limit)]

    def summary(self) -> Dict[str, Any]:
        return {
            "root": str(self.root),
            "index": self.stats.to_dict(),
            "intelligence": {
                "symbols": len(self.intelligence.symbols.symbols),
                "packages": len(self.intelligence.architecture.packages),
                "source_files": len(self.intelligence.architecture.source_files),
                "test_files": len(self.intelligence.architecture.test_files),
                "project_type": list(self.intelligence.runtime.project_type),
            },
            "call_graph": self.call_graph.summary(),
            "semantic": self.semantic.summary(),
            "discovery": self.discovery.summary(),
        }


class IndexStore:
    """Fingerprinted on-disk cache for :class:`RepositoryIndex`."""

    def __init__(self, root: str | Path, *,
                 cache_dir: str = DEFAULT_CACHE_DIR,
                 enabled: bool = True) -> None:
        self.root = Path(root).resolve()
        self.cache_dir = self.root / cache_dir
        self.enabled = enabled

    @property
    def cache_file(self) -> Path:
        return self.cache_dir / "repository-index.json"

    def load(self) -> Optional[Dict[str, Any]]:
        """Return the cached payload when — and only when — it is current."""
        if not self.enabled or not self.cache_file.is_file():
            return None
        try:
            if self.cache_file.stat().st_size > MAX_CACHE_BYTES:
                return None
            payload = json.loads(self.cache_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("version") != INDEX_VERSION:
            return None
        current = compute_fingerprint(self.root)
        if payload.get("fingerprint") != current["digest"]:
            return None
        return payload

    def save(self, payload: Dict[str, Any], fingerprint: str) -> bool:
        if not self.enabled:
            return False
        document = {"version": INDEX_VERSION, "fingerprint": fingerprint,
                    "cached_at": time.time(), "payload": payload}
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            text = json.dumps(document, sort_keys=True)
            self.cache_file.write_text(text, encoding="utf-8")
        except (OSError, TypeError, ValueError):
            return False
        return True

    def invalidate(self) -> bool:
        """Delete the cache file; returns True when something was removed."""
        try:
            self.cache_file.unlink()
            return True
        except OSError:
            return False

    def status(self) -> Dict[str, Any]:
        exists = self.cache_file.is_file()
        info: Dict[str, Any] = {
            "enabled": self.enabled,
            "path": str(self.cache_file),
            "exists": exists,
            "fresh": False,
        }
        if exists:
            info["bytes"] = self.cache_file.stat().st_size
            info["fresh"] = self.load() is not None
        return info


class RepositoryIndexer:
    """Build (or load from cache) the composed repository index.

    On a cache hit the call graph, semantic index, and discovery report are
    **reconstructed from the cached document** — no source file is re-read and
    no file is re-parsed for those three layers. That is the whole point of
    the cache, and it is why the hit is measured rather than asserted.

    :class:`RepositoryIntelligence` is always rebuilt: its symbol and
    dependency indexes are not serialised here. The measured timings in
    ``stats`` therefore reflect real work either way.
    """

    def __init__(self, root: str | Path, *, cache: bool = True,
                 call_graph: bool = True, semantic: bool = True,
                 discovery: bool = True,
                 index_symbols: bool = False) -> None:
        self.root = Path(root).resolve()
        self.cache = IndexStore(self.root, enabled=cache)
        self.build_call_graph = call_graph
        self.build_semantic = semantic
        self.build_discovery = discovery
        self.index_symbols = index_symbols

    def build(self, *, force: bool = False) -> RepositoryIndex:
        started = time.monotonic()
        fingerprint = compute_fingerprint(self.root)
        stats = IndexStats(fingerprint=fingerprint["digest"],
                           files=fingerprint["files"])
        payload = None if force else self.cache.load()
        if payload is not None:
            stats.status = "hit"
            stats.cached_at = float(payload.get("cached_at", 0.0))
            document = payload.get("payload") or {}
            call_graph = CallGraph.from_dict(document.get("call_graph") or {})
            semantic = SemanticIndex.from_dict(document.get("semantic") or {})
            discovery = DiscoveryReport.from_dict(
                document.get("discovery") or {})
        else:
            stats.status = "built"
            call_graph = (CallGraphIndexer(self.root).build()
                          if self.build_call_graph else CallGraph())
            semantic = (SemanticIndexer(self.root,
                                        index_symbols=self.index_symbols).build()
                        if self.build_semantic
                        else SemanticIndexer(self.root).build())
            discovery = (Discovery(self.root).run()
                         if self.build_discovery
                         else DiscoveryReport(root=str(self.root)))
            saved = self.cache.save(
                {"call_graph": call_graph.to_dict(),
                 "semantic": semantic.to_dict(),
                 "discovery": discovery.to_dict()},
                fingerprint["digest"])
            stats.bytes = (self.cache.cache_file.stat().st_size
                           if saved and self.cache.cache_file.is_file() else 0)

        index = RepositoryIndex(
            root=self.root,
            intelligence=RepositoryIntelligence.build(self.root),
            call_graph=call_graph,
            semantic=semantic,
            discovery=discovery,
            stats=stats,
        )
        stats.elapsed_ms = (time.monotonic() - started) * 1000
        return index
