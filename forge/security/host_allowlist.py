"""Explicit host allowlist boundary."""
class HostAllowlist:
    def __init__(self,hosts=()): self.hosts=frozenset(hosts)
    def allows(self,host): return host in self.hosts
