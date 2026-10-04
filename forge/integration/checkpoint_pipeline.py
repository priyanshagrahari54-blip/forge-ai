"""Checkpoint facade; does not claim persistence unless backend confirms it."""
def create(manager,task_id):
    if manager is None or not hasattr(manager,"create"): return None
    return manager.create(task_id)
