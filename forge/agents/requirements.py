from __future__ import annotations

import re
from dataclasses import dataclass

#: Word characters for tokenization (splits snake_case, kebab-case, paths).
_TOKEN_RE = re.compile(r"[a-z0-9]+")

#: Inflection tails accepted after a keyword stem (plurals, tense).
_INFLECTIONS = frozenset({"s", "es", "ed", "ing", "d"})


def _match_keyword(keyword: str, tokens: frozenset[str],
                   text: str) -> bool:
    """Match one keyword against tokenized text.

    Single words match whole tokens plus plain English inflections
    (``fix`` matches ``fixed``/``fixing`` but not ``prefix``;
    ``test`` matches ``tests`` but not ``latest`` or ``contest``).
    Multi-word keywords match as phrases in the normalized text.
    """
    if " " in keyword:
        return keyword in text
    if keyword in tokens:
        return True
    for token in tokens:
        if not token.startswith(keyword):
            continue
        rest = token[len(keyword):]
        if rest in _INFLECTIONS:
            return True
        # Doubled consonant: committed -> commit + ed.
        if (len(rest) >= 2 and rest[0] == keyword[-1]
                and rest[1:] in _INFLECTIONS):
            return True
    return False


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
        (
            "research",
            (
                "research",
                "researcher",
                "analyze",
                "analysis",
                "investigate",
                "explore",
                "inventory",
            ),
        ),
        (
            "architecture",
            (
                "architecture",
                "architect",
                "design",
                "structure",
                "layout",
            ),
        ),
        (
            "performance",
            (
                "performance",
                "optimize",
                "optimization",
                "profile",
                "profiling",
                "latency",
                "speed",
            ),
        ),
        (
            "git",
            (
                "git",
                "commit",
                "stage",
                "branch",
                "push",
                "repository status",
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
        "research": "research",
        "architecture": "architecture",
        "performance": "performance",
        "git": "git",
    }

    def extract(self, task_description: str) -> TaskRequirements:
        text = task_description.lower()
        tokens = frozenset(_TOKEN_RE.findall(text))
        capabilities: list[str] = []

        for capability, keywords in self.RULES:
            if any(_match_keyword(keyword, tokens, text)
                   for keyword in keywords):
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
