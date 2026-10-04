"""Default-deny network policy helper."""
from dataclasses import dataclass
@dataclass(frozen=True)
class NetworkPolicy:
    allowed_hosts:tuple=()
    def allows(self,host): return host in self.allowed_hosts
