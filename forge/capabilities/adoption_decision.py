"""Turn adoption gates into a truthful decision."""
def decide(gate):
    approved=bool(getattr(gate,"approved",False))
    return "ADOPT" if approved else "BLOCK"
