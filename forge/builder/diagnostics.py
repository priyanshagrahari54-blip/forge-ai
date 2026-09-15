"""Compiler and tool diagnostics (A83): structured errors, not raw text.

A build report that only contains a log tail cannot answer "what broke, and
where". These parsers turn real tool output into structured
:class:`Diagnostic` records with severity, file, line, and rule.

Honesty rules:

* A line is only reported as a diagnostic when a pattern actually matched it.
* ``parser="auto"`` selects by recognisable markers and reports which parser
  it chose, so a wrong guess is visible rather than silent.
* Unparsed output is preserved in :attr:`ParseResult.unparsed_lines` — a
  parser that understood nothing says so instead of returning an empty list
  that looks like a clean build.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

ERROR = "error"
WARNING = "warning"
NOTE = "note"
SEVERITIES = (ERROR, WARNING, NOTE)

#: gcc / clang: ``src/main.c:12:5: error: expected ';'``
GCC_RE = re.compile(
    r"^(?P<file>[^\s:]+):(?P<line>\d+)(?::(?P<column>\d+))?:\s*"
    r"(?P<severity>error|warning|note|fatal error):\s*(?P<message>.*)$",
    re.MULTILINE)
#: MSVC: ``src\main.c(12): error C2065: 'x': undeclared identifier``
MSVC_RE = re.compile(
    r"^(?P<file>[^\s(]+)\((?P<line>\d+)(?:,(?P<column>\d+))?\):\s*"
    r"(?P<severity>error|warning)\s+(?P<rule>[A-Z]+\d+):\s*(?P<message>.*)$",
    re.MULTILINE)
#: rustc: ``error[E0308]: mismatched types`` then `` --> src/main.rs:3:5``
RUSTC_HEAD_RE = re.compile(
    r"^(?P<severity>error|warning)(?:\[(?P<rule>[A-Z]\d+)\])?:\s*"
    r"(?P<message>.*)$")
RUSTC_LOC_RE = re.compile(
    r"^\s*-->\s+(?P<file>[^\s:]+):(?P<line>\d+):(?P<column>\d+)")
#: javac: ``Main.java:12: error: cannot find symbol``
JAVAC_RE = re.compile(
    r"^(?P<file>[^\s:]+\.[A-Za-z]+):(?P<line>\d+):\s*"
    r"(?P<severity>error|warning):\s*(?P<message>.*)$", re.MULTILINE)
#: TypeScript: ``src/app.ts(12,5): error TS2304: Cannot find name 'x'.``
TSC_RE = re.compile(
    r"^(?P<file>[^\s(]+)\((?P<line>\d+),(?P<column>\d+)\):\s*"
    r"(?P<severity>error|warning)\s+(?P<rule>[A-Z]+\d+):\s*(?P<message>.*)$",
    re.MULTILINE)
#: Python traceback frames: ``  File "app.py", line 12, in main``
PY_FRAME_RE = re.compile(
    r'^\s*File "(?P<file>[^"]+)", line (?P<line>\d+)(?:, in (?P<func>.*))?$',
    re.MULTILINE)
PY_ERROR_RE = re.compile(r"^(?P<rule>[A-Za-z_.]*(?:Error|Exception|Warning)):"
                         r"\s*(?P<message>.*)$")
MAX_DIAGNOSTICS = 2_000
PARSERS = ("auto", "gcc", "msvc", "rust", "java", "tsc", "python", "generic")


@dataclass(frozen=True)
class Diagnostic:
    """One structured problem reported by a tool."""

    severity: str
    message: str
    file: str = ""
    line: int = 0
    column: int = 0
    rule: str = ""
    parser: str = "generic"
    raw: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "severity": self.severity,
            "message": self.message,
            "file": self.file,
            "line": self.line,
            "column": self.column,
            "rule": self.rule,
            "parser": self.parser,
            "raw": self.raw[:500],
        }


@dataclass
class ParseResult:
    """Everything a parser made of a tool's output."""

    #: Set by :func:`parse_diagnostics` once the parser is known.
    parser: str = "generic"
    diagnostics: List[Diagnostic] = field(default_factory=list)
    unparsed_lines: List[str] = field(default_factory=list)
    #: Set when the output shows a failure the line parsers did not capture.
    failure_markers: List[str] = field(default_factory=list)

    @property
    def errors(self) -> List[Diagnostic]:
        return [item for item in self.diagnostics if item.severity == ERROR]

    @property
    def warnings(self) -> List[Diagnostic]:
        return [item for item in self.diagnostics
                if item.severity == WARNING]

    def summary(self) -> Dict[str, Any]:
        return {
            "parser": self.parser,
            "diagnostics": len(self.diagnostics),
            "errors": len(self.errors),
            "warnings": len(self.warnings),
            "unparsed_lines": len(self.unparsed_lines),
            "failure_markers": list(self.failure_markers),
            "files": sorted({item.file for item in self.diagnostics
                             if item.file}),
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "summary": self.summary(),
            "diagnostics": [item.to_dict() for item in self.diagnostics],
        }


