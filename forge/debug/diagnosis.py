"""Failure diagnosis (A83): classify a failure from evidence, never by guess.

Input is structured: parsed test results, build diagnostics, or a raw log.
Output is a :class:`Diagnosis` naming a category, the files the evidence
points at, and the exact lines of evidence that produced the classification.
A failure Forge cannot classify is ``unknown`` — with the evidence attached —
rather than a plausible-sounding story.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from forge.builder.diagnostics import Diagnostic, ParseResult
from forge.testing.results import TestParse

COMPILE_ERROR = "compile_error"
IMPORT_ERROR = "import_error"
SYNTAX_ERROR = "syntax_error"
TYPE_ERROR = "type_error"
ASSERTION_FAILURE = "assertion_failure"
RUNTIME_EXCEPTION = "runtime_exception"
MISSING_DEPENDENCY = "missing_dependency"
TIMEOUT = "timeout"
NO_TESTS = "no_tests"
CONFIGURATION = "configuration"
UNKNOWN = "unknown"
CATEGORIES = (COMPILE_ERROR, IMPORT_ERROR, SYNTAX_ERROR, TYPE_ERROR,
              ASSERTION_FAILURE, RUNTIME_EXCEPTION, MISSING_DEPENDENCY,
              TIMEOUT, NO_TESTS, CONFIGURATION, UNKNOWN)

TRACE_FRAME_RE = re.compile(
    r'^\s*File "(?P<file>[^"]+)", line (?P<line>\d+), in (?P<func>.+)$')
#: pytest's failure location line: ``app/calc.py:9: ZeroDivisionError``
PYTEST_LOCATION_RE = re.compile(
    r"^(?P<file>[A-Za-z0-9_./\\-]+\.py):(?P<line>\d+):\s*"
    r"(?P<exception>[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception|Warning|Failed))"
    r"\b(?::\s*(?P<message>.*))?$")
#: pytest's ``E`` lines: the assertion or exception text itself.
PYTEST_ERROR_LINE_RE = re.compile(r"^E\s{2,}(?P<message>\S.*)$")
IMPORT_RE = re.compile(
    r"(?:ModuleNotFoundError|ImportError|cannot import name)\D*(?P<detail>.*)$")
MAX_EVIDENCE = 25


@dataclass
class Diagnosis:
    """What the evidence says about a failure."""

    category: str
    summary: str
    #: Repository-relative files the failure points at, most likely first.
    suspect_files: List[str] = field(default_factory=list)
    #: The exact lines that produced the classification.
    evidence: List[str] = field(default_factory=list)
    #: Failing test ids, when the failure came from a test run.
    failing_tests: List[str] = field(default_factory=list)
    #: Structured diagnostics, when the failure came from a build.
    diagnostics: List[Diagnostic] = field(default_factory=list)
    confidence: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category,
            "summary": self.summary,
            "suspect_files": list(self.suspect_files),
            "evidence": list(self.evidence),
            "failing_tests": list(self.failing_tests),
            "diagnostics": [item.to_dict() for item in self.diagnostics],
            "confidence": round(self.confidence, 3),
        }

    def fingerprint(self) -> str:
        """Stable identity for "is this the same failure again?".

        Used by the debug loop to stop when a fix changed nothing: same
        category, same suspects, same failing tests means the attempt did not
        move the failure, and looping further would be waste.
        """
        import hashlib
        payload = "|".join([
            self.category,
            ",".join(self.suspect_files),
            ",".join(sorted(self.failing_tests)),
            ",".join(sorted({item.message for item in self.diagnostics})),
        ])
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def diagnose_test_failure(parse: TestParse, log: str = "", *,
                          root: Optional[Path] = None) -> Diagnosis:
    """Diagnose a failure that came from a test run."""
    if parse.status == "no_tests":
        return Diagnosis(
            category=NO_TESTS,
            summary="No tests were collected; the test command found nothing "
                    "to run.",
            evidence=(log or "").splitlines()[:MAX_EVIDENCE],
            confidence=0.9)

    suspects: List[str] = []
    evidence: List[str] = []
    category = ASSERTION_FAILURE
    confidence = 0.5
    root_path = Path(root).resolve() if root else None

    for line in (log or "").splitlines():
        frame = TRACE_FRAME_RE.match(line)
        if frame:
            candidate = frame.group("file")
            resolved = _relative(candidate, root_path)
            if resolved and resolved not in suspects:
                suspects.append(resolved)
            evidence.append(line.strip())
            continue
        if IMPORT_RE.search(line):
            category = IMPORT_ERROR
            confidence = 0.8
            evidence.append(line.strip())
            continue
        if "SyntaxError" in line:
            category = SYNTAX_ERROR
            confidence = 0.85
            evidence.append(line.strip())
            continue
        if "TypeError" in line:
            category = TYPE_ERROR
            confidence = 0.7
            evidence.append(line.strip())
            continue
        location = PYTEST_LOCATION_RE.match(line.strip())
        if location:
            resolved = _relative(location.group("file"), root_path)
            if resolved and resolved not in suspects:
                suspects.insert(0, resolved)
            exception = location.group("exception").rsplit(".", 1)[-1]
            category = _category_for_exception(exception, category)
            confidence = max(confidence, 0.8)
            evidence.append(line.strip())
            continue
        if PYTEST_ERROR_LINE_RE.match(line.strip()):
            text = line.strip()
            evidence.append(text)
            category = _category_for_text(text, category)
            confidence = max(confidence, 0.65)
            continue
        if "AssertionError" in line:
            category = ASSERTION_FAILURE
            confidence = max(confidence, 0.75)
            evidence.append(line.strip())

    if parse.failures:
        confidence = max(confidence, 0.6)
        for failure in parse.failures[:10]:
            suite = failure.split("::")[0]
            resolved = _relative(suite, root_path)
            if resolved and resolved not in suspects:
                suspects.append(resolved)
    if parse.inconsistent:
        evidence.append("parser: %s" % parse.inconsistent)
    if not category == ASSERTION_FAILURE and category in (
            IMPORT_ERROR, SYNTAX_ERROR, TYPE_ERROR):
        confidence = max(confidence, 0.7)
    if not evidence:
        category = category if parse.failures else UNKNOWN
        confidence = 0.3 if category == UNKNOWN else confidence
    summary = _summarise(category, parse.failures, evidence)
    return Diagnosis(
        category=category, summary=summary,
        suspect_files=_rank_suspects(suspects),
        evidence=evidence[:MAX_EVIDENCE],
        failing_tests=list(parse.failures)[:50], confidence=confidence)


def diagnose_build_failure(parse: ParseResult, log: str = "", *,
                           root: Optional[Path] = None) -> Diagnosis:
    """Diagnose a failure that came from a build."""
    root_path = Path(root).resolve() if root else None
    errors = parse.errors
    if not errors:
        evidence = [line for line in (log or "").splitlines() if line.strip()]
        return Diagnosis(
            category=CONFIGURATION if parse.failure_markers else UNKNOWN,
            summary=("The build failed but reported no error-level "
                     "diagnostics; the log is attached."
                     if parse.failure_markers else
                     "The build failed and no cause could be identified."),
            evidence=evidence[:MAX_EVIDENCE],
            confidence=0.4 if parse.failure_markers else 0.1)

    suspects: List[str] = []
    for item in errors:
        if item.file:
            resolved = _relative(item.file, root_path)
            if resolved and resolved not in suspects:
                suspects.append(resolved)
    messages = " ".join(item.message for item in errors).lower()
    if "no such file" in messages or "not found" in messages \
            or "undefined reference" in messages:
        category = MISSING_DEPENDENCY
    elif "syntax" in messages or "expected" in messages:
        category = SYNTAX_ERROR
    elif "type" in messages or "mismatched" in messages:
        category = TYPE_ERROR
    else:
        category = COMPILE_ERROR
    return Diagnosis(
        category=category,
        summary="%d build error(s); first: %s" % (
            len(errors), errors[0].message),
        suspect_files=_rank_suspects(suspects),
        evidence=[item.raw for item in errors[:MAX_EVIDENCE]],
        diagnostics=list(errors), confidence=0.9)


#: Exception name -> failure category. Only names Forge can be sure about are
#: mapped; anything else leaves the category alone rather than guessing.
EXCEPTION_CATEGORIES = {
    "AssertionError": ASSERTION_FAILURE,
    "ModuleNotFoundError": IMPORT_ERROR,
    "ImportError": IMPORT_ERROR,
    "SyntaxError": SYNTAX_ERROR,
    "IndentationError": SYNTAX_ERROR,
    "TypeError": TYPE_ERROR,
    "ZeroDivisionError": RUNTIME_EXCEPTION,
    "ValueError": RUNTIME_EXCEPTION,
    "KeyError": RUNTIME_EXCEPTION,
    "IndexError": RUNTIME_EXCEPTION,
    "AttributeError": RUNTIME_EXCEPTION,
    "FileNotFoundError": MISSING_DEPENDENCY,
    "TimeoutError": TIMEOUT,
}


def _category_for_exception(exception: str, current: str) -> str:
    return EXCEPTION_CATEGORIES.get(exception, current)


def _category_for_text(text: str, current: str) -> str:
    lowered = text.lower()
    if "assert" in lowered:
        return ASSERTION_FAILURE
    for name, category in EXCEPTION_CATEGORIES.items():
        if name.lower() in lowered:
            return category
    return current


def _relative(candidate: str, root: Optional[Path]) -> str:
    """Best-effort repository-relative path for a frame or diagnostic file."""
    text = (candidate or "").strip()
    if not text or text.startswith("<"):
        return ""
    path = Path(text.replace("\\", "/"))
    if root is not None and path.is_absolute():
        try:
            return path.resolve().relative_to(root).as_posix()
        except (ValueError, OSError):
            return ""
    if path.is_absolute():
        return ""
    return path.as_posix()


def _rank_suspects(suspects: Sequence[str]) -> List[str]:
    """Put the deepest (most specific) source files first.

    Test files and ``__init__`` modules are demoted: they are usually where a
    failure shows up, not where it originates.
    """
    def weight(path: str) -> Tuple[int, int, str]:
        name = Path(path).name
        is_test = 1 if name.startswith("test_") or "/tests/" in path else 0
        is_init = 1 if name == "__init__.py" else 0
        return (is_test, is_init, path)

    return sorted(set(suspects), key=weight)[:15]


def _summarise(category: str, failures: Sequence[str],
               evidence: Sequence[str]) -> str:
    labels = {
        ASSERTION_FAILURE: "Test assertions failed",
        IMPORT_ERROR: "A module or name could not be imported",
        SYNTAX_ERROR: "The source does not parse",
        TYPE_ERROR: "A type mismatch was raised",
        RUNTIME_EXCEPTION: "An exception was raised at runtime",
        MISSING_DEPENDENCY: "A dependency or symbol is missing",
        TIMEOUT: "The command exceeded its time limit",
        NO_TESTS: "No tests were collected",
        CONFIGURATION: "The tooling failed before running any test",
        UNKNOWN: "The failure could not be classified",
    }
    detail = ""
    if failures:
        detail = " (%s)" % ", ".join(list(failures)[:3])
    elif evidence:
        detail = " (%s)" % evidence[-1][:160]
    return "%s%s." % (labels.get(category, category.replace("_", " ")), detail)
