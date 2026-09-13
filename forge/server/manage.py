"""Forge Server management CLI (A81).

Run on the **server** machine::

    python -m forge.server add-client g560 --project demo [--max-mode assisted]
    python -m forge.server list-clients
    python -m forge.server rotate-secret g560
    python -m forge.server revoke-client g560
    python -m forge.server serve --project demo=/path/to/repo

``add-client`` prints the one-time client secret exactly once — copy it
to the desktop during setup; the server stores only a salted verifier.
"""
from __future__ import annotations

import argparse
import sys
from typing import Optional

from forge.control.control_plane import ControlConfig, ControlPlane
from forge.server.service import LinkService


def _plane(db_path: str, projects: Optional[dict[str, str]] = None) \
        -> ControlPlane:
    config = ControlConfig.from_env()
    if db_path:
        config.db_path = db_path
    if projects:
        config.projects = projects
    return ControlPlane(config)


def _require_service(args) -> LinkService:
    """Open the plane and mount every --project id=/path/to/repo spec.

    Projects are runtime state (not DB rows), so client management
    re-mounts the project directory the same way ``serve`` does.
    """
    plane = _plane(args.db, None)
    raw = getattr(args, "project", None) or []
    specs = raw if isinstance(raw, (list, tuple)) else [raw]
    for item in specs:
        project_id, _, root = item.partition("=")
        if not project_id or not root:
            raise SystemExit("--project requires id=/path/to/repo")
        try:
            plane.register_project(project_id, root)
        except Exception as exc:
            raise SystemExit(f"--project {item}: {exc}") from None
    return LinkService(plane)


def _print_secret(result: dict) -> None:
    print(f"client_id : {result['client_id']}")
    print(f"secret    : {result['secret']}")
    print()
    print("This secret is shown ONCE and is stored nowhere on the server")
    print("(only a salted verifier). Copy it to the desktop client now:")
    print("  forge client setup --server-url http://<server>:8000 "
          "--client-id " + result["client_id"])
    print("then paste the secret when prompted.")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m forge.server",
        description="Manage the Forge Server desktop-client link (A81).")
    parser.add_argument("--db", default="",
                        help="control-plane DB path "
                             "(default: FORGE_DB_PATH or .forge/cockpit.db)")
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add-client",
                         help="register a desktop client; prints a one-time "
                              "secret")
    add.add_argument("client_id", help="lowercase slug, e.g. g560")
    add.add_argument("--project", required=True,
                     help="server-side project this client is bound to")
    add.add_argument("--name", default="", help="human label")
    add.add_argument("--max-mode", default="assisted",
                     choices=("safe", "assisted", "autonomous"),
                     help="authorization ceiling for this client "
                          "(server-side, authoritative)")

    sub.add_parser("list-clients", help="list registered clients")

    rot = sub.add_parser("rotate-secret",
                         help="issue a new secret for a client")
    rot.add_argument("client_id")

    rev = sub.add_parser("revoke-client", help="revoke a client")
    rev.add_argument("client_id")

    serve = sub.add_parser("serve",
                           help="run the Forge Server (cockpit API + link)")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--project", action="append", default=[],
                       metavar="ID=/path/to/repo")

    args = parser.parse_args(argv)

    if args.command == "add-client":
        service = _require_service(args)
        project_id = args.project.split("=", 1)[0]
        if project_id not in service.plane.projects:
            print(f"error: unknown project {project_id!r}; mount it with "
                  f"--project {project_id}=/path/to/repo", file=sys.stderr)
            return 2
        result = service.register_client(args.client_id, project_id,
                                         name=args.name,
                                         max_mode=args.max_mode)
        _print_secret(result)
        return 0

    if args.command == "list-clients":
        service = _require_service(args)
        clients = service.list_clients()
        if not clients:
            print("no clients registered")
            return 0
        for client in clients:
            print("{:<20} project={:<12} max_mode={:<10} status={}".format(
                client["client_id"], client["project_id"],
                client["max_mode"], client["status"]))
        return 0

    if args.command == "rotate-secret":
        service = _require_service(args)
        _print_secret(service.rotate_client_secret(args.client_id))
        return 0

    if args.command == "revoke-client":
        service = _require_service(args)
        service.revoke_client(args.client_id)
        print(f"revoked {args.client_id}")
        return 0

    if args.command == "serve":
        from forge.api.server import run
        projects = {}
        for item in args.project:
            project_id, _, root = item.partition("=")
            if not project_id or not root:
                print("serve --project requires id=/path/to/repo",
                      file=sys.stderr)
                return 2
            projects[project_id] = root
        print(f"Forge Server: http://{args.host}:{args.port} "
              "(cockpit + desktop-client link)")
        print("Desktop clients connect with signed A81 link sessions; "
              "browser auth stays local-dev.")
        run(args.host, args.port, projects=projects or None,
            db_path=args.db)
        return 0


    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
