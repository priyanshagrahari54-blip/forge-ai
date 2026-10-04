"""Agent selection facade using capability-derived roles."""
def select(capabilities, router):
    return router.choose(capabilities) if router is not None and hasattr(router,"choose") else None
