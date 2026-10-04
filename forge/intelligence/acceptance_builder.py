"""Build measurable acceptance hints from a requirement contract."""
def build_acceptance(contract):
    if contract is None:return []
    return list(getattr(contract,"acceptance_criteria",[]) or [])
