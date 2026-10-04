"""Rollback facade requiring an explicit checkpoint reference."""
def rollback(manager,checkpoint):
    if manager is None or checkpoint is None or not checkpoint.valid(): return False
    return bool(manager.rollback(checkpoint.checkpoint_id)) if hasattr(manager,"rollback") else False
