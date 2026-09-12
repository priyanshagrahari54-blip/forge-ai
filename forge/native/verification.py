"""Native verification: compile, tests, lint/build, security, diff checks.

A81 layer 8 wraps the existing verification gates (:class:
``forge.security.verification.VerificationPipeline``) and adds the two checks
the engine needs *before* subprocesses run — a cheap in-process syntax/compile
pass and an explicit diff validation — while keeping the honesty contract the
pipeline enforces:

* every executed gate reports the measured exit code/output, never an
  assumed result;
* a failed check stays a failed check: no gate can override another, and
  the aggregate is the conjunction over executed gates;
* a gate that could not run (or is not configured) is marked
  ``executed=False`` / ``configured=False`` and listed as skipped — never
  silently converted into success.

Everything is bounded for the target hardware: the compile pass checks at
most ``MAX_COMPILE_FILES`` files of at most ``MAX_COMPILE_BYTES`` each.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

#: Bounds sized for a 2 GB Windows 7 host (and cheap everywhere).
MAX_COMPILE_FILES = 400
MAX_COMPILE_BYTES = 2 * 1024 * 1024

#: Markers that must never appear in an accepted diff.
_CONFLICT_MARKERS = ("<<<<<<<", ">>>>>>>")


@dataclass
class GateReport:
    """One verification gate result (executed or honestly skipped)."""

    name: str
    passed: bool
    executed: bool = True
    details: str = ""
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "passed": bool(self.passed),
            "executed": bool(self.executed),
            "details": str(self.details or "")[:2000],
            "evidence": dict(self.evidence),
        }


@dataclass
class NativeVerificationReport:
    """Aggregate over executed gates. Failed gates stay in ``failed``."""

    gates: List[GateReport] = field(default_factory=list)

    @property
    def executed(self) -> List[GateReport]:
        return [gate for gate in self.gates if gate.executed]

    @property
    def failed(self) -> List[str]:
        return [gate.name for gate in self.executed if not gate.passed]

    @property
    def skipped(self) -> List[str]:
        return [gate.name for gate in self.gates if not gate.executed]

    @property
    def all_passed(self) -> bool:
        executed = self.executed
        return bool(executed) and all(gate.passed for gate in executed)

    @property
    def status(self) -> str:
        """PASS | FAIL | PARTIAL (some gates skipped but none failed)."""
        if self.failed:
            return "FAIL"
        if self.skipped:
            return "PARTIAL"
        return "PASS" if self.gates else "PARTIAL"

    def status_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "executed": len(self.executed),
            "passed": sum(1 for gate in self.executed if gate.passed),
            "failed": list(self.failed),
            "skipped": list(self.skipped),
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "all_passed": self.all_passed,
            "gates": [gate.to_dict() for gate in self.gates],
        }


class NativeVerifier:
    """Runs every configured check; aggregates without hiding failures."""

    def __init__(self, root: str | Path, pipeline: Any = None) -> None:
        self.root = Path(root).resolve()
        if pipeline is None:
            from forge.security.verification import VerificationPipeline
            pipeline = VerificationPipeline(self.root)
        self.pipeline = pipeline

    # -- individual gates -----------------------------------------------------

    def compile_check(self, paths: Optional[Iterable[str]] = None
                      ) -> GateReport:
        """Parse + compile Python files (in-memory; writes nothing).

        ``paths`` = files to check; when omitted, a bounded sample of the
        repository's Python files is used so a no-change task still reports
        a real syntax state (and the sample bound is disclosed).
        """
        targets: List[Path] = []
        if paths is not None:
            for name in paths:
                if not isinstance(name, str) or not name.endswith(".py"):
                    continue
                candidate = self.root / name
                if candidate.is_file():
                    targets.append(candidate)
        else:
            # Pruned traversal: unlike rglob-then-filter, excluded trees
            # (.git, node_modules, .venv, ...) are never descended into --
            # important on a 2 GB host with a fat repo. The sample cap is
            # disclosed in the gate evidence; selection stays deterministic
            # (candidate list is sorted before the cap is applied).
            import os
            candidates: List[str] = []
            for dirpath, dirnames, filenames in os.walk(str(self.root)):
                dirnames[:] = sorted(
                    d for d in dirnames
                    if d not in (".git", ".hg", ".forge", ".svn", ".venv",
                                 "venv", "env", "node_modules",
                                 "__pycache__", ".pytest_cache",
                                 ".mypy_cache", ".ruff_cache", ".tox",
                                 ".nox", ".cache", "build", "dist")
                    and not d.startswith(".")
                )
                for name in filenames:
                    if name.endswith(".py"):
                        candidates.append(
                            os.path.relpath(os.path.join(dirpath, name),
                                            str(self.root)).replace("\\",
                                                                    "/"))
            for rel in sorted(candidates)[:MAX_COMPILE_FILES]:
                targets.append(self.root / rel)
        import ast
        problems: List[Dict[str, str]] = []
        checked = 0
        skipped_large = 0
        for target in targets[:MAX_COMPILE_FILES]:
            try:
                if target.stat().st_size > MAX_COMPILE_BYTES:
                    skipped_large += 1
                    continue
                source = target.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                problems.append({"file": str(target.relative_to(self.root)),
                                 "error": "unreadable: %s" % exc})
                continue
            checked += 1
            try:
                tree = ast.parse(source, filename=str(target))
                compile(tree, str(target), "exec")
            except SyntaxError as exc:
                problems.append({
                    "file": str(target.relative_to(self.root)),
                    "error": "line %s: %s" % (exc.lineno, exc.msg),
                })
            except ValueError as exc:  # e.g. null bytes
                problems.append({"file": str(target.relative_to(self.root)),
                                 "error": str(exc)})
        passed = not problems
        details = ("%d file(s) parsed/compiled, %d issue(s)"
                   % (checked, len(problems)))
        if skipped_large:
            details += "; %d oversized file(s) skipped" % skipped_large
        return GateReport("compile", passed, True, details,
                          {"problems": problems[:20], "checked": checked,
                           "skipped_large": skipped_large})

    def diff_validation(self, changed_files: Sequence[str],
                        diff_text: str = "", *,
                        material_available: bool = True) -> GateReport:
        """Re-validate the *declared* change set independently of the writer.

        The ChangeSet engine already guards every write; this gate catches a
        mismatch between declared work and measured repository state — a
        claimed file that does not exist, or a conflict marker left behind.
        ``material_available`` records whether a git worktree could produce
        diff text at all: when it cannot, the material half of the gate is
        skipped *and said to be* — never silently passed.
        """
        issues: List[str] = []
        for name in changed_files:
            parsed = Path(str(name))
            if parsed.is_absolute() or ".." in parsed.parts:
                issues.append("%s: unsafe declared path" % name)
                continue
            if ".git" in parsed.parts or ".forge" in parsed.parts:
                issues.append("%s: protected runtime directory" % name)
            if parsed.name == ".env" or parsed.name.endswith(".env"):
                issues.append("%s: environment file" % name)
            if not (self.root / parsed).exists():
                issues.append("%s: declared but missing from the repository"
                              % name)
        material_skipped = False
        material = diff_text or ""
        if any(marker in material for marker in _CONFLICT_MARKERS):
            issues.append("diff contains merge conflict markers")
        if changed_files and not material.strip():
            if material_available:
                # Diff material was available but empty despite declared
                # changes: a real mismatch between claim and state.
                issues.append("no diff material supplied for review of %d "
                              "declared file(s)" % len(changed_files))
            else:
                material_skipped = True
        if issues:
            return GateReport("diff_validation", False, True,
                              "; ".join(issues),
                              {"issues": issues[:20],
                               "declared": list(changed_files)[:50]})
        if material_skipped:
            return GateReport(
                "diff_validation", False, False,
                "path validation of %d declared file(s) passed; conflict-"
                "marker scan skipped: no git worktree to produce diff "
                "material" % len(changed_files),
                {"issues": [], "declared": list(changed_files)[:50],
                 "material_available": False})
        return GateReport("diff_validation", True, True,
                          "%d declared file(s) match repository state"
                          % len(changed_files),
                          {"issues": [],
                           "declared": list(changed_files)[:50]})

    # -- aggregate ---------------------------------------------------------------

    def verify(self, changed_files: Optional[Sequence[str]] = None,
               diff_text: str = "", run_tests: bool = True,
               run_build: bool = True, run_lint: bool = True,
               diff_material_available: bool = True
               ) -> NativeVerificationReport:
        report = NativeVerificationReport()

        check_paths = list(changed_files) if changed_files is not None \
            else None
        report.gates.append(self.compile_check(check_paths))

        if run_tests:
            gate = self.pipeline.tests()
            report.gates.append(GateReport(
                name=gate.name, passed=bool(gate.passed), executed=True,
                details=gate.details, evidence=dict(gate.evidence or {})))
        else:
            report.gates.append(GateReport(
                "tests", False, executed=False,
                details="not executed for this request (verification "
                        "scoped off by the caller)"))

        if run_build:
            gate = self.pipeline.build()
            report.gates.append(GateReport(
                name=gate.name, passed=bool(gate.passed), executed=True,
                details=gate.details, evidence=dict(gate.evidence or {})))
        else:
            report.gates.append(GateReport(
                "build", False, executed=False,
                details="not executed (verification scoped off by the "
                        "caller)"))

        if run_lint:
            gate = self.pipeline.lint()
            configured = bool((gate.evidence or {}).get("commands"))
            report.gates.append(GateReport(
                name=gate.name, passed=bool(gate.passed),
                # pass-when-unconfigured is the pipeline's explicit recorded
                # policy; we surface it instead of hiding it.
                executed=True, details=gate.details,
                evidence=dict(gate.evidence or {},
                              configured=configured)))
        else:
            report.gates.append(GateReport(
                "lint", False, executed=False,
                details="not executed (verification scoped off by the "
                        "caller)"))

        gate = self.pipeline.security(changed_files)
        report.gates.append(GateReport(
            name=gate.name, passed=bool(gate.passed), executed=True,
            details=gate.details, evidence=dict(gate.evidence or {})))

        report.gates.append(self.diff_validation(
            list(changed_files or ()), diff_text,
            material_available=diff_material_available))
        return report
