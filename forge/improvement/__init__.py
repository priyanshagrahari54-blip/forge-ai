"""Continuous system improvement (A84 Stage O).

Forge observes failed tasks, routing quality, tool errors, prompt
effectiveness, research quality, provider reliability and user corrections,
then generates *proposals*. A proposal is evidence + an action class + an
owner; it is never an applied change. Actual self-modification continues to
run through the existing governed loops (A58 agent self-development, A26-A30
engine loop), whose authority gates — tests, security review, authorization,
rollback — the proposal layer explicitly defers to and can never bypass.
"""
from forge.improvement.proposals import (
    ACTION_CLASSES,
    ImprovementEngine,
    Proposal,
)

__all__ = ["ACTION_CLASSES", "ImprovementEngine", "Proposal"]
