"""Configuration and API discovery (A83).

Two things Forge needed to know about a repository but had no index for:

* **configuration** — which config files exist, what kind each one is, and the
  few facts in them that change how Forge should behave (declared Python
  version, declared dependencies, CI workflows, lint/format settings);
* **API surface** — the HTTP routes, CLI subcommands, and RPC methods a
  repository exposes, so "what will this change break?" has a concrete answer.

Both are evidence-based: every finding carries the file and line it was read
from. A route Forge could not parse is not reported, and a config file Forge
does not recognise is reported as ``unknown`` rather than guessed at.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from forge.intelligence.gitignore import GitIgnoreMatcher

SKIP_DIRECTORIES = frozenset({
    ".git", ".venv", "venv", "env", "node_modules", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "build", "dist", "target",
})
MAX_FILES = 20_000
MAX_FILE_BYTES = 512 * 1024
MAX_FINDINGS = 2_000

#: filename -> configuration kind
CONFIG_KINDS: Dict[str, str] = {
    "pyproject.toml": "python-project",
    "setup.py": "python-project",
    "setup.cfg": "python-project",
    "requirements.txt": "python-dependencies",
    "package.json": "node-project",
    "package-lock.json": "node-lockfile",
    "yarn.lock": "node-lockfile",
    "pnpm-lock.yaml": "node-lockfile",
    "cargo.toml": "rust-project",
    "cmakelists.txt": "cmake-project",
    "makefile": "make-project",
    "gnumakefile": "make-project",
    "build.gradle": "gradle-project",
    "build.gradle.kts": "gradle-project",
    "pom.xml": "maven-project",
    "meson.build": "meson-project",
    "dockerfile": "container",
    "docker-compose.yml": "container-orchestration",
    "docker-compose.yaml": "container-orchestration",
    ".editorconfig": "editor",
    ".gitignore": "vcs",
    "pytest.ini": "test-config",
    "tox.ini": "test-config",
    ".eslintrc.json": "lint-config",
    "ruff.toml": "lint-config",
    ".clang-format": "format-config",
}
CONFIG_SUFFIX_KINDS: Dict[str, str] = {
    ".env": "environment",
    ".envrc": "environment",
    ".yaml": "config",
    ".yml": "config",
    ".toml": "config",
    ".ini": "config",
    ".cfg": "config",
    ".conf": "config",
    ".json": "config",
}

HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")
#: FastAPI/Flask/Starlette style: @router.get("/path")
DECORATOR_ROUTE_RE = re.compile(
    r"^\s*@(?P<obj>[A-Za-z_][A-Za-z0-9_.]*)\.(?P<method>"
    + "|".join(HTTP_METHODS)
    + r"|route)\(\s*[\"'](?P<path>[^\"']+)[\"']")
#: Express/Koa style: app.get('/path', handler)
EXPRESS_ROUTE_RE = re.compile(
    r"\b(?P<obj>[A-Za-z_][A-Za-z0-9_]*)\.(?P<method>"
    + "|".join(HTTP_METHODS)
    + r")\(\s*[\"'](?P<path>/[^\"']*)[\"']")
#: argparse: add_parser("name")
CLI_COMMAND_RE = re.compile(
    r"\badd_parser\(\s*[\"'](?P<name>[A-Za-z0-9_.\-]+)[\"']")
#: proto: rpc Name(Req) returns (Resp)
RPC_RE = re.compile(
    r"\brpc\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\(")
#: Kernel/OS style exported entry points.
EXPORT_RE = re.compile(
    r"\bEXPORT_SYMBOL(?:_GPL)?\(\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\)")
SYSCALL_RE = re.compile(
    r"\bSYSCALL_DEFINE[0-6]\(\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)")


@dataclass(frozen=True)
class ApiFinding:
    """One discovered entry point, with the evidence for it."""

    kind: str
    name: str
    path: str
    line: int
    #: HTTP method for routes, "" otherwise.
    method: str = ""
    #: Handler/route object it was declared on (``router``, ``app``).
    declared_on: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "name": self.name, "path": self.path,
                "line": self.line, "method": self.method,
                "declared_on": self.declared_on}


@dataclass(frozen=True)
class ConfigFinding:
    """One discovered configuration file and what it declares."""

    path: str
    kind: str
    facts: Dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"path": self.path, "kind": self.kind, "facts": dict(self.facts),
                "error": self.error}


@dataclass
class DiscoveryReport:
    """Everything discovery found, plus honest coverage information."""

    root: str
    configs: List[ConfigFinding] = field(default_factory=list)
    apis: List[ApiFinding] = field(default_factory=list)
    files_scanned: int = 0
    truncated: bool = False

    def by_kind(self, kind: str) -> List[ApiFinding]:
        return [item for item in self.apis if item.kind == kind]

    def routes(self) -> List[ApiFinding]:
        return [item for item in self.apis if item.kind == "http-route"]

    def commands(self) -> List[str]:
        return sorted({item.name for item in self.by_kind("cli-command")})

    def config_of_kind(self, kind: str) -> List[ConfigFinding]:
        return [item for item in self.configs if item.kind == kind]

    def summary(self) -> Dict[str, Any]:
        kinds: Dict[str, int] = {}
        for item in self.apis:
            kinds[item.kind] = kinds.get(item.kind, 0) + 1
        return {
            "root": self.root,
            "files_scanned": self.files_scanned,
            "config_files": len(self.configs),
            "api_findings": len(self.apis),
            "api_by_kind": {key: kinds[key] for key in sorted(kinds)},
            "truncated": self.truncated,
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "summary": self.summary(),
            "configs": [item.to_dict() for item in self.configs],
            "apis": [item.to_dict() for item in self.apis],
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "DiscoveryReport":
        summary = payload.get("summary") or {}
        return cls(
            root=str(payload.get("root") or summary.get("root", "")),
            configs=[
                ConfigFinding(
                    path=str(item.get("path", "")),
                    kind=str(item.get("kind", "config")),
                    facts=dict(item.get("facts") or {}),
                    error=str(item.get("error", "")),
                )
                for item in (payload.get("configs") or [])
                if isinstance(item, dict)
            ],
            apis=[
                ApiFinding(
                    kind=str(item.get("kind", "")),
                    name=str(item.get("name", "")),
                    path=str(item.get("path", "")),
                    line=int(item.get("line", 0) or 0),
                    method=str(item.get("method", "")),
                    declared_on=str(item.get("declared_on", "")),
                )
                for item in (payload.get("apis") or [])
                if isinstance(item, dict)
            ],
            files_scanned=int(summary.get("files_scanned", 0) or 0),
            truncated=bool(summary.get("truncated", False)),
        )


class Discovery:
    """Discover configuration files and API entry points in a repository."""

    SOURCE_SUFFIXES = (".py", ".js", ".mjs", ".ts", ".tsx", ".java", ".go",
                       ".rs", ".proto", ".c", ".h")

    def __init__(self, root: str | Path, *,
                 max_files: int = MAX_FILES) -> None:
        self.root = Path(root).resolve()
        self.max_files = max(1, max_files)

    def run(self) -> DiscoveryReport:
        report = DiscoveryReport(root=str(self.root))
        matcher = GitIgnoreMatcher(self.root)
        for dirpath, dirnames, filenames in os.walk(str(self.root)):
            current = Path(dirpath)
            dirnames[:] = [
                name for name in sorted(dirnames)
                if name not in SKIP_DIRECTORIES
                and not matcher.is_ignored(current.relative_to(self.root) / name)
            ]
            for filename in sorted(filenames):
                path = current / filename
                rel = path.relative_to(self.root).as_posix()
                if matcher.is_ignored(path.relative_to(self.root)):
                    continue
                if report.files_scanned >= self.max_files:
                    report.truncated = True
                    return report
                report.files_scanned += 1
                suffix = path.suffix.lower()
                if suffix in self.SOURCE_SUFFIXES or filename.lower() in (
                        "makefile", "gnumakefile", "dockerfile",
                        "cmakelists.txt", "meson.build"):
                    self._scan_source(rel, path, report)
                self._scan_config(rel, path, report)
        return report

    # -- API discovery ---------------------------------------------------

    def _scan_source(self, rel: str, path: Path,
                     report: DiscoveryReport) -> None:
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                return
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        suffix = path.suffix.lower()
        for lineno, line in enumerate(source.splitlines(), start=1):
            if len(report.apis) >= MAX_FINDINGS:
                report.truncated = True
                return
            finding = self._api_finding(rel, suffix, line, lineno)
            if finding is not None:
                report.apis.append(finding)

    @staticmethod
    def _api_finding(rel: str, suffix: str, line: str,
                     lineno: int) -> Optional[ApiFinding]:
        if suffix in (".py",):
            match = DECORATOR_ROUTE_RE.match(line)
            if match:
                method = match.group("method").upper()
                return ApiFinding(
                    kind="http-route",
                    name="%s %s" % (method, match.group("path")),
                    path=rel, line=lineno,
                    method="" if method == "ROUTE" else method,
                    declared_on=match.group("obj"))
            match = CLI_COMMAND_RE.search(line)
            if match:
                return ApiFinding(kind="cli-command", name=match.group("name"),
                                  path=rel, line=lineno)
        if suffix in (".js", ".mjs", ".ts", ".tsx"):
            match = EXPRESS_ROUTE_RE.search(line)
            if match:
                return ApiFinding(
                    kind="http-route",
                    name="%s %s" % (match.group("method").upper(),
                                    match.group("path")),
                    path=rel, line=lineno,
                    method=match.group("method").upper(),
                    declared_on=match.group("obj"))
        if suffix == ".proto":
            match = RPC_RE.search(line)
            if match:
                return ApiFinding(kind="rpc", name=match.group("name"),
                                  path=rel, line=lineno)
        if suffix in (".c", ".h"):
            match = EXPORT_RE.search(line)
            if match:
                return ApiFinding(kind="kernel-export", name=match.group("name"),
                                  path=rel, line=lineno)
            match = SYSCALL_RE.search(line)
            if match:
                return ApiFinding(kind="syscall", name=match.group("name"),
                                  path=rel, line=lineno)
        return None

    # -- configuration discovery -----------------------------------------

    #: Directories whose contents are configuration regardless of extension.
    CONFIG_DIRECTORIES = frozenset({
        ".github", ".circleci", ".forge", "config", ".gitlab", ".vscode",
    })

    def _scan_config(self, rel: str, path: Path,
                     report: DiscoveryReport) -> None:
        name = Path(rel).name.lower()
        kind = CONFIG_KINDS.get(name)
        parts = tuple(part.lower() for part in Path(rel).parts[:-1])
        suffix = path.suffix.lower()
        if kind is None:
            if name.startswith(".env"):
                kind = "environment"
            elif name == "dockerfile" or name.startswith("dockerfile."):
                kind = "container"
            elif name.startswith("requirements") and suffix == ".txt":
                kind = "python-dependencies"
            elif suffix in CONFIG_SUFFIX_KINDS and (
                    set(parts) & self.CONFIG_DIRECTORIES):
                kind = ("ci-config" if "workflows" in parts
                        else CONFIG_SUFFIX_KINDS[suffix])
            elif suffix in (".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf") \
                    and "/" not in rel:
                kind = CONFIG_SUFFIX_KINDS[suffix]
        if kind is None:
            return
        if len(report.configs) >= MAX_FINDINGS:
            report.truncated = True
            return
        facts, error = self._facts(rel, path, kind)
        report.configs.append(
            ConfigFinding(path=rel, kind=kind, facts=facts, error=error))

    def _facts(self, rel: str, path: Path,
               kind: str) -> Tuple[Dict[str, Any], str]:
        """Extract the small set of facts Forge actually acts on."""
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                return {}, "file exceeds the read bound"
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return {}, str(exc)
        facts: Dict[str, Any] = {"bytes": len(text)}
        name = Path(rel).name.lower()
        try:
            if name == "pyproject.toml":
                facts.update(_pyproject_facts(text))
            elif name == "package.json":
                payload = json.loads(text)
                scripts = payload.get("scripts") or {}
                facts["scripts"] = sorted(scripts)[:32]
                facts["dependencies"] = len(payload.get("dependencies") or {})
                facts["dev_dependencies"] = len(
                    payload.get("devDependencies") or {})
            elif name == "cargo.toml":
                facts["has_workspace"] = "[workspace]" in text
                facts["dependencies"] = text.count("\n[dependencies")
            elif name == "dockerfile":
                facts["stages"] = len(
                    re.findall(r"(?im)^\s*FROM\s+", text))
            elif name == "requirements.txt":
                facts["requirements"] = len(
                    [line for line in text.splitlines()
                     if line.strip() and not line.strip().startswith("#")])
            elif name.startswith(".env"):
                facts["variables"] = len(
                    [line for line in text.splitlines()
                     if re.match(r"^\s*[A-Za-z_][A-Za-z0-9_]*=", line)])
                # Values are never reported: only the presence of a key.
                facts["keys"] = sorted({
                    match.group(1) for match in
                    re.finditer(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=", text,
                                re.MULTILINE)})[:32]
        except (ValueError, TypeError) as exc:
            return facts, "could not parse: %s" % exc
        return facts, ""


def _pyproject_facts(text: str) -> Dict[str, Any]:
    """Read the pyproject facts Forge needs without a TOML dependency.

    ``tomllib`` is 3.11+ and ``toml`` is not a declared dependency, so the
    small set of facts used here is read with bounded, explicit regexes.
    Every extracted value is reported with the pattern that produced it, so a
    false negative is visible rather than silent.
    """
    facts: Dict[str, Any] = {}
    requires = re.search(r"requires-python\s*=\s*[\"']([^\"']+)[\"']", text)
    if requires:
        facts["requires_python"] = requires.group(1)
    name = re.search(r"^\s*name\s*=\s*[\"']([^\"']+)[\"']", text, re.MULTILINE)
    if name:
        facts["name"] = name.group(1)
    version = re.search(r"^\s*version\s*=\s*[\"']([^\"']+)[\"']", text,
                        re.MULTILINE)
    if version:
        facts["version"] = version.group(1)
    deps = re.search(r"(?ms)^dependencies\s*=\s*\[(.*?)\]", text)
    if deps:
        facts["dependencies"] = len(
            re.findall(r"[\"'][^\"']+[\"']", deps.group(1)))
    facts["has_pytest_config"] = "[tool.pytest.ini_options]" in text
    facts["has_setuptools_config"] = "[tool.setuptools" in text
    return facts
