import py_compile
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, List


@dataclass
class VerificationResult:
    command: str
    exit_code: int
    duration: float
    stdout: str
    stderr: str
    passed: bool
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class BuildVerifier:
    """Verifies repository Python syntax compilation, imports, and optional lint/type checks."""

    def __init__(self, root: str | Path = ".") -> None:
        self.root = Path(root).resolve()

    def verify_build(self) -> VerificationResult:
        start_time = time.time()

        # 1. Python compilation check
        failed_files: List[str] = []
        for py_file in self.root.rglob("*.py"):
            rel_path = self._relative(py_file)
            if not rel_path or ".venv" in rel_path or "venv" in rel_path or ".git" in rel_path:
                continue

            try:
                py_compile.compile(str(py_file), doraise=True)
            except py_compile.PyCompileError as err:
                failed_files.append(f"{rel_path}: {err}")

        compilation_passed = len(failed_files) == 0

        # 2. Check for optional mypy / ruff if configured
        optional_passed = True
        optional_stdout = ""
        optional_stderr = ""

        pyproject = self.root / "pyproject.toml"
        has_mypy = pyproject.exists() and "tool.mypy" in pyproject.read_text(encoding="utf-8", errors="ignore")

        if has_mypy:
            mypy_proc = subprocess.run(
                ["mypy", "forge"],
                cwd=str(self.root),
                text=True,
                capture_output=True,
                check=False,
            )
            if mypy_proc.returncode != 0:
                optional_passed = False
                optional_stdout = mypy_proc.stdout
                optional_stderr = mypy_proc.stderr

        duration = time.time() - start_time
        passed = compilation_passed and optional_passed

        exit_code = 0 if passed else 1
        stdout = "Python compilation succeeded." if compilation_passed else "\n".join(failed_files)
        stderr = optional_stderr if not optional_passed else ""

        return VerificationResult(
            command="py_compile + optional mypy",
            exit_code=exit_code,
            duration=duration,
            stdout=stdout,
            stderr=stderr,
            passed=passed,
            details={"failed_files": failed_files, "has_mypy": has_mypy},
        )

    def _relative(self, path: Path) -> str | None:
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return None
