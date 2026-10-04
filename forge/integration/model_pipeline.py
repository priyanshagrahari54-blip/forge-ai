"""Model selection facade; provider truth remains owned by Model Fabric."""
def select(context, router):
    return router.choose(context) if router is not None and hasattr(router,"choose") else None
