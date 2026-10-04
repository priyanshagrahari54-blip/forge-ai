"""Monotonic deadline helper."""
import time
class Deadline:
    def __init__(self,seconds): self.end=time.monotonic()+max(0.0,seconds)
    def expired(self): return time.monotonic()>=self.end
