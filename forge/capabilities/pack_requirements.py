"""Capability-pack requirement normalization."""
def normalize_capabilities(values):
    aliases={"web":"research","browser":"research","coding":"software","programming":"software","3d":"creative","vfx":"creative","operating-system":"os"}
    return sorted({aliases.get(str(v).lower(),str(v).lower()) for v in (values or [])})
