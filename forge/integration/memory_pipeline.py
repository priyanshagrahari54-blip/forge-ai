"""Project-scoped memory facade for pipeline stages."""
def recall(memory,query=None):
    if memory is None:return []
    return memory.query() if query is None else memory.query(query)
