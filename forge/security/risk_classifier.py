"""Small deterministic action-risk classifier."""
class RiskClassifier:
    def classify(self, kind:str, action:str="")->str:
        text=f"{kind} {action}".lower()
        if any(x in text for x in ("delete","format","credential","secret","deploy","publish")):return "high"
        if kind in {"network","browser","terminal"}:return "medium"
        return "low"
