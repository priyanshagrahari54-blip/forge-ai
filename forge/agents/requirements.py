from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TaskRequirements:
    capabilities: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()


class TaskRequirementExtractor:
    """Extract deterministic agent requirements from task descriptions."""

    RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
        (
            "debugging",
            (
                "bug",
                "bugs",
                "debug",
                "debugging",
                "error",
                "exception",
                "failure",
                "failing",
                "crash",
                "broken",
                "fix",
            ),
        ),
        (
            "coding",
            (
                "code",
                "coding",
                "implement",
                "implementation",
                "build",
                "create",
                "add",
                "feature",
                "refactor",
                "refactoring",
            ),
        ),
        (
            "testing",
            (
                "test",
                "tests",
                "testing",
                "pytest",
                "regression",
                "validate",
                "validation",
            ),
        ),
        (
            "review",
            (
                "review",
                "reviewer",
                "reviewing",
                "inspect",
                "inspection",
            ),
        ),
        (
            "security",
            (
                "security",
                "secure",
                "vulnerability",
                "vulnerabilities",
                "exploit",
                "permission",
                "authentication",
                "authorization",
            ),
        ),
        (
            "documentation",
            (
                "documentation",
                "document",
                "docs",
                "readme",
                "comment",
            ),
        ),
    )

    ROLE_MAP: dict[str, str] = {
        "coding": "coding",
        "testing": "testing",
        "debugging": "debugging",
        "review": "reviewing",
        "security": "security",
        "documentation": "documentation",
    }

    def extract(self, task_description: str) -> TaskRequirements:
        text = task_description.lower()
        capabilities: list[str] = []

        for capability, keywords in self.RULES:
            if any(keyword in text for keyword in keywords):
                capabilities.append(capability)

        roles = [
            self.ROLE_MAP[capability]
            for capability in capabilities
            if capability in self.ROLE_MAP
        ]

        return TaskRequirements(
            capabilities=tuple(capabilities),
            roles=tuple(dict.fromkeys(roles)),
        )
