"""Data models and stage-prompt assembly for staged builds (A82)."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


class StageStatus(str, Enum):
    """Lifecycle of one build stage.

    ``COMPLETED`` is only ever written by the service after a linked run
    passes strict verification (SUCCEEDED + acceptance accepted + zero
    failed gates). Nothing else in the codebase may write it.
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


TERMINAL_STAGE_STATUSES = frozenset(
    {StageStatus.COMPLETED, StageStatus.FAILED}
)

#: Hard bounds (mirrored in the API schemas; the store trusts the service).
MAX_NAME_CHARS = 120
MAX_DESCRIPTION_CHARS = 2000
MAX_DOC_CHARS = 20000  # roadmap / blueprint each
MAX_STAGE_TITLE_CHARS = 160
MAX_STAGE_PROMPT_CHARS = 4000
MAX_STAGES_PER_BUILD = 50
MAX_BUILDS_PER_PROJECT = 50

#: Must stay within the control plane's requirement limit
#: (``ControlPlane.submit_task`` rejects longer requirements).
MAX_REQUIREMENT_CHARS = 8000


@dataclass
class BuildProject:
    """One project section: roadmap + blueprint + ordered stages."""

    id: str
    project_id: str  # control-plane project (execution workspace)
    name: str
    description: str = ""
    roadmap: str = ""
    blueprint: str = ""
    created_by: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self, *, include_docs: bool = False) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "build_id": self.id,
            "project_id": self.project_id,
            "name": self.name,
            "description": self.description,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "roadmap_chars": len(self.roadmap),
            "blueprint_chars": len(self.blueprint),
        }
        if include_docs:
            payload["roadmap"] = self.roadmap
            payload["blueprint"] = self.blueprint
        return payload


@dataclass
class BuildStage:
    """One ordered step of a build project."""

    id: str
    build_id: str
    project_id: str
    position: int  # 1-based, contiguous per build
    title: str
    prompt: str
    status: StageStatus = StageStatus.PENDING
    run_id: str = ""  # latest linked control-plane run
    attempts: int = 0
    runs: List[str] = field(default_factory=list)  # every linked run
    evidence: Dict[str, Any] = field(default_factory=dict)
    docs_snapshot: Dict[str, Any] = field(default_factory=dict)
    error: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self, *, include_prompt: bool = True) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "stage_id": self.id,
            "build_id": self.build_id,
            "position": self.position,
            "title": self.title,
            "status": self.status.value,
            "run_id": self.run_id,
            "attempts": self.attempts,
            "runs": list(self.runs),
            "evidence": dict(self.evidence),
            "docs_snapshot": dict(self.docs_snapshot),
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "prompt_chars": len(self.prompt),
        }
        if include_prompt:
            payload["prompt"] = self.prompt
        return payload


def _truncate(text: str, budget: int) -> Tuple[str, bool]:
    """Fit ``text`` into ``budget`` chars, marking honest truncation."""
    if budget < 0:
        budget = 0
    if len(text) <= budget:
        return text, False
    marker = "\n[... truncated %d chars ...]\n" % (len(text) - budget)
    # Keep head and tail so contracts at the end survive truncation.
    keep = max(0, budget - len(marker))
    head = (keep * 2) // 3
    tail = keep - head
    if tail > 0:
        clipped = text[:head] + marker + text[len(text) - tail:]
    else:
        clipped = (text[:keep] + marker) if keep else marker.strip()
    return clipped[:budget + len(marker)], True


def assemble_stage_requirement(
    build: BuildProject,
    stages: List[BuildStage],
    position: int,
) -> Tuple[str, Dict[str, Any]]:
    """Build the *one at a time* requirement for ``position``.

    The requirement always carries the roadmap + blueprint context plus
    exactly one stage prompt (the current stage) and short summaries of
    already-verified earlier stages. Later stages are never included, so
    the model cannot race ahead.

    Returns ``(requirement, snapshot)`` where the snapshot records exactly
    what was included (char counts, truncation flags, sha256) for evidence.
    The requirement is guaranteed to fit ``MAX_REQUIREMENT_CHARS``.
    """
    ordered = sorted(stages, key=lambda item: item.position)
    current: Optional[BuildStage] = None
    for stage in ordered:
        if stage.position == position:
            current = stage
            break
    if current is None:
        raise ValueError("Unknown stage position: %r" % (position,))

    summaries: List[str] = []
    for stage in ordered:
        if stage.position >= position:
            break
        summary = str(stage.evidence.get("summary", "") or "")
        if not summary:
            summary = "Stage %d %r: verified complete." % (
                stage.position, stage.title)
        summaries.append("- " + summary[:300])
    if summaries:
        previous = "\n".join(summaries)
    else:
        previous = "None -- this is the first stage."

    header = (
        "STAGED BUILD -- stage %d of %d: %s\n"
        "\n"
        "You are executing ONE stage of a multi-stage build. "
        "Implement ONLY this stage; do not build later stages.\n"
        % (position, len(ordered), current.title)
    )
    roadmap_head = "\n=== ROADMAP (full project plan -- context only, do not build ahead) ===\n"
    blueprint_head = "\n=== BLUEPRINT (architecture + contracts -- must follow) ===\n"
    stage_head = "\n=== CURRENT STAGE %d: %s ===\n" % (position, current.title)
    prev_head = "\n=== COMPLETED STAGES (verified -- build on this, do not redo) ===\n"
    footer = (
        "\n=== RULES ===\n"
        "- Real implementation only: write production code and the tests "
        "proving this stage works.\n"
        "- Never claim completion without passing tests and a clean review.\n"
    )

    # Budget: stage prompt always complete, previous summaries bounded,
    # roadmap/blueprint share whatever remains.
    prompt = current.prompt.strip()
    fixed = header + roadmap_head + blueprint_head + stage_head + prev_head + footer
    prev_budget = min(len(previous), 1200)
    remaining = MAX_REQUIREMENT_CHARS - len(fixed) - len(prompt) - prev_budget
    if remaining < 0:
        # Extremely long prompt: footer/prev shrink first; prompt is sacred
        # up to its own schema cap (4000) which always fits.
        prev_budget = max(0, prev_budget + remaining)
        remaining = 0
        previous = previous[:prev_budget]
    else:
        previous = previous[:prev_budget]
    doc_budget = remaining // 2
    roadmap_part, roadmap_cut = _truncate(build.roadmap.strip(), doc_budget)
    blueprint_part, blueprint_cut = _truncate(
        build.blueprint.strip(), remaining - doc_budget)

    requirement = (
        header + roadmap_head + (roadmap_part or "(no roadmap provided)")
        + blueprint_head + (blueprint_part or "(no blueprint provided)")
        + stage_head + prompt + prev_head + previous + footer
    )
    # Absolute guarantee (defensive; budgets above already ensure it).
    if len(requirement) > MAX_REQUIREMENT_CHARS:
        requirement = requirement[:MAX_REQUIREMENT_CHARS]

    snapshot = {
        "position": position,
        "stages_total": len(ordered),
        "roadmap_chars": len(build.roadmap),
        "roadmap_included_chars": len(roadmap_part),
        "roadmap_truncated": roadmap_cut,
        "blueprint_chars": len(build.blueprint),
        "blueprint_included_chars": len(blueprint_part),
        "blueprint_truncated": blueprint_cut,
        "previous_stages": len(summaries),
        "requirement_chars": len(requirement),
        "requirement_sha256": hashlib.sha256(
            requirement.encode("utf-8")).hexdigest(),
    }
    return requirement, snapshot
