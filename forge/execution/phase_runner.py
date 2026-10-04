"""Minimal phase runner with explicit success/failure semantics."""
def run(phases,handler):
    results=[]
    for phase in phases or ():
        try: results.append((phase,handler(phase)))
        except Exception as exc: results.append((phase,{"status":"FAIL","error":str(exc)})); break
    return results
