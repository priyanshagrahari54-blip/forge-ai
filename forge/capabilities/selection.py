"""Verified-first capability selection facade."""
def select_verified(candidates):
    usable=[c for c in (candidates or []) if getattr(c,"usable",False)]
    return sorted(usable,key=lambda c:(getattr(c,"quality_score",0),getattr(c,"name","")),reverse=True)
