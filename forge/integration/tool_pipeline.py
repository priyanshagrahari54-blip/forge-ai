"""Tool invocation facade with fail-closed policy."""
def invoke(tool,request,policy):
    if policy is None or not policy(tool,request): return {"status":"BLOCKED"}
    return tool(request) if callable(tool) else {"status":"UNKNOWN"}
