"""Allow ``python -m forge.server`` (server-side client management)."""
from forge.server.manage import main

if __name__ == "__main__":
    raise SystemExit(main())
