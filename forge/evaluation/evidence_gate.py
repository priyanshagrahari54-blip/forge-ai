"""Evidence gate that fails closed on missing proof."""
def passable(evidence):
    if not evidence:return False
    return all(getattr(e,"status",e) in ("PASS",getattr(getattr(e,"status",None),"PASS",None)) for e in evidence)
