"""Normalize model requirements without selecting a provider."""
def normalize(requirements=None):
    r=requirements or {}
    return {"capability":str(r.get("capability","text")),"quality":float(r.get("quality",.5)),"latency":float(r.get("latency",.5)),"cost":float(r.get("cost",.5)),"privacy":bool(r.get("privacy",False))}
