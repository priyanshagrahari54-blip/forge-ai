"""Bridge research/evidence objects into bounded context items."""
from __future__ import annotations
from forge.intelligence.context_assembler import ContextItem

def research_to_context(items):
    out=[]
    for i,item in enumerate(items or []):
        if isinstance(item,dict):
            text=item.get("text") or item.get("content") or ""
            source=item.get("source") or item.get("url") or "research"
            confidence=float(item.get("confidence",.5))
        else:
            text=str(item); source="research"; confidence=.5
        if text.strip(): out.append(ContextItem(key=f"research:{i}",text=text,source=source,confidence=confidence,priority=5))
    return out
