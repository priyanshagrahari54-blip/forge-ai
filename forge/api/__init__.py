"""Versioned HTTP API for the Forge browser cockpit (A34).

The API is a thin translation layer over
:class:`forge.control.control_plane.ControlPlane`: it validates input,
authenticates sessions, and renders structured errors. It enforces no
policy itself — the control plane and A33 do.
"""
from forge.api.app import create_app

__all__ = ["create_app"]
