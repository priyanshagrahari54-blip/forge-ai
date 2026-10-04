"""Provider/model health normalization for routing."""
def normalize_health(value):
    if value is None:return {"available":False,"score":0.0}
    if isinstance(value,bool):return {"available":value,"score":1.0 if value else 0.0}
    available=bool(value.get("available",value.get("healthy",False))) if isinstance(value,dict) else False
    score=float(value.get("score",1.0 if available else 0.0)) if isinstance(value,dict) else 0.0
    return {"available":available,"score":max(0.0,min(1.0,score))}
