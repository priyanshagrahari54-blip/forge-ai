"""Forge Desktop client: the lightweight side of the Forge link (A81).

This package runs on the *client* machine (e.g. a Lenovo G560). It is
deliberately small and dependency-free (stdlib only): it never imports
the model fabric, never runs the Supervisor pipeline, and never loads a
model — all heavy engineering happens on the Forge Server.

Layout:

* :mod:`forge.client.config`   — connection settings (server URL, client
  id, execution mode, timeouts, reconnect policy) + secret storage with
  ``0600`` files and no plaintext credentials in the settings JSON.
* :mod:`forge.client.transport`— signed HTTP transport (stdlib urllib).
* :mod:`forge.client.connection` — connection state machine + automatic
  reconnect with deterministic exponential backoff.
* :mod:`forge.client.estimator`— deterministic task size/kind estimation.
* :mod:`forge.client.resources`— bounded local resource probe.
* :mod:`forge.client.router`   — LOCAL/SERVER/HYBRID execution selector.
* :mod:`forge.client.local_exec` — strictly bounded local operations.
* :mod:`forge.client.client`   — :class:`ForgeClient` facade used by the
  desktop app backend (submit, snapshot, restore, approvals).
"""
from __future__ import annotations

__all__ = ["config", "transport", "connection", "estimator",
           "resources", "router", "local_exec", "client"]
