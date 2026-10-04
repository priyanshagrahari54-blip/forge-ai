"""Verification aggregation facade."""
def verify(checks, summarizer):
    return summarizer(checks) if callable(summarizer) else "UNKNOWN"
