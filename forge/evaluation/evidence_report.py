"""Compact evidence report for delivery and audit."""
def report(evidence):
    vals=list(evidence or ())
    return {"count":len(vals),"statuses":[getattr(getattr(x,"status",None),"value",getattr(x,"status",x)) for x in vals]}
