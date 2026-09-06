from __future__ import annotations

import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


@dataclass
class GateResult:
    name: str
    passed: bool
    details: str = ""
    evidence: dict = field(default_factory=dict)


@dataclass
class VerificationResult:
    passed: bool
    gates: list[GateResult]

    @property
    def failures(self) -> list[GateResult]:
        return [gate for gate in self.gates if not gate.passed]


class VerificationPipeline:
    """Runs executable acceptance gates and records their evidence."""

    SECRET_PATTERNS = (
        re.compile(r"(?:api[_-]?key|secret|password|token)\s*[:=]\s*['\"][^'\"]{8,}", re.I),
        re.compile(r"(?:AKIA|ASIA)[A-Z0-9]{16}"),
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
        re.compile(r"(?:postgres|mysql|mongodb(?:\+srv)?)://[^\s'\"]+", re.I),
    )
    DANGEROUS_PATTERNS = (
        re.compile(r"\b(?:eval|exec)\s*\("),
        re.compile(r"\bos\.system\s*\("),
        re.compile(r"subprocess\.(?:run|Popen|call)\([^\n]*shell\s*=\s*True", re.I),
        re.compile(r"(?:^|[\s\"'])sh\s+-c(?:[\s\"'])"),
        re.compile(r"(?:^|[\s\"'])\.\.(?:[/\\])"),
    )

    def __init__(self, root: str | Path = "."):
        self.root = Path(root).resolve()

    def _run(self, command: list[str], timeout: int = 120):
        try:
            return subprocess.run(command, cwd=self.root, text=True, capture_output=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return None

    def tests(self) -> GateResult:
        process = self._run([sys.executable, "-m", "pytest", "-q"])
        output = (process.stdout + process.stderr) if process else "test command failed"
        no_tests = bool(process and process.returncode == 5 and "no tests ran" in output.lower())
        return GateResult("tests", bool(process and (process.returncode == 0 or no_tests)), output[-4000:], {
            "returncode": process.returncode if process else None,
            "no_tests": no_tests,
            "command": [sys.executable, "-m", "pytest", "-q"],
        })

    def build(self) -> GateResult:
        process = self._run([sys.executable, "-m", "compileall", "-q", "."])
        return GateResult("build", bool(process and process.returncode == 0),
                          (process.stdout + process.stderr)[-2000:] if process else "build failed",
                          {"command": [sys.executable, "-m", "compileall", "-q", "."],
                           "returncode": process.returncode if process else None})

    def lint(self) -> GateResult:
        commands: list[list[str]] = []
        if (self.root / "pyproject.toml").exists():
            text = (self.root / "pyproject.toml").read_text(encoding="utf-8")
            if "ruff" in text:
                commands.append([sys.executable, "-m", "ruff", "check", "."])
            if "mypy" in text:
                commands.append([sys.executable, "-m", "mypy", "."])
        if not commands:
            return GateResult("lint/type", True, "No configured lint/type checker; compile verification remains active", {"commands": []})
        evidence = []
        passed = True
        for command in commands:
            process = self._run(command)
            ok = bool(process and process.returncode == 0)
            passed = passed and ok
            evidence.append({"command": command, "returncode": process.returncode if process else None,
                             "output": ((process.stdout + process.stderr)[-2000:] if process else "tool unavailable")})
        return GateResult("lint/type", passed, "configured checks executed", {"commands": evidence})

    def _candidate_files(self, changed_files: Iterable[str] | None) -> list[Path]:
        if changed_files is None:
            return [path for path in self.root.rglob("*") if path.is_file()]
        files = []
        for name in changed_files:
            path = (self.root / name).resolve()
            try:
                path.relative_to(self.root)
            except ValueError:
                continue
            if path.is_file():
                files.append(path)
        return files

    def security(self, changed_files: Iterable[str] | None = None) -> GateResult:
        findings: list[dict[str, str]] = []
        for path in self._candidate_files(changed_files):
            if ".git" in path.parts or ".forge" in path.parts or path.stat().st_size > 2_000_000:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for pattern in self.SECRET_PATTERNS + self.DANGEROUS_PATTERNS:
                if pattern.search(text):
                    findings.append({"file": str(path.relative_to(self.root)), "rule": pattern.pattern})
        return GateResult("security", not findings,
                          "No candidate security findings" if not findings else "Security findings detected",
                          {"findings": findings, "files_scanned": [str(p.relative_to(self.root)) for p in self._candidate_files(changed_files)]})

    def review(self, diff: str = "", changed_files: Iterable[str] | None = None) -> GateResult:
        issues: list[str] = []
        material = diff
        for name in changed_files or ():
            path = self.root / name
            if path.is_file():
                try:
                    material += f"\nFILE {name}\n{path.read_text(encoding='utf-8')}"
                except (OSError, UnicodeDecodeError):
                    issues.append(f"unreadable changed file: {name}")
        if "<<<<<<<" in material or re.search(r"^\+\s*pass\s*$", material, re.M):
            issues.append("conflict marker or incomplete implementation")
        if any(pattern.search(material) for pattern in self.DANGEROUS_PATTERNS[:2]):
            issues.append("dynamic code execution")
        if re.search(r"tests?[/\\].*(?:assert\s+True|pytest\.skip)", material, re.I):
            issues.append("suspicious test weakening")
        if changed_files is not None and not material.strip():
            issues.append("review received no changed material")
        return GateResult("review", not issues, "; ".join(issues), {"issues": issues, "changed_files": list(changed_files or [])})

    def run(self, diff: str = "", changed_files: Iterable[str] | None = None) -> VerificationResult:
        started = time.perf_counter()
        gates = [self.tests(), self.build(), self.lint(), self.security(changed_files), self.review(diff, changed_files)]
        for gate in gates:
            gate.evidence.setdefault("duration_seconds", 0.0)
        gates[-1].evidence["verification_duration_seconds"] = time.perf_counter() - started
        return VerificationResult(all(gate.passed for gate in gates), gates)


class SecurityGate:
    def __init__(self, root="."): self.root = root
    def verify(self): return VerificationPipeline(self.root).security()


class ReviewGate:
    def __init__(self, root="."): self.root = root
    def verify(self, diff=""): return VerificationPipeline(self.root).review(diff)


class AcceptanceGate:
    def verify(self, result): return result
