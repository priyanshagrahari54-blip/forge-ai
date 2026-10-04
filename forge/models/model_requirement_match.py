"""Match model metadata against hard requirements."""
def matches(model,required):
    required=required or {}
    return all(getattr(model,k,None)==v for k,v in required.items() if v is not None)
