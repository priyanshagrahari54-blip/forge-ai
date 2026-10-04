"""Run event helper for correlation and stage status."""
def run_event(trace_id,task_id,stage,status,**data):
    return {"trace_id":trace_id,"task_id":task_id,"stage":stage,"status":status,**data}
