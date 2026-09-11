"""Research engine configuration (operator-controlled, fail-closed).

Loaded from ``.forge/research.yaml`` when present; every field has a
safe default. Web sources are *operator configuration*: only the
listed hosts are ever contacted for configured/official documentation
lookups, always through the SSRF-safe chain.

Example ``.forge/research.yaml``::

    web_enabled: true
    allow_http: false
    timeout_seconds: 10
    max_bytes: 200000
    max_redirects: 3
    cache_ttl_seconds: 21600
    allow_model_knowledge: false
    doc_paths: [docs, README.md]
    web_sources:
      - name: python-docs
        url: "https://docs.python.org/3/search.html?q={query}"
      - name: mdn
        url: "https://developer.mozilla.org/en-US/search?q={query}"
    official_docs:
      fastapi: "https://fastapi.tiangolo.com/"
      httpx: "https://www.python-httpx.org/"
"""
from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from forge.security.ssrf import (
    DEFAULT_MAX_BYTES, DEFAULT_MAX_REDIRECTS, DEFAULT_TIMEOUT, FetchPolicy,
    hostname_blocked_reason,
)

CONFIG_RELATIVE_PATH = Path(".forge") / "research.yaml"
CACHE_RELATIVE_PATH = Path(".forge") / "research_cache"

#: Built-in official documentation index. Only well-known public HTTPS
#: hosts; operators can extend or override via ``official_docs``.
DEFAULT_OFFICIAL_DOCS: Dict[str, str] = {
    "python": "https://docs.python.org/3/",
    "fastapi": "https://fastapi.tiangolo.com/",
    "starlette": "https://www.starlette.io/",
    "pydantic": "https://docs.pydantic.dev/latest/",
    "uvicorn": "https://www.uvicorn.org/",
    "httpx": "https://www.python-httpx.org/",
    "requests": "https://requests.readthedocs.io/en/latest/",
    "pytest": "https://docs.pytest.org/en/stable/",
    "pyyaml": "https://pyyaml.org/wiki/PyYAMLDocumentation",
    "yaml": "https://pyyaml.org/wiki/PyYAMLDocumentation",
    "sqlalchemy": "https://docs.sqlalchemy.org/en/20/",
    "django": "https://docs.djangoproject.com/en/stable/",
    "flask": "https://flask.palletsprojects.com/en/stable/",
    "numpy": "https://numpy.org/doc/stable/",
    "pandas": "https://pandas.pydata.org/docs/",
    "node": "https://nodejs.org/api/",
    "npm": "https://docs.npmjs.com/",
    "react": "https://react.dev/reference/react",
    "typescript": "https://www.typescriptlang.org/docs/",
    "docker": "https://docs.docker.com/",
    "git": "https://git-scm.com/docs",
    "pip": "https://pip.pypa.io/en/stable/",
    "setuptools": "https://setuptools.pypa.io/en/latest/",
    "ruff": "https://docs.astral.sh/ruff/",
    "mypy": "https://mypy.readthedocs.io/en/stable/",
}

#: Python standard-library modules routed to docs.python.org/3/library.
STDLIB_MODULES: Tuple[str, ...] = (
    "asyncio", "argparse", "collections", "dataclasses", "datetime",
    "functools", "hashlib", "http", "ipaddress", "itertools", "json",
    "logging", "os", "pathlib", "re", "socket", "sqlite3", "ssl",
    "subprocess", "sys", "threading", "time", "typing", "unittest",
    "urllib", "uuid", "enum", "shutil", "tempfile", "io", "csv",
    "secrets", "base64", "struct", "queue", "multiprocessing", "signal",
    "inspect", "importlib", "contextlib", "abc", "copy", "math",
    "random", "statistics", "string", "textwrap", "traceback",
    "warnings", "weakref", "zipfile", "tarfile", "gzip", "select",
    "selectors", "email", "html", "xml", "concurrent", "decimal",
    "fractions", "operator", "pickle", "pprint", "platform", "glob",
    "fnmatch", "locale", "gettext", "codecs", "unicodedata", "heapq",
    "bisect", "array", "types", "ast", "dis", "tokenize", "venv",
)


@dataclass(frozen=True)
class WebSourceConfig:
    """One operator-configured web source (search template or page)."""

    name: str
    url: str                      # may contain ``{query}``
    kind: str = "search"          # "search" (has {query}) or "page"

    @property
    def host(self) -> str:
        return (urllib.parse.urlparse(self.url).hostname or "").lower()

    def render(self, query: str) -> str:
        if "{query}" in self.url:
            return self.url.replace("{query}", urllib.parse.quote_plus(query[:200]))
        return self.url


