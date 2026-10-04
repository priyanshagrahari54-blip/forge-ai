"""Combine quality profile, evidence and benchmark outcomes."""
def evaluate(profile,evidence_status,benchmark_status):
    if "FAIL" in (evidence_status,benchmark_status): return "FAIL"
    if "UNKNOWN" in (evidence_status,benchmark_status): return "UNKNOWN"
    return "PASS" if profile else "UNKNOWN"
