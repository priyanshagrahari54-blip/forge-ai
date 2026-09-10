"""AI Council (A45): multi-model deliberation.

Simulated members by default (explicitly labeled); real models deliberate
through :class:`FabricCouncilMember` when the caller provides a fabric.
"""
from forge.council.engine import (AICouncilEngine, DEFAULT_MEMBERS,
                                  FabricCouncilMember, MemberOpinion,
                                  SimulatedCouncilModel)

__all__ = ["AICouncilEngine", "DEFAULT_MEMBERS", "FabricCouncilMember",
           "MemberOpinion", "SimulatedCouncilModel"]
