"""Truthful verification summary."""
def summarize_verification(checks):
    vals=[getattr(c,"status",c) for c in (checks or [])]
    vals=[getattr(v,"value",v) for v in vals]
    return "FAIL" if "FAIL" in vals else ("UNKNOWN" if not vals or "UNKNOWN" in vals else "PASS")
