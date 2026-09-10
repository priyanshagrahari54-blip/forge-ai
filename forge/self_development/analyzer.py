from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from forge.intelligence.repository import RepositoryIntelligence
from forge.self_development.findings import (
    Finding,
    FindingCategory,
    FindingSeverity,
)
from forge.self_development.metrics import (
    PerformanceMetrics,
    RepoMetrics,
    SecurityMetrics,
    TestMetrics,
)


class ForgeSelfAnalyzer:
    """Analyzes Forge repository and produces structured findings and metrics."""

    def __init__(self, root: str | Path = ".") -> None:
        self.root = Path(root).resolve()
        self.output_dir = self.root / ".forge" / "self"
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def analyze(self) -> dict[str, Any]:
        repo_intel = RepositoryIntelligence.build(self.root)

        findings: list[Finding] = []
        finding_counter = 1

        # 1. TODO / FIXME scanning
        todo_findings, finding_counter = self._scan_todos(repo_intel, finding_counter)
        findings.extend(todo_findings)

        # 2. Incomplete integrations scanning
        inc_findings, finding_counter = self._scan_incomplete_integrations(
            repo_intel, finding_counter
        )
        findings.extend(inc_findings)

        # 3. Architecture & Dependency cycles scanning
        arch_findings, finding_counter = self._scan_architecture(
            repo_intel, finding_counter
        )
        findings.extend(arch_findings)

        # 4. Test health & missing tests
        test_findings, finding_counter = self._scan_test_health(
            repo_intel, finding_counter
        )
        findings.extend(test_findings)

        # 5. Security audit
        sec_findings, finding_counter = self._scan_security(
            repo_intel, finding_counter
        )
        findings.extend(sec_findings)

        # Collect metrics
        repo_metrics = self._collect_repo_metrics(repo_intel)
        total_test_files = len(repo_intel.tests.test_to_sources)

        # Test pass/fail is measured during evaluation (BenchmarkRunner actually
        # runs pytest), never invented here. Analysis reports only what is
        # statically knowable: the number of mapped test files.
        test_metrics = TestMetrics(
            total_tests=total_test_files,
            passed_tests=0,
            failed_tests=0,
            duration=0.0,
        )

        # Permission counts are read from the live permission table so they can
        # never drift from the enforced policy.
        permission_rules, blocked_operations, approval_required_operations = self._permission_metrics()
        security_metrics = SecurityMetrics(
            permission_rules_count=permission_rules,
            blocked_operations=blocked_operations,
            approval_required_operations=approval_required_operations,
            potential_vulnerabilities=len(sec_findings),
        )

        # Not measured during static analysis; recorded as zero (unmeasured).
        performance_metrics = PerformanceMetrics(
            avg_execution_time=0.0,
            memory_usage_mb=0.0,
        )

        analysis_data = {
            "root": str(self.root),
            "findings": [f.to_dict() for f in findings],
            "repo_metrics": repo_metrics.to_dict(),
            "test_metrics": test_metrics.to_dict(),
            "security_metrics": security_metrics.to_dict(),
            "performance_metrics": performance_metrics.to_dict(),
            "summary": repo_intel.summary(),
        }

        # Persist to .forge/self/analysis.json
        analysis_file = self.output_dir / "analysis.json"
        analysis_file.write_text(json.dumps(analysis_data, indent=2), encoding="utf-8")

        return analysis_data

    def _scan_todos(
        self, repo_intel: RepositoryIntelligence, start_id: int
    ) -> tuple[list[Finding], int]:
        findings: list[Finding] = []
        counter = start_id

        todo_pattern = re.compile(r"#\s*(TODO|FIXME|HACK|XXX)\b:(.*)", re.IGNORECASE)

        for source_file in repo_intel.architecture.source_files:
            file_path = self.root / source_file
            if not file_path.is_file():
                continue

            try:
                lines = file_path.read_text(encoding="utf-8").splitlines()
            except Exception:
                continue

            for idx, line in enumerate(lines, 1):
                match = todo_pattern.search(line)
                if match:
                    tag = match.group(1).upper()
                    comment = match.group(2).strip()
                    fid = f"FINDING-{counter:03d}"
                    counter += 1

                    severity = (
                        FindingSeverity.MEDIUM.value
                        if tag in ("FIXME", "XXX")
                        else FindingSeverity.LOW.value
                    )

                    findings.append(
                        Finding(
                            id=fid,
                            category=FindingCategory.TODO_FIXME.value,
                            severity=severity,
                            evidence=f"{source_file}:{idx}: {line.strip()}",
                            affected_files=[source_file],
                            affected_symbols=[],
                            description=f"Found {tag} item: {comment or line.strip()}",
                            proposed_improvement=f"Resolve {tag} in {source_file}",
                            estimated_complexity="low",
                            estimated_risk="low",
                        )
                    )

        return findings, counter

    def _scan_incomplete_integrations(
        self, repo_intel: RepositoryIntelligence, start_id: int
    ) -> tuple[list[Finding], int]:
        findings: list[Finding] = []
        counter = start_id

        for source_file in repo_intel.architecture.source_files:
            file_path = self.root / source_file
            if not file_path.is_file():
                continue

            try:
                content = file_path.read_text(encoding="utf-8")
            except Exception:
                continue

            if "raise NotImplementedError" in content:
                fid = f"FINDING-{counter:03d}"
                counter += 1
                findings.append(
                    Finding(
                        id=fid,
                        category=FindingCategory.INCOMPLETE_INTEGRATION.value,
                        severity=FindingSeverity.MEDIUM.value,
                        evidence=f"NotImplementedError found in {source_file}",
                        affected_files=[source_file],
                        affected_symbols=[],
                        description=f"Incomplete implementation with NotImplementedError in {source_file}",
                        proposed_improvement=f"Implement missing functionality in {source_file}",
                        estimated_complexity="medium",
                        estimated_risk="low",
                    )
                )

        return findings, counter

    def _scan_architecture(
        self, repo_intel: RepositoryIntelligence, start_id: int
    ) -> tuple[list[Finding], int]:
        findings: list[Finding] = []
        counter = start_id

        cycles = repo_intel.dependency_analysis.cycles()
        for cycle in cycles:
            fid = f"FINDING-{counter:03d}"
            counter += 1
            findings.append(
                Finding(
                    id=fid,
                    category=FindingCategory.ARCHITECTURE.value,
                    severity=FindingSeverity.HIGH.value,
                    evidence=f"Dependency cycle detected: {' -> '.join(cycle)}",
                    affected_files=cycle,
                    affected_symbols=[],
                    description=f"Circular dependency among modules: {cycle}",
                    proposed_improvement="Refactor dependencies to eliminate circular import cycle",
                    estimated_complexity="high",
                    estimated_risk="medium",
                )
            )

        return findings, counter

    def _scan_test_health(
        self, repo_intel: RepositoryIntelligence, start_id: int
    ) -> tuple[list[Finding], int]:
        findings: list[Finding] = []
        counter = start_id

        for source_file in repo_intel.architecture.source_files:
            if source_file.endswith("__init__.py"):
                continue

            tests = repo_intel.affected_tests(source_file)
            if not tests:
                fid = f"FINDING-{counter:03d}"
                counter += 1
                findings.append(
                    Finding(
                        id=fid,
                        category=FindingCategory.TEST_HEALTH.value,
                        severity=FindingSeverity.LOW.value,
                        evidence=f"No mapped test files found for {source_file}",
                        affected_files=[source_file],
                        affected_symbols=[],
                        description=f"Source file {source_file} lacks direct test mapping",
                        proposed_improvement=f"Add unit tests for {source_file}",
                        estimated_complexity="medium",
                        estimated_risk="low",
                    )
                )

        return findings, counter

    def _scan_security(
        self, repo_intel: RepositoryIntelligence, start_id: int
    ) -> tuple[list[Finding], int]:
        findings: list[Finding] = []
        counter = start_id

        secret_pattern = re.compile(
            r'(api_key|secret|password|private_key)\s*=\s*["\'][A-Za-z0-9_\-]{8,}["\']',
            re.IGNORECASE,
        )

        for source_file in repo_intel.architecture.source_files:
            file_path = self.root / source_file
            if not file_path.is_file():
                continue

            try:
                lines = file_path.read_text(encoding="utf-8").splitlines()
            except Exception:
                continue

            for idx, line in enumerate(lines, 1):
                if secret_pattern.search(line):
                    fid = f"FINDING-{counter:03d}"
                    counter += 1
                    findings.append(
                        Finding(
                            id=fid,
                            category=FindingCategory.SECURITY.value,
                            severity=FindingSeverity.CRITICAL.value,
                            evidence=f"{source_file}:{idx}: hardcoded credentials",
                            affected_files=[source_file],
                            affected_symbols=[],
                            description=f"Potential hardcoded secret or credential in {source_file}",
                            proposed_improvement="Remove hardcoded credentials and load from environment variables",
                            estimated_complexity="low",
                            estimated_risk="low",
                        )
                    )

        return findings, counter

    def _permission_metrics(self) -> tuple[int, int, int]:
        """Read the live permission table (never a hardcoded constant)."""
        from forge.security.permissions import PermissionLevel, PermissionManager

        rules = PermissionManager().rules
        blocked = sum(1 for level in rules.values() if level == PermissionLevel.BLOCKED)
        approval = sum(
            1 for level in rules.values() if level == PermissionLevel.APPROVAL_REQUIRED
        )
        return len(rules), blocked, approval

    def _collect_repo_metrics(
        self, repo_intel: RepositoryIntelligence
    ) -> RepoMetrics:
        total_files = len(repo_intel.architecture.source_files) + len(
            repo_intel.architecture.test_files
        )
        total_lines = 0
        for src in (
            repo_intel.architecture.source_files + repo_intel.architecture.test_files
        ):
            p = self.root / src
            if p.is_file():
                try:
                    total_lines += len(p.read_text(encoding="utf-8").splitlines())
                except Exception:
                    pass

        return RepoMetrics(
            total_files=total_files,
            total_lines=total_lines,
            python_files=len(repo_intel.architecture.source_files),
            symbol_count=len(repo_intel.symbols.symbols),
            dependency_count=len(repo_intel.dependencies.dependencies),
            package_count=len(repo_intel.architecture.packages),
            test_file_count=len(repo_intel.architecture.test_files),
        )
