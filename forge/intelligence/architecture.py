from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from forge.intelligence.gitignore import GitIgnoreMatcher


@dataclass
class ArchitectureNode:
    """A high-level architectural component."""

    path: str
    kind: str
    files: list[str] = field(default_factory=list)


@dataclass
class ArchitectureReport:
    """Machine-readable repository architecture summary."""

    packages: list[ArchitectureNode] = field(default_factory=list)
    entry_points: list[str] = field(default_factory=list)
    config_files: list[str] = field(default_factory=list)
    test_files: list[str] = field(default_factory=list)
    source_files: list[str] = field(default_factory=list)

    def package_for_file(self, file: str) -> str | None:
        """Return the package containing a file, if known."""

        normalized = file.replace("\\", "/")

        matches = [
            package.path
            for package in self.packages
            if normalized == package.path
            or normalized.startswith(package.path.rstrip("/") + "/")
        ]

        if not matches:
            return None

        return max(matches, key=len)


class ArchitectureAnalyzer:
    """Detect high-level project architecture."""

    CONFIG_NAMES = {
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "tox.ini",
        "pytest.ini",
        "mypy.ini",
        "ruff.toml",
        "package.json",
        "tsconfig.json",
        "vite.config.js",
        "vite.config.ts",
        "webpack.config.js",
        "webpack.config.ts",
        "docker-compose.yml",
        "docker-compose.yaml",
        "Dockerfile",
        ".env.example",
    }

    ENTRY_POINT_NAMES = {
        "__main__.py",
        "main.py",
        "app.py",
        "server.py",
        "cli.py",
        "manage.py",
    }

    TEST_DIRECTORY_NAMES = {
        "test",
        "tests",
    }

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.gitignore = GitIgnoreMatcher(self.root)

    def analyze(
        self,
        all_files: set[str] | list[str] | None = None,
        source_files: list[str] | None = None,
        test_files: list[str] | None = None,
        config_files: list[str] | None = None,
    ) -> ArchitectureReport:
        """Analyze repository architecture, accepting pre-discovered file lists.

        Performance optimization (Bolt ⚡): Accepting pre-discovered file lists
        avoids redundant `rglob("*")` filesystem traversals when called from `RepositoryIntelligence.build`.
        """
        report = ArchitectureReport()

        if all_files is not None:
            report.config_files = list(config_files) if config_files is not None else []
            report.test_files = list(test_files) if test_files is not None else []
            report.source_files = list(source_files) if source_files is not None else []

            if config_files is None or test_files is None or source_files is None:
                for relative in all_files:
                    name = Path(relative).name
                    parts = Path(relative).parts

                    if config_files is None and name in self.CONFIG_NAMES:
                        report.config_files.append(relative)

                    if test_files is None and self._is_test_file(Path(relative), parts):
                        report.test_files.append(relative)

                    if source_files is None and relative.endswith(".py"):
                        report.source_files.append(relative)
        else:
            self._scan_files(report)

        self._detect_packages(report)
        self._detect_entry_points(report)

        return report

    def _scan_files(self, report: ArchitectureReport) -> None:
        for path in self.root.rglob("*"):
            if not path.is_file():
                continue

            relative = self._relative(path)

            if relative is None or self.gitignore.is_ignored(relative):
                continue

            name = path.name
            parts = Path(relative).parts

            if name in self.CONFIG_NAMES:
                report.config_files.append(relative)

            if self._is_test_file(path, parts):
                report.test_files.append(relative)

            if path.suffix == ".py":
                report.source_files.append(relative)

    def _detect_packages(self, report: ArchitectureReport) -> None:
        directories: set[str] = set()

        for source in report.source_files:
            path = Path(source)

            for parent in path.parents:
                parent_string = parent.as_posix()

                if parent_string == ".":
                    continue

                init_file = self.root / parent / "__init__.py"

                if init_file.is_file():
                    directories.add(parent_string)

        for directory in sorted(directories):
            files = [
                source
                for source in report.source_files
                if source == directory
                or source.startswith(directory.rstrip("/") + "/")
            ]

            report.packages.append(
                ArchitectureNode(
                    path=directory,
                    kind="python_package",
                    files=sorted(files),
                )
            )

    def _detect_entry_points(self, report: ArchitectureReport) -> None:
        for source in report.source_files:
            name = Path(source).name

            if name in self.ENTRY_POINT_NAMES:
                report.entry_points.append(source)

        report.entry_points.sort()

    def _is_test_file(
        self,
        path: Path,
        parts: tuple[str, ...],
    ) -> bool:
        if any(part.lower() in self.TEST_DIRECTORY_NAMES for part in parts):
            return True

        name = path.name.lower()

        return (
            name.startswith("test_")
            or name.endswith("_test.py")
        )

    def _relative(self, path: Path) -> str | None:
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return None


def generate_architecture_report(
    architecture: ArchitectureReport,
) -> str:
    """Generate a human-readable architecture report."""

    lines: list[str] = [
        "# Repository Architecture",
        "",
        "## Packages",
    ]

    if architecture.packages:
        for package in architecture.packages:
            lines.append(f"- `{package.path}` ({package.kind})")
    else:
        lines.append("- None detected")

    lines.extend(["", "## Entry Points"])

    if architecture.entry_points:
        lines.extend(
            f"- `{entry}`"
            for entry in architecture.entry_points
        )
    else:
        lines.append("- None detected")

    lines.extend(["", "## Configuration"])

    if architecture.config_files:
        lines.extend(
            f"- `{config}`"
            for config in architecture.config_files
        )
    else:
        lines.append("- None detected")

    lines.extend(["", "## Tests"])

    if architecture.test_files:
        lines.extend(
            f"- `{test}`"
            for test in architecture.test_files
        )
    else:
        lines.append("- None detected")

    lines.extend(
        [
            "",
            "## Source Files",
            f"- {len(architecture.source_files)} Python source files",
        ]
    )

    return "\n".join(lines)
