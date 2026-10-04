"""Contract-aware acceptance facade."""
def accept(contract_status,quality_status,evidence_status):
    vals=(contract_status,quality_status,evidence_status)
    return "PASS" if all(getattr(v,"value",v)=="PASS" for v in vals) else ("FAIL" if "FAIL" in [getattr(v,"value",v) for v in vals] else "UNKNOWN")
