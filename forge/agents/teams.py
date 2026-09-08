"""Agent teams (A52): ordered execution of runtime-defined agents.

Honesty rules:

* Teams contain real, bound agent definitions only; the team stores
  names, and execution resolves members at run time through the same
  runner + gates as direct agent runs.
* Execution is strictly sequential: each member's output summary
  (bounded) is passed to the next member as context. Nothing is
  claimed about parallelism or coordination beyond that handoff.
* Team results carry each member's real result — no member's
  failure is ever hidden.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

MAX_MEMBERS = 6
MAX_TEAMS = 12
MAX_NAME = 48


@dataclass
class AgentTeam:
    team_id: str
    name: str
    members: tuple[str, ...]
    created_by: str = ""
    created_at: float = field(default_factory=time.time)
    status: str = "draft"

    def to_dict(self) -> dict[str, Any]:
        return {"team_id": self.team_id, "name": self.name,
                "members": list(self.members), "created_by":
                self.created_by, "created_at": self.created_at,
                "status": self.status}


class TeamRegistry:
    """Session-bounded validated team definitions."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self._teams: dict[str, AgentTeam] = {}

    def create(self, name: str, members: tuple[str, ...] | list[str],
               *, agent_lookup, created_by: str = "") -> AgentTeam:
        name = (name or "").strip()[:MAX_NAME]
        members = tuple(members)
        if not name:
            raise ValueError("team name must be non-empty")
        if not members:
            raise ValueError("a team needs at least one member")
        if len(members) > MAX_MEMBERS:
            raise ValueError(f"too many members (max {MAX_MEMBERS})")
        seen: list[str] = []
        for member in members:
            definition = agent_lookup(member)
            if definition is None:
                raise ValueError(f"unknown agent: {member}")
            if not getattr(definition, "real", False):
                raise ValueError(
                    f"agent {member!r} has no bound executor; teams "
                    "run real agents only")
            if member in seen:
                raise ValueError(f"duplicate member: {member}")
            seen.append(member)
        if len(self._teams) >= MAX_TEAMS:
            raise ValueError(f"team limit reached ({MAX_TEAMS})")
        team = AgentTeam(team_id=uuid4().hex[:12], name=name,
                         members=tuple(seen), created_by=created_by)
        self._teams[team.team_id] = team
        return team

    def get(self, team_id: str) -> AgentTeam | None:
        return self._teams.get(team_id)

    def list(self) -> list[AgentTeam]:
        return [self._teams[team_id] for team_id in
                sorted(self._teams)]
