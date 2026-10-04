"""Aggregate acceptance without treating unknown as pass."""
def summarize(results):
    vals=[getattr(x,"status",x) for x in (results or [])]
    vals=[getattr(v,"value",v) for v in vals]
    if "FAIL" in vals:return "FAIL"
    if not vals or "UNKNOWN" in vals:return "UNKNOWN"
    return "PASS"
