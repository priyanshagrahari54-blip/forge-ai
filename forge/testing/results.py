"""Structured test results (A83): real per-test outcomes, parsed from output.

A test run that reports only an exit code cannot tell the debugging loop
*which* test failed, so it cannot rerun it. These parsers extract individual
test outcomes from real tool output.

Parsers are conservative on purpose:

* a test is only recorded when its outcome line was actually seen;
* the summary counts the tool itself printed are captured separately and
  compared against the parsed cases — a mismatch is reported in
  ``ParseResult.inconsistent`` rather than silently trusting either source;
* "no tests collected" is its own status, never ``passed``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

PASSED = "passed"
FAILED = "failed"
ERROR = "error"
SKIPPED = "skipped"
XFAIL = "xfail"
XPASS = "xpass"
OUTCOMES = (PASSED, FAILED, ERROR, SKIPPED, XFAIL, XPASS)
NO_TESTS = "no_tests"
PARSERS = ("auto", "pytest", "cargo", "go", "junit", "generic")

#: pytest -v: ``tests/test_x.py::test_y PASSED``
PYTEST_CASE_RE = re.compile(
    r"^(?P<id>[^\s]+::[^\s]+)\s+(?P<outcome>PASSED|FAILED|ERROR|SKIPPED|"
    r"XFAIL|XPASS)(?:\s+\[[^\]]*\])?\s*$")
#: pytest short summary: ``FAILED tests/test_x.py::test_y - AssertionError``
PYTEST_SHORT_RE = re.compile(
    r"^(?P<outcome>FAILED|ERROR|XFAIL|XPASS|SKIPPED)\s+(?P<id>[^\s:]+::[^\s]+)")
PYTEST_SUMMARY_RE = re.compile(
    r"(?:(?P<passed>\d+) passed)?[^=\n]*?"
    r"(?:(?P<failed>\d+) failed)?[^=\n]*?"
    r"(?:(?P<errors>\d+) errors?)?[^=\n]*?"
    r"(?:(?P<skipped>\d+) skipped)?[^=\n]*?"
    r"(?:(?P<xfail>\d+) xfailed)?")
PYTEST_COUNT_RE = re.compile(r"(\d+)\s+(passed|failed|error|errors|skipped|"
                             r"xfailed|xpassed|deselected|warnings?)")
PYTEST_NO_TESTS_RE = re.compile(r"no tests ran|collected 0 items")
#: cargo test: ``test tests::it_works ... ok``
CARGO_CASE_RE = re.compile(
    r"^test\s+(?P<id>[^\s]+)\s+\.\.\.\s+(?P<outcome>ok|FAILED|ignored)")
CARGO_SUMMARY_RE = re.compile(
    r"test result: (?P<outcome>ok|FAILED)\.\s+(?P<passed>\d+) passed;\s+"
    r"(?P<failed>\d+) failed;\s+(?P<ignored>\d+) ignored")
#: go test -v: ``--- PASS: TestFoo (0.00s)``
GO_CASE_RE = re.compile(
    r"^\s*---\s+(?P<outcome>PASS|FAIL|SKIP):\s+(?P<id>[^\s]+)")
MAX_CASES = 20_000


@dataclass(frozen=True)
class TestCase:
    """One test's measured outcome."""

    id: str
    outcome: str
    suite: str = ""
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "outcome": self.outcome, "suite": self.suite,
                "message": self.message[:500]}


@dataclass
class TestParse:
    """Structured result of parsing one test command's output."""

    parser: str
    status: str                       # passed | failed | no_tests | unknown
    cases: List[TestCase] = field(default_factory=list)
    #: Counts the tool itself printed.
    reported: Dict[str, int] = field(default_factory=dict)
    failures: List[str] = field(default_factory=list)
    unparsed: List[str] = field(default_factory=list)
    #: Set when parsed cases and the tool's own summary disagree.
    inconsistent: str = ""

    @property
    def counts(self) -> Dict[str, int]:
        counts = {outcome: 0 for outcome in OUTCOMES}
        for case in self.cases:
            counts[case.outcome] = counts.get(case.outcome, 0) + 1
        return counts

    @property
    def passed(self) -> bool:
        return self.status == PASSED

    def summary(self) -> Dict[str, Any]:
        return {
            "parser": self.parser,
            "status": self.status,
            "cases": len(self.cases),
            "counts": self.counts,
            "reported": dict(self.reported),
            "failures": list(self.failures)[:50],
            "inconsistent": self.inconsistent,
            "unparsed": len(self.unparsed),
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "summary": self.summary(),
            "cases": [case.to_dict() for case in self.cases[:MAX_CASES]],
        }


def detect_parser(output: str) -> str:
    if CARGO_SUMMARY_RE.search(output) or CARGO_CASE_RE.search(output):
        return "cargo"
    if GO_CASE_RE.search(output):
        return "go"
    if PYTEST_CASE_RE.search(output) or PYTEST_SHORT_RE.search(output) \
            or "=====" in output and "test session starts" in output:
        return "pytest"
    return "generic"


