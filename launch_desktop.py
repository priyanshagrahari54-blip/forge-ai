"""Launch the Forge AI Desktop app (native Python GUI, no server needed).

Usage:
    python launch_desktop.py                        # pick a folder on first run
    python launch_desktop.py --project demo=C:/code/demo
    python launch_desktop.py --db C:/forge/desktop.db

Equivalent commands:
    forge desktop
    python -m forge.desktop_app
"""
from forge.desktop_app.app import main

if __name__ == "__main__":
    raise SystemExit(main())
