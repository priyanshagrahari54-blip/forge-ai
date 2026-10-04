"""Health event for subsystem monitoring."""
def health_event(component,status,**details):
    return {"component":component,"status":status,**details}
