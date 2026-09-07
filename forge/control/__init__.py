"""Secure control plane for the Forge browser cockpit (A34).

The control plane is the ONLY path between the browser and Forge core::

    Browser -> API -> ControlPlane -> A33 policy -> Supervisor -> ... -> Git

It owns sessions, the background worker, persistent events, approval
adaptation, checkpoints, and audit — but implements no policy of its own:
every authorization decision is delegated to the existing A33
permission/policy infrastructure.
"""
from forge.control.control_plane import ControlPlane, ControlConfig

__all__ = ["ControlPlane", "ControlConfig"]
