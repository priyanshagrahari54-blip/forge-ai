"""Benchmark execution facade."""
def measure(lab,spec,fn):
    if lab is None or not callable(fn): return None
    return lab.measure(spec,fn)
