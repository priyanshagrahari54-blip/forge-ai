"""Truthful capability resolution report."""
def report(resolutions):
    vals=list(resolutions or [])
    return {"total":len(vals),"available":sum(bool(getattr(x,"available",False)) for x in vals),"blocked":sum(not bool(getattr(x,"available",False)) for x in vals)}
