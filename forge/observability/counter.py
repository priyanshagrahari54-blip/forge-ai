"""Tiny process-local counter for subsystem events."""
class Counter:
    def __init__(self): self.value=0
    def inc(self,n=1): self.value+=n; return self.value
