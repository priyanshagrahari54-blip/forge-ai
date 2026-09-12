"""Forge Desktop ↔ Forge Server link (A81).

This package holds the pieces *both* sides of the link share: the
authentication/handshake protocol and the error taxonomy. It has zero
third-party dependencies so the lightweight desktop client (e.g. a
Lenovo G560) only needs stdlib plus the :mod:`forge.client` stack.

Layout:

* :mod:`forge.link.protocol` — deterministic challenge/response
  protocol: no plaintext credential is ever stored or transmitted.
* :mod:`forge.link.errors` — typed link failures used by both sides.

The server side lives in :mod:`forge.server`, the client side in
:mod:`forge.client`. See ``docs/A81-DESKTOP-SERVER-LINK.md``.
"""
from __future__ import annotations

__all__ = ["protocol", "errors"]
