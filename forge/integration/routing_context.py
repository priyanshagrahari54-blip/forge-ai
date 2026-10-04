"""Derive routing inputs from a requirement contract without guessing provider availability."""
from forge.models.routing_intelligence import RoutingContext
def from_contract(contract):
    quality=.8 if getattr(contract,"quality_requirements",None) else .5
    text=str(getattr(contract,"user_intent","")).lower()
    return RoutingContext(task_type=text[:64],complexity=.8 if len(text)>300 else .5,quality=quality,privacy_sensitive=bool(getattr(contract,"resources",{}).get("private",False)) if hasattr(getattr(contract,"resources",{}),"get") else False)
