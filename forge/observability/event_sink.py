"""Small event sink compatible with existing telemetry primitives."""
class EventSink:
    def __init__(self, recorder=None): self.recorder=recorder
    def emit(self,event):
        if self.recorder is not None and hasattr(self.recorder,"record"): self.recorder.record(event)
        return event
