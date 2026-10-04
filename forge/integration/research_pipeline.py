"""Research request facade preserving source boundaries."""
def execute(engine,request):
    if request is None or not request.valid(): return {"status":"BLOCKED","reason":"invalid research request"}
    return engine(request.query) if callable(engine) else {"status":"UNKNOWN"}
