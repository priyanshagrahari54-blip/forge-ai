"""Benchmark gate: missing measurements cannot pass."""
def gate(results):
    if not results:return "UNKNOWN"
    values=[getattr(r,"status",r) for r in results]
    values=[getattr(v,"value",v) for v in values]
    return "FAIL" if "FAIL" in values else ("UNKNOWN" if "UNKNOWN" in values else "PASS")
