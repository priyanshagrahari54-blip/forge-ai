"""Explain tool selection using declared metadata."""
def report(selected,required=()):
    return {"selected":[getattr(x,"name",str(x)) for x in (selected or ())],"required":list(required or ())}
