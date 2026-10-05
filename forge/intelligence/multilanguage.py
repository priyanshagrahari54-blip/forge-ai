from __future__ import annotations

"""Lightweight multi-language repository intelligence.

This adapter intentionally avoids requiring a heavyweight parser on every
machine. It detects source languages and extracts conservative symbols/import
edges with regex fallbacks; Python continues to use Forge's AST parser.
External parsers can be plugged in later without changing the index contract.
"""
from __future__ import annotations

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

    def by_language(self, language: str) -> list[SourceRecord]:
        return [r for r in self.records if r.language == language]

    def find(self, query: str) -> list[SourceRecord]:
        q=query.lower().strip()
        return [r for r in self.records if q in r.path.lower() or
                any(q in s.lower() for s in r.symbols)]

    def summary(self) -> dict:
        counts={}
        for r in self.records: counts[r.language]=counts.get(r.language,0)+1
        return {"files":len(self.records),"languages":counts}

def _extract(language: str, text: str) -> tuple[tuple[str,...],tuple[str,...]]:
    symbols=set(); imports=set()
    if language=="python":
        return (), ()
    if language in {"javascript","typescript"}:
        symbols.update(re.findall(r"\b(?:function|class)\s+([A-Za-z_$][\w$]*)",text))
        imports.update(re.findall(r'''(?:from|import)\s+["']([^"']+)["']''',text))
    elif language in {"java","kotlin","csharp"}:
        symbols.update(re.findall(r"\b(?:class|interface|enum|object)\s+([A-Za-z_]\w*)",text))
        imports.update(re.findall(r'''(?:from|import)\s+["']([^"']+)["']''',text))
    elif language in {"go","rust","swift","c","cpp"}:
        symbols.update(re.findall(r"\b(?:func|fn|struct|class|enum|interface)\s+([A-Za-z_]\w*)",text))
        imports.update(re.findall(r'''(?:from|import)\s+["']([^"']+)["']''',text))
    elif language in {"ruby","php"}:
        symbols.update(re.findall(r"\b(?:class|module|def)\s+([A-Za-z_]\w*[!?=]?)",text))
        imports.update(re.findall(r'''(?:from|import)\s+["']([^"']+)["']''',text))
    return tuple(sorted(symbols)), tuple(sorted({a or b for a,b in imports}))

def build_multilanguage_index(root: str|Path=".") -> MultiLanguageIndex:
    base=Path(root).resolve(); records=[]
    for path in base.rglob("*"):
        if len(records)>=MAX_FILES: break
        if not path.is_file() or path.suffix.lower() not in EXTENSIONS: continue
        if any(part in SKIP for part in path.parts): continue
        try:
            size=path.stat().st_size
            if size>MAX_BYTES: continue
            text=path.read_text(encoding="utf-8",errors="replace")
            rel=path.relative_to(base).as_posix()
            symbols,imports=_extract(EXTENSIONS[path.suffix.lower()],text)
            records.append(SourceRecord(rel,EXTENSIONS[path.suffix.lower()],size,symbols,imports))
        except OSError:
            continue
    return MultiLanguageIndex(records)
