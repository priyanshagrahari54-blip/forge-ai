"""Adapt planner output into a stable integration payload."""
def adapt_plan(plan):
    if plan is None:return {}
    return {"requirements":list(getattr(plan,"requirements",[]) or []),"architecture":getattr(plan,"architecture",None),"tasks":getattr(plan,"tasks",[]) or [],"acceptance":list(getattr(plan,"acceptance_criteria",[]) or [])}
