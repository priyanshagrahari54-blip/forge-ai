"""Evidence quality classification for research outputs."""
def classify(evidence):
    if not evidence:return "UNKNOWN"
    confidence=[float(getattr(e,"confidence",0.0)) for e in evidence]
    return "HIGH" if sum(confidence)/len(confidence)>=.8 else ("MEDIUM" if sum(confidence)/len(confidence)>=.5 else "LOW")
