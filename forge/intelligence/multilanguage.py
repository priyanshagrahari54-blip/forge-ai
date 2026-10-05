"""Lightweight multi-language repository intelligence.

This adapter intentionally avoids requiring a heavyweight parser on every
machine. It detects source languages and extracts conservative symbols/import
edges with regex fallbacks; Python continues to use Forge's AST parser.
External parsers can be plugged in later without changing the index contract.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

EXTENSIONS = {
    ".py":"python", ".js":"javascript", ".jsx":"javascript", ".ts":"typescript",
    ".tsx":"typescript", ".java":"java", ".go":"go", ".rs":"rust", ".c":"c",
    ".h":"c", ".cc":"cpp", ".cpp":"cpp", ".cxx":"cpp", ".hpp":"cpp",
    ".cs":"csharp", ".kt":"kotlin", ".kts":"kotlin", ".swift":"swift",
    ".rb":"ruby", ".php":"php", ".dart":"dart",
}
SKIP = {".git",".forge","node_modules","target","build","dist",".venv","venv","__pycache__"}
MAX_FILES = 30000
MAX_BYTES = 1_000_000

@dataclass(frozen=True)
class SourceRecord:
    path: str
    language: str
    size: int
    symbols: tuple[str,...] = ()
    imports: tuple[str,...] = ()

@dataclass
class MultiLanguageIndex:
    records: list[SourceRecord] = field(default_factory=list)
    # Performance optimization (Bolt ⚡): Maintain O(1) hash map index for by_language queries
    _by_language: dict[str, list[SourceRecord]] = field(
        default_factory=dict, init=False, repr=False
    )

    def __post_init__(self) -> None:
        by_lang: dict[str, list[SourceRecord]] = {}
        for r in self.records:
            by_lang.setdefault(r.language, []).append(r)
        self._by_language = by_lang

    def add(self, record: SourceRecord) -> None:
        self.records.append(record)
        self._by_language.setdefault(record.language, []).append(record)

    def by_language(self, language: str) -> list[SourceRecord]:
        return list(self._by_language.get(language, []))

    def find(self, query: str) -> list[SourceRecord]:
        q=query.lower().strip()
        return [r for r in self.records if q in r.path.lower() or
                any(q in s.lower() for s in r.symbols)]

    def summary(self) -> dict:
        counts = {lang: len(recs) for lang, recs in self._by_language.items()}
        return {"files":len(self.records),"languages":counts}

def _extract(language: str, text: str) -> tuple[tuple[str,...],tuple[str,...]]:
    symbols=set(); imports=set()
    if language=="python":
        return (), ()
    if language in {"javascript","typescript"}:
        symbols.update(re.findall(r"\b(?:function|class)\s+([A-Za-z_$][\w$]*)",text))
        imports.update(re.findall(r"""(?:from|import)\s+['"]([^'"]+)['"]""",text))
    elif language in {"java","kotlin","csharp"}:
        symbols.update(re.findall(r"\b(?:class|interface|enum|object)\s+([A-Za-z_]\w*)",text))
        imports.update(re.findall(r"\bimport\s+([\w.]+)",text))
    elif language in {"go","rust","swift","c","cpp"}:
        symbols.update(re.findall(r"\b(?:func|fn|struct|class|enum|interface)\s+([A-Za-z_]\w*)",text))
        imports.update(re.findall(r"#include\s*[<\"]([^>\"]+)|\buse\s+([\w:]+)",text))
    elif language in {"ruby","php"}:
        symbols.update(re.findall(r"\b(?:class|module|def)\s+([A-Za-z_]\w*[!?=]?)",text))
        imports.update(re.findall(r"""\brequire(?:_once)?\s*[\( ]\s*['"]([^'"]+)""",text))
    return tuple(sorted(symbols)), tuple(sorted({a or b for a,b in imports}))

def build_multilanguage_index(root: str|Path=".") -> MultiLanguageIndex:
    # Performance optimization (Bolt ⚡): Prune ignored directory trees using os.walk
    # to avoid traversing millions of files in .git, node_modules, etc.
    base=Path(root).resolve()
    base_str=str(base)
    records=[]
    for dirpath, dirnames, filenames in os.walk(base_str):
        dirnames[:] = [d for d in dirnames if d not in SKIP]
        if len(records) >= MAX_FILES:
            break
        for file in filenames:
            if len(records) >= MAX_FILES:
                break
            ext = os.path.splitext(file)[1].lower()
            if ext not in EXTENSIONS:
                continue
            full_path = os.path.join(dirpath, file)
            try:
                size = os.path.getsize(full_path)
                if size > MAX_BYTES:
                    continue
                with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                    text = f.read()
                rel = os.path.relpath(full_path, base_str).replace("\\", "/")
                lang = EXTENSIONS[ext]
                symbols, imports = _extract(lang, text)
                records.append(SourceRecord(rel, lang, size, symbols, imports))
            except OSError:
                continue
    return MultiLanguageIndex(records)
