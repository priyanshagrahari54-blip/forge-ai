"""Staged builds (A82): multi-project, stage-by-stage verified execution.

Each *build project* is a section with its own roadmap, blueprint, and an
ordered list of stages. The operator supplies every stage prompt up front;
Forge consumes them strictly one at a time -- stage N+1 can never start
until stage N is *verified complete* through the real Supervisor
transaction (code -> tests -> debug -> review -> security -> acceptance ->
commit). There is no API that marks a stage complete by hand.
"""
from __future__ import annotations

from forge.staged.models import (
    BuildProject,
    BuildStage,
    StageStatus,
    assemble_stage_requirement,
)
from forge.staged.service import StagedBuilds
from forge.staged.store import StagedStore

__all__ = [
    "BuildProject",
    "BuildStage",
    "StageStatus",
    "StagedBuilds",
    "StagedStore",
    "assemble_stage_requirement",
]
