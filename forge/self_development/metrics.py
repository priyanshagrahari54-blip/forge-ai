from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class RepoMetrics:
    total_files: int = 0
    total_lines: int = 0
    python_files: int = 0
    symbol_count: int = 0
    dependency_count: int = 0
    package_count: int = 0
    test_file_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TestMetrics:
    total_tests: int = 0
    passed_tests: int = 0
    failed_tests: int = 0
    duration: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SecurityMetrics:
    permission_rules_count: int = 0
    blocked_operations: int = 0
    approval_required_operations: int = 0
    potential_vulnerabilities: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PerformanceMetrics:
    avg_execution_time: float = 0.0
    memory_usage_mb: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
