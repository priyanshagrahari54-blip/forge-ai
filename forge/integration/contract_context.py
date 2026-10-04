"""Build a bounded execution context from a requirement contract."""
def build_contract_context(contract):
    if contract is None:return {}
    return {"intent":getattr(contract,"user_intent",""),"requirements":list(getattr(contract,"explicit_requirements",[]) or []),"quality":list(getattr(contract,"quality_requirements",[]) or []),"acceptance":list(getattr(contract,"acceptance_criteria",[]) or []),"status":str(getattr(contract,"status",""))}
