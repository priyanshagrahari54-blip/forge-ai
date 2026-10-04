"""Structured phase events for lightweight observability."""
def event(task_id,phase,status,**extra):
    return {"task_id":task_id,"phase":phase,"status":status,**extra}
