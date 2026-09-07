"""Telemetry for the Model Fabric.

Telemetry is observational only. It records routing decisions, provider call
outcomes, and feedback *without* persisting prompt/context/response content or
any credential material, so it can be written to disk safely.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class TelemetryEvent:
    kind: str
    timestamp: float
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "timestamp": self.timestamp, **self.payload}


class Telemetry:
    """In-memory event buffer with an optional NDJSON file sink.

    By default telemetry is memory-only; callers that want persistence pass a
    ``sink_path`` and call :meth:`flush`. Flushing appends one JSON object per
    line, which is safe to rotate/truncate and never rewrites history.
    """

    def __init__(self, enabled: bool = True, sink_path: str | Path | None = None) -> None:
        self.enabled = enabled
        self.sink_path = Path(sink_path) if sink_path else None
        self._events: list[TelemetryEvent] = []

    def record(self, kind: str, **payload: Any) -> None:
        if not self.enabled:
            return
        self._events.append(TelemetryEvent(kind=kind, timestamp=time.time(), payload=dict(payload)))

    def events(self, kind: str | None = None) -> list[dict[str, Any]]:
        events = self._events if kind is None else [e for e in self._events if e.kind == kind]
        return [e.to_dict() for e in events]

    def count(self, kind: str | None = None) -> int:
        if kind is None:
            return len(self._events)
        return sum(1 for e in self._events if e.kind == kind)

    def clear(self) -> None:
        self._events.clear()

    def flush(self) -> int:
        """Append buffered events to the sink file, if configured.

        Returns the number of events written. Without a sink this is a no-op.
        """
        if not self.sink_path or not self._events:
            return 0
        self.sink_path.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(event.to_dict(), sort_keys=True) for event in self._events]
        with self.sink_path.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        count = len(self._events)
        self.clear()
        return count

    def __len__(self) -> int:
        return len(self._events)
