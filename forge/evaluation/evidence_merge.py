"""Merge evidence sources without converting unknown into pass."""
def merge_statuses(statuses):
    vals=list(statuses or [])
    if not vals:return "UNKNOWN"
    normalized=[getattr(x,"value",x) for x in vals]
    if "FAIL" in normalized:return "FAIL"
    if "UNKNOWN" in normalized:return "UNKNOWN"
    return "PASS"
