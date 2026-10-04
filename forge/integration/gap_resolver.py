"""Resolve explicit capability gaps through a supplied discovery function."""
def resolve(gap,discover):
    if gap is None or not getattr(gap,"can_discover",False) or not callable(discover): return []
    return discover(gap.name)
