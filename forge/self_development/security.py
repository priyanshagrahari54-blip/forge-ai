import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, List

from forge.security.permissions import PermissionManager


@dataclass
class SecurityFinding:
    severity: str
    category: str
    file: str
    line: int
    evidence: str
    description: str


@dataclass
class SecurityResult:
    passed: bool = True
    findings: List[dict] = field(default_factory=list)
    new_findings: List[dict] = field(default_factory=list)
    resolved_findings: List[dict] = field(default_factory=list)
    severity: str = "none"
    files: List[str] = field(default_factory=list)
    evidence: List[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SecurityScanner:
    """Scans repository files for hardcoded secrets, shell execution, path traversal, and permission bypasses."""

    SECRET_PATTERN = re.compile(
        r'(api_key|secret_key|private_key|password|auth_token)\s*=\s*["\'][A-Za-z0-9_\-]{8,}["\']',
        re.IGNORECASE,
    )

    DANGEROUS_SHELL_PATTERNS = [
        (re.compile(r"\bos\.system\("), "os.system usage"),
        (re.compile(r"\beval\("), "eval usage"),
        (re.compile(r"\bexec\("), "exec usage"),
        (re.compile(r"subprocess\.[A-Za-z0-9_]+\([^)]*shell\s*=\s*True"), "subprocess with shell=True"),
    ]

    PATH_TRAVERSAL_PATTERN = re.compile(r'("\.\./\.\./|\'\.\./\.\./)')

    def __init__(self, root: str | Path = ".") -> None:
        self.root = Path(root).resolve()
        self.permission_manager = PermissionManager()

    def scan(self) -> SecurityResult:
        findings: List[SecurityFinding] = []

        for py_file in self.root.rglob("*.py"):
            rel_path = self._relative(py_file)
            if not rel_path or ".venv" in rel_path or "venv" in rel_path or ".git" in rel_path:
                continue

            try:
                lines = py_file.read_text(encoding="utf-8").splitlines()
            except Exception:
                continue

            for idx, line in enumerate(lines, 1):
                # 1. Secret detection
                if self.SECRET_PATTERN.search(line):
                    findings.append(
                        SecurityFinding(
                            severity="critical",
                            category="hardcoded_secret",
                            file=rel_path,
                            line=idx,
                            evidence=line.strip(),
                            description="Hardcoded credential or secret key detected",
                        )
                    )

                # 2. Shell execution detection
                for pat, desc in self.DANGEROUS_SHELL_PATTERNS:
                    if pat.search(line):
                        findings.append(
                            SecurityFinding(
                                severity="high",
                                category="dangerous_execution",
                                file=rel_path,
                                line=idx,
                                evidence=line.strip(),
                                description=f"Dangerous shell execution: {desc}",
                            )
                        )

                # 3. Path traversal detection
                if self.PATH_TRAVERSAL_PATTERN.search(line):
                    findings.append(
                        SecurityFinding(
                            severity="medium",
                            category="path_traversal",
                            file=rel_path,
                            line=idx,
                            evidence=line.strip(),
                            description="Potential path traversal pattern detected",
                        )
                    )

        has_critical_or_high = any(
            f.severity in ("critical", "high") for f in findings
        )
        passed = not has_critical_or_high

        finding_dicts = [asdict(f) for f in findings]
        files = sorted({f.file for f in findings})
        evidences = [f.evidence for f in findings]

        max_sev = "none"
        if any(f.severity == "critical" for f in findings):
            max_sev = "critical"
        elif any(f.severity == "high" for f in findings):
            max_sev = "high"
        elif any(f.severity == "medium" for f in findings):
            max_sev = "medium"

        return SecurityResult(
            passed=passed,
            findings=finding_dicts,
            new_findings=[],
            resolved_findings=[],
            severity=max_sev,
            files=files,
            evidence=evidences,
        )

    def compare(
        self, baseline: SecurityResult, candidate: SecurityResult
    ) -> SecurityResult:
        baseline_set = {
            (f["file"], f["line"], f["category"]) for f in baseline.findings
        }
        candidate_set = {
            (f["file"], f["line"], f["category"]) for f in candidate.findings
        }

        new_dicts = [
            f
            for f in candidate.findings
            if (f["file"], f["line"], f["category"]) not in baseline_set
        ]

        resolved_dicts = [
            f
            for f in baseline.findings
            if (f["file"], f["line"], f["category"]) not in candidate_set
        ]

        passed = candidate.passed and len(new_dicts) == 0

        return SecurityResult(
            passed=passed,
            findings=candidate.findings,
            new_findings=new_dicts,
            resolved_findings=resolved_dicts,
            severity=candidate.severity,
            files=candidate.files,
            evidence=candidate.evidence,
        )

    def _relative(self, path: Path) -> str | None:
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return None
