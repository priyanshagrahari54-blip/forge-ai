"""Record stage events without making telemetry mandatory for correctness."""
def record(sink,event):
    if sink is None:return event
    return sink.emit(event) if hasattr(sink,"emit") else event
