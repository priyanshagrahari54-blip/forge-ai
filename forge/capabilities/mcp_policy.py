"""MCP boundary policy: discovery metadata is never executable authority."""
from __future__ import annotations
from dataclasses import dataclass

@dataclass(frozen=True)
class MCPServerDecision:
    server: str
    allowed: bool
    reason: str
    permissions: tuple[str,...]=()

def review_server(server: str, *, verified: bool=False,
                  license_ok: bool=False, security_ok: bool=False,
                  permissions=()):
    if not verified:return MCPServerDecision(server,False,"server is unverified")
    if not license_ok:return MCPServerDecision(server,False,"license/provenance not approved")
    if not security_ok:return MCPServerDecision(server,False,"security review not passed")
    return MCPServerDecision(server,True,"verified; least-privilege permissions required",
                             tuple(sorted(set(permissions))))
