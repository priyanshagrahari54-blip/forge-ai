"""Explain model routing decisions from available metadata only."""
def explain(route):
    return {"selected":getattr(route,"model",None),"provider":getattr(route,"provider",None),"reason":getattr(route,"reason","advisory routing")}
