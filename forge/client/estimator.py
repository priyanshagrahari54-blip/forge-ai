"""Deterministic task estimation for the HYBRID selector (A81).

Pure text classification — no model, no network. The estimator answers
two questions the router needs:

1. **kind** — which bounded vocabulary the task belongs to
   (``inspect`` = light read-only work the G560 can do; ``engineering``
   = real code changes that need a model on the server);
2. **size** — requirement length (chars) as the size proxy.

The same requirement always produces the same estimate (unit-tested).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

KIND_INSPECT = "inspect"
KIND_ENGINEERING = "engineering"

#: Words that mark read-only inspection tasks (light, model-free).
_INSPECT_WORDS = (
    "status", "inspect", "summarize", "summary", "list", "show",
    "report", "count", "stats", "statistics", "health", "check",
    "outline", "inventory", "snapshot", "diff", "log", "logs",
)

#: Words that mark engineering tasks (need a real model => server).
_ENGINEERING_WORDS = (
    "implement", "add", "fix", "refactor", "write", "create", "change",
    "modify", "remove", "delete", "migrate", "optimize", "test",
    "bug", "feature", "endpoint", "function", "class", "patch",
)

_WORD_RE = re.compile(r"[a-z]+")


@dataclass(frozen=True)
class TaskEstimate:
    kind: str            # KIND_INSPECT | KIND_ENGINEERING
    chars: int
    words: int
    needs_model: bool    # True => never local on the G560
    #: Matched signals recorded for the desktop decision log.
    signals: tuple


def estimate(requirement: str) -> TaskEstimate:
    """Classify a requirement deterministically.

    Engineering signals win over inspection signals (fail towards the
    server: an ambiguous task is treated as needing the model).
    """
    text = (requirement or "").strip().lower()
    words = _WORD_RE.findall(text)
    inspect_hits = tuple(sorted({w for w in words
                                 if w in _INSPECT_WORDS}))
    engineering_hits = tuple(sorted({w for w in words
                                     if w in _ENGINEERING_WORDS}))
    if engineering_hits:
        kind = KIND_ENGINEERING
    elif inspect_hits:
        kind = KIND_INSPECT
    else:
        # Unrecognized task: assume it needs the model (server-side).
        kind = KIND_ENGINEERING
    return TaskEstimate(
        kind=kind,
        chars=len(text),
        words=len(words),
        needs_model=(kind == KIND_ENGINEERING),
        signals=engineering_hits + inspect_hits,
    )
