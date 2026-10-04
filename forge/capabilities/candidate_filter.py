"""Fail-closed candidate filtering."""
def verified_only(candidates):
    return [c for c in candidates or () if getattr(c,"usable",False)]
