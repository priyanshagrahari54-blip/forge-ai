"""Forge Server: the heavy-engineering side of the Forge Desktop link.

The Forge Server is a machine with the real resources (CPU, RAM, models)
that runs the existing Forge cockpit application plus the authenticated
desktop-client link (A81). It executes every task through the existing
:class:`~forge.control.control_plane.ControlPlane`, whose dispatcher and
worker pool are independent of any connected client: **a desktop that
disconnects mid-task never interrupts server work**.

Layout:

* :mod:`forge.server.store` — SQLite persistence for registered desktop
  clients, handshake state, and the signature replay cache.
* :mod:`forge.server.service` — :class:`LinkService`: registration,
  handshake, per-request signature verification, and task/approval
  operations delegated to the ControlPlane under **server-side
  authoritative authorization**.
* :mod:`forge.server.manage` — CLI to add/list/revoke clients
  (``python -m forge.server ...``); run on the server machine.

The HTTP surface lives in :mod:`forge.api.routes_link`, mounted by the
existing cockpit app (``forge serve``). Security properties: no plaintext
credentials at rest or on the wire (see :mod:`forge.link.protocol`), a
fixed endpoint vocabulary with no command passthrough, bounded requests,
and fail-closed authorization.
"""
from __future__ import annotations

__all__ = ["store", "service", "manage"]
