"""Requirement-first pipeline facade."""
def prepare(request, analyzer):
    return analyzer(request) if callable(analyzer) else request