def _normalise_path(value: str) -> str:
    """Normalise a tool-reported path for comparison and display.

    Compilers report ``./src/x.c`` and ``src\\x.c`` interchangeably; the
    index and the debug loop both need one spelling.
    """
    text = (value or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def detect_parser(output: str) -> str:
    """Pick a parser from recognisable markers in the output."""
    if RUSTC_LOC_RE.search(output) or "could not compile" in output:
        return "rust"
    if TSC_RE.search(output) or re.search(r"\berror TS\d+:", output):
        return "tsc"
    if MSVC_RE.search(output):
        return "msvc"
    if PY_FRAME_RE.search(output):
        return "python"
    if JAVAC_RE.search(output):
        return "java"
    if GCC_RE.search(output):
        return "gcc"
    return "generic"


def parse_diagnostics(output: str, parser: str = "auto", *,
                      limit: int = MAX_DIAGNOSTICS) -> ParseResult:
    """Parse tool output into structured diagnostics."""
    chosen = parser if parser in PARSERS and parser != "auto" else (
        detect_parser(output or ""))
    handler = {
        "gcc": _parse_gcc,
        "msvc": _parse_msvc,
        "rust": _parse_rust,
        "java": _parse_java,
        "tsc": _parse_tsc,
        "python": _parse_python,
        "generic": _parse_generic,
    }[chosen]
    result = handler(output or "", limit)
    result.parser = chosen
    return result


def _bounded(result: ParseResult, limit: int) -> ParseResult:
    if len(result.diagnostics) > limit:
        result.diagnostics = result.diagnostics[:limit]
    if len(result.unparsed_lines) > 200:
        result.unparsed_lines = result.unparsed_lines[:200]
    return result


def _mark_failures(result: ParseResult, output: str) -> ParseResult:
    for marker in ("error: could not compile", "make: *** ",
                   "make[1]: *** ", "ninja: build stopped",
                   "fatal error:", "FAILED:",
                   "Traceback (most recent call last)",
                   "collect2: error", "linker command failed",
                   "recipe for target", "Error: ", "npm ERR!"):
        if marker in output:
            result.failure_markers.append(marker.strip())
    return result


def _parse_gcc(output: str, limit: int) -> ParseResult:
    diagnostics: List[Diagnostic] = []
    unparsed: List[str] = []
    for line in output.splitlines():
        match = GCC_RE.match(line.rstrip())
        if not match:
            if line.strip():
                unparsed.append(line.strip())
            continue
        diagnostics.append(Diagnostic(
            severity=ERROR if "error" in match.group("severity") else (
                WARNING if match.group("severity") == "warning" else NOTE),
            message=match.group("message").strip(),
            file=_normalise_path(match.group("file")),
            line=int(match.group("line")),
            column=int(match.group("column") or 0),
            parser="gcc", raw=line.strip()))
        if len(diagnostics) >= limit:
            break
    return _bounded(_mark_failures(
        ParseResult(diagnostics=diagnostics, unparsed_lines=unparsed),
        output), limit)


def _parse_msvc(output: str, limit: int) -> ParseResult:
    diagnostics: List[Diagnostic] = []
    unparsed: List[str] = []
    for line in output.splitlines():
        match = MSVC_RE.match(line.rstrip())
        if not match:
            if line.strip():
                unparsed.append(line.strip())
            continue
        diagnostics.append(Diagnostic(
            severity=match.group("severity"),
            message=match.group("message").strip(),
            file=_normalise_path(match.group("file")),
            line=int(match.group("line")),
            column=int(match.group("column") or 0),
            rule=match.group("rule"), parser="msvc", raw=line.strip()))
    return _bounded(_mark_failures(
        ParseResult(diagnostics=diagnostics, unparsed_lines=unparsed),
        output), limit)


def _parse_rust(output: str, limit: int) -> ParseResult:
    diagnostics: List[Diagnostic] = []
    unparsed: List[str] = []
    pending: Optional[Dict[str, Any]] = None
    for line in output.splitlines():
        head = RUSTC_HEAD_RE.match(line.rstrip())
        if head:
            pending = {
                "severity": head.group("severity"),
                "rule": head.group("rule") or "",
                "message": head.group("message").strip(),
                "raw": line.strip(),
            }
            continue
        location = RUSTC_LOC_RE.match(line)
        if location and pending is not None:
            diagnostics.append(Diagnostic(
                severity=pending["severity"], message=pending["message"],
                file=_normalise_path(location.group("file")),
                line=int(location.group("line")),
                column=int(location.group("column")),
                rule=pending["rule"], parser="rust", raw=pending["raw"]))
            pending = None
            if len(diagnostics) >= limit:
                break
            continue
        if line.strip():
            unparsed.append(line.strip())
    if pending is not None:
        # A rustc error with no location line is still an error; report it
        # without inventing a file for it.
        diagnostics.append(Diagnostic(
            severity=pending["severity"], message=pending["message"],
            rule=pending["rule"], parser="rust", raw=pending["raw"]))
    return _bounded(_mark_failures(
        ParseResult(diagnostics=diagnostics, unparsed_lines=unparsed),
        output), limit)


def _parse_java(output: str, limit: int) -> ParseResult:
    diagnostics: List[Diagnostic] = []
    unparsed: List[str] = []
    for line in output.splitlines():
        match = JAVAC_RE.match(line.rstrip())
        if not match:
            if line.strip():
                unparsed.append(line.strip())
            continue
        diagnostics.append(Diagnostic(
            severity=match.group("severity"),
            message=match.group("message").strip(),
            file=_normalise_path(match.group("file")),
            line=int(match.group("line")),
            parser="java", raw=line.strip()))
    return _bounded(_mark_failures(
        ParseResult(diagnostics=diagnostics, unparsed_lines=unparsed),
        output), limit)


def _parse_tsc(output: str, limit: int) -> ParseResult:
    diagnostics: List[Diagnostic] = []
    unparsed: List[str] = []
    for line in output.splitlines():
        match = TSC_RE.match(line.rstrip())
        if not match:
            if line.strip():
                unparsed.append(line.strip())
            continue
        diagnostics.append(Diagnostic(
            severity=match.group("severity"),
            message=match.group("message").strip(),
            file=_normalise_path(match.group("file")),
            line=int(match.group("line")),
            column=int(match.group("column")),
            rule=match.group("rule"), parser="tsc", raw=line.strip()))
    return _bounded(_mark_failures(
        ParseResult(diagnostics=diagnostics, unparsed_lines=unparsed),
        output), limit)


def _parse_python(output: str, limit: int) -> ParseResult:
    diagnostics: List[Diagnostic] = []
    unparsed: List[str] = []
    frames: List[Any] = []
    for line in output.splitlines():
        frame = PY_FRAME_RE.match(line)
        if frame:
            frames.append((frame.group("file"), int(frame.group("line"))))
            continue
        error = PY_ERROR_RE.match(line.strip())
        if error:
            file_name, line_no = frames[-1] if frames else ("", 0)
            diagnostics.append(Diagnostic(
                severity=ERROR, message=error.group("message").strip(),
                file=_normalise_path(file_name), line=line_no,
                rule=error.group("rule").rsplit(".", 1)[-1],
                parser="python", raw=line.strip()))
            if len(diagnostics) >= limit:
                break
            continue
        if line.strip():
            unparsed.append(line.strip())
    return _bounded(_mark_failures(
        ParseResult(diagnostics=diagnostics, unparsed_lines=unparsed),
        output), limit)


def _parse_generic(output: str, limit: int) -> ParseResult:
    """Last resort: recognise common error/warning prefixes, keep the rest.

    This deliberately does *not* try to guess structure. Lines that do not
    match are preserved as unparsed so the caller can see that the output was
    not understood, and failure markers are still detected.
    """
    diagnostics: List[Diagnostic] = []
    unparsed: List[str] = []
    generic = re.compile(
        r"^(?P<severity>error|warning|fatal|fail(?:ed|ure)?)"
        r"(?:\[[^\]]*\])?:\s*(?P<message>.+)$", re.IGNORECASE)
    for line in output.splitlines():
        match = generic.match(line.strip())
        if match:
            severity = match.group("severity").lower()
            diagnostics.append(Diagnostic(
                severity=WARNING if severity == "warning" else ERROR,
                message=match.group("message").strip(),
                parser="generic", raw=line.strip()))
            if len(diagnostics) >= limit:
                break
            continue
        if line.strip():
            unparsed.append(line.strip())
    return _bounded(_mark_failures(
        ParseResult(diagnostics=diagnostics, unparsed_lines=unparsed),
        output), limit)
