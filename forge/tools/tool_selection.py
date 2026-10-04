"""Verified-first tool selection helper."""
def select_tools(candidates,required=()):
    required=set(required or ())
    usable=[c for c in (candidates or []) if getattr(c,"usable",False)]
    return [c for c in usable if not required or getattr(c,"name",None) in required]
