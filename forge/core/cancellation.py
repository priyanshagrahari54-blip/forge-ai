"""Cooperative cancellation token for long-running work."""
class CancellationToken:
    def __init__(self): self._cancelled=False
    def cancel(self): self._cancelled=True
    @property
    def cancelled(self): return self._cancelled
