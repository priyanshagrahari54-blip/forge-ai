"""Forge AI Desktop: a native Python desktop app for Forge.

This package is the *desktop application* (a Tkinter GUI that runs on
Windows, macOS, and Linux with no extra dependencies). It embeds the Forge
:class:`~forge.control.control_plane.ControlPlane` in-process, so there is
no server to install or port to configure.

It is intentionally separate from :mod:`forge.desktop`, which is the
*desktop-control agent* (Forge driving the OS desktop), not an app you run.

Layout:

* :mod:`forge.desktop_app.backend` — headless ControlPlane wrapper. All
  product logic lives here and is covered by unit tests.
* :mod:`forge.desktop_app.app` — the Tkinter user interface (thin view over
  the backend; requires a display + stdlib ``tkinter``).

Launch with ``forge desktop``, ``python -m forge.desktop_app``, or
``python launch_desktop.py``.
"""

from __future__ import annotations

__version__ = "1.0.0"
__all__ = ["__version__"]