@dataclass
class ResearchConfig:
    """Validated research settings; invalid entries are dropped, never widened."""

    web_enabled: bool = True
    allow_http: bool = False
    timeout_seconds: float = min(DEFAULT_TIMEOUT, 10.0)
    max_bytes: int = DEFAULT_MAX_BYTES
    max_redirects: int = DEFAULT_MAX_REDIRECTS
    cache_ttl_seconds: int = 6 * 60 * 60
    cache_max_entries: int = 256
    allow_model_knowledge: bool = False
    max_results: int = 12
    doc_paths: List[str] = field(default_factory=lambda: [
        "docs", "doc", "documentation", "README.md", "README.rst",
        "README", "CONTRIBUTING.md", "CHANGELOG.md", "ARCHITECTURE.md"])
    web_sources: List[WebSourceConfig] = field(default_factory=list)
    official_docs: Dict[str, str] = field(
        default_factory=lambda: dict(DEFAULT_OFFICIAL_DOCS))
    rejected: List[str] = field(default_factory=list)
    source_path: str = ""

    # -- derived ------------------------------------------------------------

    def fetch_policy(self) -> FetchPolicy:
        """SSRF policy for every research web fetch (HTTPS-only default)."""
        hosts = tuple(sorted({s.host for s in self.web_sources if s.host}
                             | {h for h in self.official_hosts()}))
        return FetchPolicy(
            allow_http=bool(self.allow_http),
            host_allowlist=hosts,
            max_redirects=int(self.max_redirects),
            timeout=float(self.timeout_seconds),
            max_bytes=int(self.max_bytes),
            audit_operation="research_fetch",
            audit_agent="forge-research",
        )

    def official_hosts(self) -> List[str]:
        hosts = set()
        for url in self.official_docs.values():
            host = (urllib.parse.urlparse(url).hostname or "").lower()
            if host:
                hosts.add(host)
        hosts.add("docs.python.org")
        return sorted(hosts)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "web_enabled": self.web_enabled,
            "allow_http": self.allow_http,
            "timeout_seconds": self.timeout_seconds,
            "max_bytes": self.max_bytes,
            "max_redirects": self.max_redirects,
            "cache_ttl_seconds": self.cache_ttl_seconds,
            "cache_max_entries": self.cache_max_entries,
            "allow_model_knowledge": self.allow_model_knowledge,
            "max_results": self.max_results,
            "doc_paths": list(self.doc_paths),
            "web_sources": [
                {"name": s.name, "url": s.url, "kind": s.kind, "host": s.host}
                for s in self.web_sources],
            "official_docs_count": len(self.official_docs),
            "allowed_hosts": self.fetch_policy().host_allowlist,
            "rejected": list(self.rejected),
            "source_path": self.source_path,
        }


def _validate_url(url: Any, *, allow_http: bool) -> Optional[str]:
    if not isinstance(url, str) or not url.strip():
        return "url must be a non-empty string"
    parsed = urllib.parse.urlparse(url.strip())
    scheme = (parsed.scheme or "").lower()
    if scheme == "http" and not allow_http:
        return "http:// refused (allow_http is false)"
    if scheme not in ("https", "http"):
        return f"scheme {scheme!r} refused"
    host = (parsed.hostname or "").lower()
    reason = hostname_blocked_reason(host)
    if reason:
        return reason
    if parsed.username or parsed.password:
        return "embedded credentials refused"
    return None


def load_research_config(root: str | Path,
                         data: Optional[Dict[str, Any]] = None) -> ResearchConfig:
    """Load and validate ``.forge/research.yaml`` (or an explicit mapping)."""
    root = Path(root)
    config = ResearchConfig()
    if data is None:
        path = root / CONFIG_RELATIVE_PATH
        if not path.is_file():
            return config
        try:
            import yaml  # PyYAML is a project dependency
            with path.open("r", encoding="utf-8") as handle:
                loaded = yaml.safe_load(handle) or {}
        except Exception as exc:  # malformed config never widens policy
            config.rejected.append(f"config unreadable: {exc}")
            return config
        config.source_path = str(path)
        data = loaded if isinstance(loaded, dict) else {}
        if not isinstance(loaded, dict):
            config.rejected.append("config root must be a mapping")

    def _bool(key: str, default: bool) -> bool:
        value = data.get(key, default)
        if isinstance(value, bool):
            return value
        config.rejected.append(f"{key}: expected boolean")
        return default

    def _num(key: str, default: float, lo: float, hi: float) -> float:
        value = data.get(key, default)
        if isinstance(value, (int, float)) and not isinstance(value, bool) \
                and lo <= value <= hi:
            return value
        config.rejected.append(f"{key}: expected number in [{lo}, {hi}]")
        return default

    config.web_enabled = _bool("web_enabled", True)
    config.allow_http = _bool("allow_http", False)
    config.allow_model_knowledge = _bool("allow_model_knowledge", False)
    config.timeout_seconds = float(_num("timeout_seconds", config.timeout_seconds, 1, 60))
    config.max_bytes = int(_num("max_bytes", config.max_bytes, 1_000, 2_000_000))
    config.max_redirects = int(_num("max_redirects", config.max_redirects, 0, 5))
    config.cache_ttl_seconds = int(_num("cache_ttl_seconds", config.cache_ttl_seconds, 0, 30 * 86400))
    config.cache_max_entries = int(_num("cache_max_entries", config.cache_max_entries, 1, 10_000))
    config.max_results = int(_num("max_results", config.max_results, 1, 50))

    doc_paths = data.get("doc_paths")
    if isinstance(doc_paths, list):
        clean = []
        for item in doc_paths:
            if isinstance(item, str) and item and ".." not in item \
                    and not item.startswith(("/", "\\")):
                clean.append(item)
            else:
                config.rejected.append(f"doc_paths: refused {item!r}")
        if clean:
            config.doc_paths = clean

    sources = data.get("web_sources")
    if isinstance(sources, list):
        for entry in sources:
            if not isinstance(entry, dict):
                config.rejected.append("web_sources: entry must be a mapping")
                continue
            name = str(entry.get("name", "")).strip()[:64]
            url = entry.get("url", "")
            problem = _validate_url(url, allow_http=config.allow_http)
            if not name or problem:
                config.rejected.append(
                    f"web_sources[{name or '?'}]: {problem or 'missing name'}")
                continue
            kind = "search" if "{query}" in url else "page"
            config.web_sources.append(WebSourceConfig(name=name, url=url.strip(), kind=kind))

    official = data.get("official_docs")
    if isinstance(official, dict):
        for lib, url in official.items():
            problem = _validate_url(url, allow_http=config.allow_http)
            if problem or not isinstance(lib, str):
                config.rejected.append(f"official_docs[{lib}]: {problem or 'bad key'}")
                continue
            config.official_docs[lib.lower()] = url.strip()
    return config
