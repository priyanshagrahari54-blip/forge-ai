"""Delivery gate requiring explicit PASS rather than absence of failure."""
def can_deliver(status):
    value=getattr(status,"value",status)
    return value=="PASS"
