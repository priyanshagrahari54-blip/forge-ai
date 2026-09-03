from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from forge.intelligence.gitignore import GitIgnoreMatcher


@dataclass
class RuntimeCommand:
    """A repository command detected from project evidence."""

    kind: str
    command: str
    evidence: list[str] = field(default_factory=list)
    confidence: str = "medium"


@dataclass
class RuntimeReport:
    """Detected ways to install, test, run, and build a project."""

    project_type: list[str] = field(default_factory=list)
    commands: list[RuntimeCommand] = field(default_factory=list)

    def commands_for(self, kind: str) -> list[RuntimeCommand]:
        return [
            command
            for command in self.commands
            if command.kind == kind
        ]

    def has_command(self, kind: str, command: str) -> bool:
        return any(
            item.kind == kind and item.command == command
            for item in self.commands
        )


class RuntimeDetector:
    """Detect executable project behavior from repository evidence."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.gitignore = GitIgnoreMatcher(self.root)

    def detect(self) -> RuntimeReport:
        report = RuntimeReport()

        files = self._files()

        self._detect_python(files, report)
        self._detect_node(files, report)
        self._detect_docker(files, report)
        self._detect_generic_tests(files, report)

        report.project_type = sorted(set(report.project_type))

        return report

    def _detect_python(
        self,
        files: set[str],
        report: RuntimeReport,
    ) -> None:
        has_python = any(
            file.endswith(".py")
            for file in files
        )

        if not has_python:
            return

        report.project_type.append("python")

        if "pyproject.toml" in files:
            report.commands.append(
                RuntimeCommand(
                    kind="install",
                    command="python -m pip install -e .",
                    evidence=["pyproject.toml"],
                    confidence="high",
                )
            )

        if "tests" in {
            Path(file).parts[0]
            for file in files
            if Path(file).parts
        }:
            report.commands.append(
                RuntimeCommand(
                    kind="test",
                    command="python -m pytest",
                    evidence=["tests/"],
                    confidence="high",
                )
            )

        for file in sorted(files):
            name = Path(file).name

            if name == "__main__.py":
                module = self._module_name(file)

                if module:
                    report.commands.append(
                        RuntimeCommand(
                            kind="run",
                            command=f"python -m {module}",
                            evidence=[file],
                            confidence="high",
                        )
                    )

            elif name == "cli.py" and file.count("/") == 0:
                report.commands.append(
                    RuntimeCommand(
                        kind="run",
                        command=f"python {file}",
                        evidence=[file],
                        confidence="medium",
                    )
                )

            elif name == "main.py" and file.count("/") == 0:
                report.commands.append(
                    RuntimeCommand(
                        kind="run",
                        command=f"python {file}",
                        evidence=[file],
                        confidence="medium",
                    )
                )

    def _detect_node(
        self,
        files: set[str],
        report: RuntimeReport,
    ) -> None:
        if "package.json" not in files:
            return

        report.project_type.append("node")

        package_json = self.root / "package.json"

        try:
            content = package_json.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return

        scripts = self._extract_npm_scripts(content)

        for kind, script in scripts.items():
            report.commands.append(
                RuntimeCommand(
                    kind=kind,
                    command=f"npm run {script}",
                    evidence=["package.json"],
                    confidence="high",
                )
            )

    def _detect_docker(
        self,
        files: set[str],
        report: RuntimeReport,
    ) -> None:
        if "Dockerfile" in files:
            report.project_type.append("docker")
            report.commands.append(
                RuntimeCommand(
                    kind="build",
                    command="docker build .",
                    evidence=["Dockerfile"],
                    confidence="high",
                )
            )

        compose = (
            "docker-compose.yml" in files
            or "docker-compose.yaml" in files
        )

        if compose:
            report.project_type.append("docker-compose")
            report.commands.append(
                RuntimeCommand(
                    kind="run",
                    command="docker compose up",
                    evidence=[
                        file
                        for file in (
                            "docker-compose.yml",
                            "docker-compose.yaml",
                        )
                        if file in files
                    ],
                    confidence="high",
                )
            )

    def _detect_generic_tests(
        self,
        files: set[str],
        report: RuntimeReport,
    ) -> None:
        if any(
            file.endswith(".py")
            and (
                Path(file).name.startswith("test_")
                or Path(file).name.endswith("_test.py")
            )
            for file in files
        ):
            if not report.has_command("test", "python -m pytest"):
                report.commands.append(
                    RuntimeCommand(
                        kind="test",
                        command="python -m pytest",
                        evidence=["Python test files"],
                        confidence="medium",
                    )
                )

    def _extract_npm_scripts(
        self,
        content: str,
    ) -> dict[str, str]:
        import json

        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            return {}

        scripts = data.get("scripts", {})

        if not isinstance(scripts, dict):
            return {}

        mapping: dict[str, str] = {}

        if "test" in scripts:
            mapping["test"] = "test"

        if "build" in scripts:
            mapping["build"] = "build"

        if "start" in scripts:
            mapping["run"] = "start"

        if "dev" in scripts:
            mapping["dev"] = "dev"

        return mapping

    def _module_name(self, file: str) -> str | None:
        path = Path(file)

        if path.name == "__main__.py":
            path = path.parent

        parts = list(path.parts)

        if not parts:
            return None

        if parts[-1] == ".":
            return None

        return ".".join(parts)

    def _files(self) -> set[str]:
        result: set[str] = set()

        for path in self.root.rglob("*"):
            if not path.is_file():
                continue

            try:
                relative = path.relative_to(self.root).as_posix()
            except ValueError:
                continue

            if self.gitignore.is_ignored(relative):
                continue

            result.add(relative)

        return result


def generate_runtime_report(runtime: RuntimeReport) -> str:
    """Generate a human-readable runtime detection report."""

    lines = [
        "# Runtime Detection",
        "",
        "## Project Types",
    ]

    if runtime.project_type:
        lines.extend(
            f"- `{project_type}`"
            for project_type in runtime.project_type
        )
    else:
        lines.append("- None detected")

    lines.extend(["", "## Commands"])

    if runtime.commands:
        for item in runtime.commands:
            evidence = ", ".join(item.evidence)
            lines.append(
                f"- **{item.kind}**: `{item.command}` "
                f"({item.confidence}; evidence: {evidence})"
            )
    else:
        lines.append("- None detected")

    return "\n".join(lines)
