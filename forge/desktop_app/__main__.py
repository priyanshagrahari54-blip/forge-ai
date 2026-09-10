"""Allow ``python -m forge.desktop_app``."""
from forge.desktop_app.app import main

if __name__ == "__main__":
    raise SystemExit(main())
