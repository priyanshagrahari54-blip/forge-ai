"""Structured agent communication (A81).

Agents never read each other's working state: they communicate through
validated :class:`AgentMessage` records on the :class:`MessageBus`.

Guarantees:

* **typed** — only whitelisted message types exist;
* **bounded** — content and evidence are capped, confidence is clamped
  to ``[0, 1]``;
* **addressed** — every message names a sender, a receiver, and the
  task it belongs to; sending to an unregistered receiver is an error,
  not a silent drop;
* **auditable** — the bus keeps the full ordered log (persisted by the
  execution state store) and each receiver has its own inbox, drained in
  order.

Dependency hand-offs are messages of type ``task_result`` carrying the
validated structured result; dependent tasks receive prior results as
this structured context, never uncontrolled free text.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

MESSAGE_TYPES = frozenset({
    "task_request", "task_result", "handoff", "query", "ack",
    "error", "status",
})
MAX_CONTENT = 4_000
MAX_EVIDENCE = 50
MAX_EVIDENCE_ITEM = 500


@dataclass(frozen=True)
class AgentMessage:
    """One validated inter-agent message."""

    sender: str
    receiver: str
    task_id: str
    message_type: str
    content: str = ""
    evidence: tuple[str, ...] = ()
    confidence: float = 0.0
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sender": self.sender,
            "receiver": self.receiver,
            "task_id": self.task_id,
            "message_type": self.message_type,
            "content": self.content[:MAX_CONTENT],
            "evidence": list(self.evidence)[:MAX_EVIDENCE],
            "confidence": self.confidence,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentMessage":
        return cls(
            sender=data["sender"], receiver=data["receiver"],
            task_id=data["task_id"],
            message_type=data["message_type"],
            content=data.get("content", ""),
            evidence=tuple(data.get("evidence", ())),
            confidence=data.get("confidence", 0.0),
            timestamp=data.get("timestamp", 0.0),
        )


def validate_message_fields(sender: str, receiver: str, task_id: str,
                            message_type: str, content: str,
                            evidence: tuple[str, ...],
                            confidence: float) -> None:
    """Raise ValueError on any field that violates the message contract."""
    if not sender or not str(sender).strip():
        raise ValueError("Message sender must be non-empty")
    if not receiver or not str(receiver).strip():
        raise ValueError("Message receiver must be non-empty")
    if message_type not in MESSAGE_TYPES:
        raise ValueError(
            f"Unknown message type {message_type!r}; allowed: "
            f"{sorted(MESSAGE_TYPES)}")
    if not isinstance(content, str):
        raise ValueError("Message content must be a string")
    if len(content) > MAX_CONTENT:
        raise ValueError(
            f"Message content exceeds {MAX_CONTENT} characters")
    for item in evidence:
        if not isinstance(item, str):
            raise ValueError("Evidence items must be strings")
        if len(item) > MAX_EVIDENCE_ITEM:
            raise ValueError(
                f"Evidence item exceeds {MAX_EVIDENCE_ITEM} characters")
    if len(evidence) > MAX_EVIDENCE:
        raise ValueError(f"At most {MAX_EVIDENCE} evidence items")
    if not isinstance(confidence, (int, float)) or \
            isinstance(confidence, bool):
        raise ValueError("Confidence must be a number")
    if not 0.0 <= float(confidence) <= 1.0:
        raise ValueError("Confidence must be within [0, 1]")


class MessageBus:
    """Validated point-to-point message delivery with a full log."""

    def __init__(self, roles: set[str] | frozenset[str] | None = None) -> None:
        self.roles = set(roles) if roles is not None else None
        self._inboxes: dict[str, list[AgentMessage]] = {}
        self._log: list[AgentMessage] = []
        self._seq = 0

    def register(self, role: str) -> None:
        if self.roles is not None and role not in self.roles:
            raise ValueError(f"Unknown role: {role!r}")
        self._inboxes.setdefault(role, [])

    def send(self, sender: str, receiver: str, task_id: str,
             message_type: str, content: str = "",
             evidence: tuple[str, ...] = (), confidence: float = 0.0
             ) -> AgentMessage:
        validate_message_fields(sender, receiver, task_id, message_type,
                                content, evidence, confidence)
        if self.roles is not None and receiver not in self.roles:
            raise ValueError(f"Unknown message receiver: {receiver!r}")
        if sender == receiver:
            raise ValueError("A message must be addressed to another role")
        self._inboxes.setdefault(receiver, [])
        message = AgentMessage(
            sender=sender, receiver=receiver, task_id=task_id,
            message_type=message_type, content=content,
            evidence=tuple(evidence[:MAX_EVIDENCE]),
            confidence=float(confidence), timestamp=time.time())
        self._inboxes[receiver].append(message)
        self._log.append(message)
        self._seq += 1
        return message

    def inbox(self, role: str) -> list[AgentMessage]:
        """Drain and return the role's inbox, in delivery order."""
        return self._inboxes.pop(role, [])

    def peek(self, role: str) -> list[AgentMessage]:
        return list(self._inboxes.get(role, []))

    def for_task(self, task_id: str) -> list[AgentMessage]:
        return [message for message in self._log if message.task_id == task_id]

    def log(self) -> list[AgentMessage]:
        return list(self._log)

    def to_list(self) -> list[dict[str, Any]]:
        return [message.to_dict() for message in self._log]
