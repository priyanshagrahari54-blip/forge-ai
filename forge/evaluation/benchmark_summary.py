"""Compact benchmark aggregation without inventing measurements."""
def summarize(results):
    vals=[r for r in (results or []) if r is not None]
    if not vals:return {"count":0,"measured":False}
    scores=[]
    for r in vals:
        if isinstance(r,dict) and isinstance(r.get("score"),(int,float)):scores.append(float(r["score"]))
    return {"count":len(vals),"measured":bool(scores),"mean_score":sum(scores)/len(scores) if scores else None}
