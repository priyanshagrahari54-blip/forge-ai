"""Completion report that distinguishes proof from assertion."""
def build(status,evidence=(),artifacts=()):
    return {"status":getattr(status,"value",status),"evidence_count":len(evidence or ()),"artifact_count":len(artifacts or ())}
