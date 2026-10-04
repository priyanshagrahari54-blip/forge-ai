"""Normalize research evidence into a stable internal shape."""
from dataclasses import dataclass
@dataclass(frozen=True)
class ResearchEvidence:
    text:str
    source:str
    confidence:float=.5
    provenance:str=""
def adapt_evidence(items):
    out=[]
    for x in items or []:
        if isinstance(x,dict):out.append(ResearchEvidence(str(x.get("text") or x.get("content") or ""),str(x.get("source") or x.get("url") or ""),float(x.get("confidence",.5)),str(x.get("provenance") or "")))
        elif x:out.append(ResearchEvidence(str(x),""))
    return [x for x in out if x.text]
