"""Dependency-light structured telemetry recorder."""
from __future__ import annotations
from dataclasses import dataclass, field
import time, uuid
from typing import Any

@dataclass(frozen=True)
class TelemetryEvent:
    name: str
    kind: str = "event"
    trace_id: str = ""
    timestamp: float = 0.0
    attributes: dict[str, Any] = field(default_factory=dict)

    def materialized(self) -> "TelemetryEvent":
        return TelemetryEvent(self.name,self.kind,self.trace_id or uuid.uuid4().hex,
                              self.timestamp or time.time(),dict(self.attributes))

class TelemetryRecorder:
    def __init__(self, max_events: int = 10000):
        self.max_events=max_events
        self.events:list[TelemetryEvent]=[]
    def record(self,event:TelemetryEvent)->TelemetryEvent:
        x=event.materialized()
        if len(self.events)>=self.max_events:self.events.pop(0)
        self.events.append(x); return x
    def trace(self,name:str,**attributes:Any)->TelemetryEvent:
        return self.record(TelemetryEvent(name,"trace",attributes=attributes))
    def snapshot(self)->list[dict[str,Any]]:
        return [{"name":e.name,"kind":e.kind,"trace_id":e.trace_id,
                 "timestamp":e.timestamp,"attributes":e.attributes} for e in self.events]
