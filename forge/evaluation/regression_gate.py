"""Regression gate requiring explicit comparison results."""
def evaluate(current,baseline):
    if current is None or baseline is None:return "UNKNOWN"
    return "PASS" if current>=baseline else "FAIL"
