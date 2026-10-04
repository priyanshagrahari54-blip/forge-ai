"""Quality-bar normalization used by routing and acceptance."""
def normalize(bar="standard"):
    b=str(bar or "standard").lower()
    return {"name":b,"strict":b in {"professional","production","aaa","critical"},"requires_evidence":b not in {"draft","prototype"}}
