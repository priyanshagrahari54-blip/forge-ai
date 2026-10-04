"""Run lifecycle facade."""
def start(run_store,run):
    if run_store is None:return None
    return run_store.create(run) if hasattr(run_store,"create") else None
