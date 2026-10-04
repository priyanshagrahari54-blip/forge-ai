"""Convert contract checks into explicit evidence statuses."""
def contract_evidence(contract):
    if contract is None:return "UNKNOWN"
    status=getattr(contract,"status",None)
    value=getattr(status,"value",status)
    if value in ("READY","ready"):return "PASS"
    return "UNKNOWN"