def parse_test_output(output: str, parser: str = "auto", *,
                      return_code: Optional[int] = None) -> TestParse:
    """Parse a test command's output into structured per-test results."""
    text = output or ""
    chosen = parser if parser in PARSERS and parser != "auto" else (
        detect_parser(text))
    handler = {"pytest": _parse_pytest, "cargo": _parse_cargo,
               "go": _parse_go, "generic": _parse_generic}[
        chosen if chosen in ("pytest", "cargo", "go") else "generic"]
    result = handler(text)
    result.parser = chosen
    if result.status == "unknown":
        # No parseable per-test output: fall back to the exit code, and say
        # that is what happened.
        if PYTEST_NO_TESTS_RE.search(text) or (
                return_code == 5 and "no tests ran" in text.lower()):
            result.status = NO_TESTS
        elif return_code == 0:
            result.status = PASSED
            result.inconsistent = ("exit code 0 but no per-test outcomes "
                                   "were parsed")
        elif return_code is not None:
            result.status = FAILED
            result.inconsistent = ("non-zero exit code with no per-test "
                                   "outcomes parsed")
    return result


def _consistency(result: TestParse) -> TestParse:
    counts = result.counts
    reported = result.reported
    if not reported:
        return result
    checks = (("passed", "passed"), ("failed", "failed"),
              ("skipped", "skipped"))
    for key, outcome in checks:
        if key in reported and reported[key] != counts.get(outcome, 0):
            result.inconsistent = (
                "tool reported %d %s but %d were parsed"
                % (reported[key], key, counts.get(outcome, 0)))
            break
    return result


def _suite_of(case_id: str) -> str:
    return case_id.split("::")[0] if "::" in case_id else case_id


def _parse_pytest(output: str) -> TestParse:
    cases: Dict[str, TestCase] = {}
    failures: List[str] = []
    unparsed: List[str] = []
    for line in output.splitlines():
        stripped = line.rstrip()
        match = PYTEST_CASE_RE.match(stripped.strip())
        if match:
            outcome = match.group("outcome").lower()
            case_id = match.group("id")
            cases[case_id] = TestCase(id=case_id, outcome=outcome,
                                      suite=_suite_of(case_id))
            if outcome in (FAILED, ERROR):
                failures.append(case_id)
            continue
        short = PYTEST_SHORT_RE.match(stripped.strip())
        if short:
            outcome = short.group("outcome").lower()
            case_id = short.group("id")
            message = stripped.split(" - ", 1)[-1] if " - " in stripped else ""
            cases.setdefault(case_id, TestCase(
                id=case_id, outcome=outcome, suite=_suite_of(case_id),
                message=message))
            if case_id not in failures and outcome in (FAILED, ERROR):
                failures.append(case_id)
            continue
        if stripped.strip() and len(unparsed) < 200:
            unparsed.append(stripped.strip())
    reported: Dict[str, int] = {}
    for value, kind in PYTEST_COUNT_RE.findall(output):
        normalised = {"errors": "error", "warnings": "warning",
                      "deselected": "deselected"}.get(kind, kind)
        reported[normalised] = reported.get(normalised, 0) + int(value)
    if not cases:
        return TestParse(parser="pytest", status="unknown",
                         reported=reported, unparsed=unparsed)
    failed = (reported.get("failed", 0) or reported.get("error", 0)
              or sum(1 for case in cases.values()
                     if case.outcome in (FAILED, ERROR)))
    status = FAILED if failed else PASSED
    return _consistency(TestParse(
        parser="pytest", status=status, cases=list(cases.values()),
        reported=reported, failures=failures, unparsed=unparsed))


def _parse_cargo(output: str) -> TestParse:
    cases: List[TestCase] = []
    failures: List[str] = []
    unparsed: List[str] = []
    for line in output.splitlines():
        match = CARGO_CASE_RE.match(line.strip())
        if match:
            outcome = {"ok": PASSED, "FAILED": FAILED,
                       "ignored": SKIPPED}[match.group("outcome")]
            case_id = match.group("id")
            cases.append(TestCase(id=case_id, outcome=outcome,
                                  suite=_suite_of(case_id)))
            if outcome == FAILED:
                failures.append(case_id)
            continue
        if line.strip() and len(unparsed) < 200:
            unparsed.append(line.strip())
    reported: Dict[str, int] = {}
    summary = CARGO_SUMMARY_RE.search(output)
    if summary:
        reported = {"passed": int(summary.group("passed")),
                    "failed": int(summary.group("failed")),
                    "skipped": int(summary.group("ignored"))}
    if not cases and not summary:
        return TestParse(parser="cargo", status="unknown",
                         unparsed=unparsed)
    status = FAILED if (reported.get("failed") or failures) else PASSED
    return _consistency(TestParse(
        parser="cargo", status=status, cases=cases, reported=reported,
        failures=failures, unparsed=unparsed))


def _parse_go(output: str) -> TestParse:
    cases: List[TestCase] = []
    failures: List[str] = []
    unparsed: List[str] = []
    for line in output.splitlines():
        match = GO_CASE_RE.match(line.rstrip())
        if match:
            outcome = {"PASS": PASSED, "FAIL": FAILED,
                       "SKIP": SKIPPED}[match.group("outcome")]
            case_id = match.group("id")
            cases.append(TestCase(id=case_id, outcome=outcome,
                                  suite=_suite_of(case_id)))
            if outcome == FAILED:
                failures.append(case_id)
            continue
        if line.strip() and len(unparsed) < 200:
            unparsed.append(line.strip())
    if not cases:
        return TestParse(parser="go", status="unknown", unparsed=unparsed)
    status = FAILED if failures else PASSED
    return TestParse(parser="go", status=status, cases=cases,
                     failures=failures, unparsed=unparsed)


def _parse_generic(output: str) -> TestParse:
    """No per-test parser matched: report what is knowable, nothing more."""
    unparsed = [line.strip() for line in output.splitlines()
                if line.strip()][:200]
    return TestParse(parser="generic", status="unknown", unparsed=unparsed)
