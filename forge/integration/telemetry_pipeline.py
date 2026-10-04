"""Telemetry facade that never becomes a correctness dependency."""
def emit(recorder,event):
    if recorder is None:return False
    if hasattr(recorder,"record"): recorder.record(event); return True
    return False
