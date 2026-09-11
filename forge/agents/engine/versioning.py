"""Agent versioning (A81): immutable, append-only spec history.

Every accepted spec change produces a *new* version; old versions are
never edited. The history is bounded, ordered, and carries provenance
(who changed it, when, and why). Rolling back does not mutate history —
it appends a new version whose spec is a copy of an older one.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

MAX_HISTORY = 20
MAX_NOTE = 200


@dataclass(frozen=True)
class VersionRecord:
    """One immutable snapshot of an agent's specification."""

    version: int
    spec_dict: dict
    changed_by: str
    changed_at: float
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "spec": dict(self.spec_dict),
            "changed_by": self.changed_by,
            "changed_at": self.changed_at,
            "note": self.note,
        }


class VersionHistory:
    """Bounded, append-only version history for one agent."""

    def __init__(self, agent_id: str,
                 records: list[VersionRecord] | None = None) -> None:
        self.agent_id = agent_id
        self._records: list[VersionRecord] = list(records or [])

    # -- queries ---------------------------------------------------------

    def __len__(self) -> int:
        return len(self._records)

    def current_version(self) -> int:
        return self._records[-1].version if self._records else 0

    def current(self) -> VersionRecord | None:
        return self._records[-1] if self._records else None

    def get(self, version: int) -> VersionRecord:
        for record in self._records:
            if record.version == version:
                return record
        raise KeyError(
            f"Agent {self.agent_id!r} has no version {version}; "
            f"known versions: "
            f"{[record.version for record in self._records]}")

    def versions(self) -> list[VersionRecord]:
        return list(self._records)

    # -- mutation (operator path only) ------------------------------------

    def append(self, spec_dict: dict, *, changed_by: str, note: str = "",
               version: int | None = None) -> VersionRecord:
        note = (note or "").strip()[:MAX_NOTE]
        changed_by = (changed_by or "operator")[:64]
        number = version if version is not None else self.current_version() + 1
        if number < 1:
            raise ValueError("Version numbers start at 1")
        if any(record.version == number for record in self._records):
            raise ValueError(
                f"Version {number} already exists; history is append-only")
        record = VersionRecord(
            version=number,
            spec_dict=dict(spec_dict),
            changed_by=changed_by,
            changed_at=time.time(),
            note=note)
        self._records.append(record)
        # Bounded: keep the newest MAX_HISTORY records. Older versions
        # fall out of the window but current_version never decreases.
        if len(self._records) > MAX_HISTORY:
            self._records = self._records[-MAX_HISTORY:]
        return record

    def snapshot(self) -> list[dict[str, Any]]:
        return [record.to_dict() for record in self._records]

    @classmethod
    def from_snapshot(cls, agent_id: str,
                      records: list[dict[str, Any]]) -> "VersionHistory":
        history = cls(agent_id)
        for entry in records or []:
            history._records.append(VersionRecord(
                version=int(entry["version"]),
                spec_dict=dict(entry.get("spec", {})),
                changed_by=str(entry.get("changed_by", "")),
                changed_at=float(entry.get("changed_at", 0.0)),
                note=str(entry.get("note", ""))))
        return history
