"""Live preview helpers for staged builds (A82).

Serves two needs: *"show me the website Forge is making"* (sandboxed
iframe over project files) and *"show me what each stage made"*
(per-stage file lists with a safe content viewer).

Safety: every path is validated back into the project root (no
traversal, no absolute paths, no ``.git``/``.forge``), sensitive names
(``.env``, keys, secrets) are refused, raw bytes are served only for an
extension allowlist with ``nosniff`` + a ``sandbox`` CSP, and everything
is size-bounded.
"""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
from typing import List, Optional, Tuple

#: Raw-byte serving allowlist: extension -> media type.
RAW_MEDIA_TYPES = {
    ".html": "text/html",
    ".htm": "text/html",
    ".css": "text/css",
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".json": "application/json",
    ".map": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".txt": "text/plain",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".eot": "application/vnd.ms-fontobject",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
}

#: Extensions the content viewer renders as images.
IMAGE_EXTENSIONS = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico"})

#: Preview entry points must be HTML documents.
ENTRY_EXTENSIONS = frozenset({".html", ".htm"})

#: Conventional entries, preferred in order when present.
CONVENTIONAL_ENTRIES = (
    "index.html",
    "public/index.html",
    "dist/index.html",
    "build/index.html",
    "site/index.html",
    "src/index.html",
)

#: Directories never scanned for candidates.
SKIP_DIRS = frozenset({
    ".git", ".forge", "node_modules", "__pycache__", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".nox",
})

#: Sensitive file names / extensions never served or viewed.
DENY_EXACT_NAMES = frozenset({
    ".env", "id_rsa", "id_ed25519", "id_ecdsa", ".netrc", ".git-credentials",
})
DENY_SUFFIXES = frozenset({".pem", ".key", ".p12", ".pfx", ".keystore"})
DENY_NAME_PARTS = ("secret", "credential", "private_key", "privatekey")

MAX_PATH_CHARS = 512
MAX_TEXT_BYTES = 200_000  # viewer cap (larger files truncate with a flag)
MAX_RAW_BYTES = 5_000_000  # raw serving cap
MAX_SCAN_ENTRIES = 2000  # candidate-scan visit cap
MAX_SCAN_DEPTH = 4
MAX_CANDIDATES = 50


class PreviewError(ValueError):
    """A safe, user-facing preview failure (message contains no paths)."""


def normalize_preview_path(raw: object) -> str:
    """Validate ``raw`` as a project-relative POSIX path.

    Returns the normalized relative path. Raises :class:`PreviewError`
    for anything malformed, escaping, protected, or sensitive — with
    messages that never echo filesystem locations.
    """
    if not isinstance(raw, str):
        raise PreviewError("Preview path must be a string.")
    text = raw.strip().replace("\\", "/")
    if not text or len(raw) > MAX_PATH_CHARS:
        raise PreviewError("Preview path is empty or too long.")
    if "\x00" in text:
        raise PreviewError("Preview path is invalid.")
    while text.startswith("./"):
        text = text[2:]
    if not text or text.startswith("/") or text.startswith("~"):
        raise PreviewError("Preview path must be relative to the project.")
    parts = [part for part in PurePosixPath(text).parts
             if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise PreviewError("Preview path escapes the project.")
    lowered = [part.lower() for part in parts]
    if ".git" in lowered or ".forge" in lowered:
        raise PreviewError("Preview path is protected.")
    name = parts[-1]
    name_lower = name.lower()
    if name_lower in DENY_EXACT_NAMES:
        raise PreviewError("Preview path is sensitive.")
    if any(name_lower.endswith(suffix) for suffix in DENY_SUFFIXES):
        raise PreviewError("Preview path is sensitive.")
    if any(part in name_lower for part in DENY_NAME_PARTS):
        raise PreviewError("Preview path is sensitive.")
    return "/".join(parts)


def resolve_under_root(root: str, relative: str) -> Path:
    """Resolve ``relative`` strictly inside ``root``.

    ``relative`` must already be normalized; the resolved path is
    re-checked against the root to defeat symlinks pointing outside.
    Raises :class:`PreviewError` on escape.
    """
    base = Path(root).resolve()
    try:
        target = (base / relative).resolve()
    except (OSError, RuntimeError):
        raise PreviewError("Preview path cannot be resolved.") from None
    try:
        target.relative_to(base)
    except ValueError:
        raise PreviewError("Preview path escapes the project.") from None
    return target


def scan_candidates(root: str) -> List[str]:
    """Find previewable HTML entry points under ``root``.

    Conventional entries come first, then a bounded recursive scan
    (hidden directories, VCS, caches, and virtualenvs skipped).
    Returns project-relative POSIX paths.
    """
    base = Path(root)
    found: List[str] = []

    def _add(relative: str) -> None:
        if relative not in found and len(found) < MAX_CANDIDATES:
            found.append(relative)

    for conventional in CONVENTIONAL_ENTRIES:
        candidate = base / conventional
        try:
            if candidate.is_file():
                _add(conventional)
        except OSError:
            continue
    visited = 0
    stack: List[Tuple[Path, int]] = [(base, 0)]
    while stack and visited < MAX_SCAN_ENTRIES and len(found) < MAX_CANDIDATES:
        directory, depth = stack.pop()
        if depth > MAX_SCAN_DEPTH:
            continue
        try:
            entries = sorted(os.scandir(directory),
                             key=lambda entry: entry.name)
        except OSError:
            continue
        for entry in entries:
            visited += 1
            if visited >= MAX_SCAN_ENTRIES:
                break
            name = entry.name
            if name.startswith("."):
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    if (name.startswith(".") or name in SKIP_DIRS
                            or depth >= MAX_SCAN_DEPTH):
                        continue
                    stack.append((Path(entry.path), depth + 1))
                elif entry.is_file(follow_symlinks=False):
                    lowered = name.lower()
                    if lowered.endswith((".html", ".htm")):
                        try:
                            relative = Path(entry.path).relative_to(
                                base).as_posix()
                        except ValueError:
                            continue
                        _add(relative)
            except OSError:
                continue
    return found


def read_text_preview(path: Path) -> Tuple[Optional[str], bool]:
    """Read ``path`` as UTF-8 text up to ``MAX_TEXT_BYTES``.

    Returns ``(text, truncated)``; ``text`` is ``None`` when the file is
    not decodable text (binary).
    """
    try:
        with open(path, "rb") as handle:
            chunk = handle.read(MAX_TEXT_BYTES + 1)
    except OSError:
        raise PreviewError("Preview file cannot be read.") from None
    truncated = len(chunk) > MAX_TEXT_BYTES
    try:
        return chunk[:MAX_TEXT_BYTES].decode("utf-8"), truncated
    except UnicodeDecodeError:
        return None, False


def media_type_for(relative: str) -> Optional[str]:
    """Media type for raw serving, or ``None`` when not servable."""
    return RAW_MEDIA_TYPES.get(PurePosixPath(relative).suffix.lower())
