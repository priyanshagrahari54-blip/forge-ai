"""Resolve capabilities through the existing broker when available."""
def resolve(request, broker):
    names=getattr(request,"names",()) or ()
    return [broker.resolve(name) for name in names] if broker is not None else []
