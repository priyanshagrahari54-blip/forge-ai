"""Explain an agent route without inventing unavailable evidence."""
def explain(route):
    if route is None:return {"selected":None,"reason":"no route"}
    return {"selected":getattr(route,"agent",getattr(route,"name",None)),"reason":"ranked by declared capabilities and quality"}
