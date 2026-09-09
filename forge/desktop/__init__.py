"""Desktop Agent execution architecture (A35).

A35 turns the A33 desktop *permission* foundation
(:mod:`forge.tools.desktop`) into a controlled execution architecture:

``DesktopRequest -> agent identity -> task scope -> A33 PolicyGate ->
permission check -> risk classification -> approval if required ->
execution (DesktopProvider) -> observation -> audit``

Modules:

- :mod:`forge.desktop.actions` — structured action vocabulary + validation
- :mod:`forge.desktop.provider` — backend-agnostic provider protocol and a
  deterministic, scriptable fake desktop for tests/dev
- :mod:`forge.desktop.risk` — risk classification and hard security
  invariants no profile can override
- :mod:`forge.desktop.profiles` — SAFE / ASSISTED / AUTONOMOUS / CUSTOM
- :mod:`forge.desktop.agent` — the policy-gated execution agent
- :mod:`forge.desktop.bridge` — the authenticated bridge boundary between
  the Forge server and the desktop provider

There is no unrestricted desktop control anywhere in this package. Every
action — including pure observations — passes the A33 permission system.
"""

from forge.desktop.actions import DesktopActionKind, DesktopRequest
from forge.desktop.agent import DesktopAgent, DesktopActionResult
from forge.desktop.bridge import DesktopBridge, DesktopBridgeError
from forge.desktop.profiles import DesktopProfile
from forge.desktop.provider import (
    DesktopProvider,
    DesktopProviderError,
    FakeDesktopProvider,
)
from forge.desktop.risk import RiskLevel, hard_violations, classify

__all__ = [
    "DesktopActionKind",
    "DesktopRequest",
    "DesktopAgent",
    "DesktopActionResult",
    "DesktopBridge",
    "DesktopBridgeError",
    "DesktopProfile",
    "DesktopProvider",
    "DesktopProviderError",
    "FakeDesktopProvider",
    "RiskLevel",
    "classify",
    "hard_violations",
]
