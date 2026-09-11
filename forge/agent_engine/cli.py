"""`forge agents` CLI (A81).

Subcommands::

    forge agents                      list agents and their lifecycle state
    forge agents list                 (same)
    forge agents templates            show first-party templates
    forge agents create NAME          create from a template or a spec file
    forge agents show NAME            full package, versions, history
    forge agents validate NAME        run structural/security validation
    forge agents test NAME            run the code-judged benchmark
    forge agents enable NAME          operator-only; requires a passing test
    forge agents disable NAME         stop the agent
    forge agents pause NAME           temporarily stop the agent
    forge agents retire NAME          terminal
    forge agents versions NAME        version history

State lives in a JSON store (default ``.forge/agents/agents.json``) so
the CLI, the desktop Agent Manager, and tests all see the same agents.
No CLI command can grant an agent a permission its specification does
not declare.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from forge.agent_engine.engine import AgentCreationEngine, EngineError
from forge.agent_engine.lifecycle import LifecycleError
from forge.agent_engine.spec import SpecError
from forge.agent_engine.templates import TEMPLATE_NAMES, TEMPLATES
from forge.agent_engine.version import VersionError

DEFAULT_STORE = ".forge/agents/agents.json"


def _emit(payload: Any) -> None:
    print(json.dumps(payload, indent=2, default=str, sort_keys=True))


def load_engine(store: str = "") -> AgentCreationEngine:
    path = store or DEFAULT_STORE
    engine = AgentCreationEngine(store_dir=str(Path(path).parent))
    engine.load(path)
    # Restore lifecycle states recorded alongside the packages.
    source = Path(path)
    if source.exists():
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except ValueError:
            payload = {}
        for name, state in (payload.get("states") or {}).items():
            if name in engine.list_names():
                engine.lifecycle.register(name, state)
                engine.get(name).state = state
        for item in payload.get("agents", ()):
            name = (item.get("manifest") or {}).get("name", "")
            if name in engine.list_names():
                package = engine.get(name)
                package.validation = item.get("validation") or {}
                package.benchmark = item.get("benchmark") or {}
    return engine


def add_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "agents",
        help="Create and manage specialized Forge agents",
        description="Forge Agent Creation Engine: build agents from "
                    "structured specifications, validate them, benchmark "
                    "them, and control their lifecycle. Agents never "
                    "grant themselves permissions.")
    parser.add_argument("--store", default=DEFAULT_STORE,
                        help="Agent store path (default: {0})".format(
                            DEFAULT_STORE))
    parser.add_argument("--json", action="store_true",
                        help="Emit machine-readable JSON")
    subs = parser.add_subparsers(dest="agents_subcommand")

    subs.add_parser("list", help="List agents and lifecycle states")
    subs.add_parser("templates", help="Show first-party agent templates")

    create = subs.add_parser("create", help="Create an agent")
    create.add_argument("name", nargs="?", default="",
                        help="Agent name (with --template)")
    create.add_argument("--template", default="",
                        help="Template: {0}".format(
                            ", ".join(TEMPLATE_NAMES)))
    create.add_argument("--spec", default="",
                        help="Path to a JSON specification file")
    create.add_argument("--purpose", default="")
    create.add_argument("--actor", default="operator")

    for name, help_text in (
            ("show", "Show a package, its versions, and its history"),
            ("validate", "Validate the package"),
            ("test", "Run the agent benchmark suite"),
            ("enable", "Enable a tested agent"),
            ("disable", "Disable an agent"),
            ("pause", "Pause an enabled agent"),
            ("retire", "Retire an agent (terminal)"),
            ("versions", "Show the version history")):
        sub = subs.add_parser(name, help=help_text)
        sub.add_argument("name")
        sub.add_argument("--actor", default="operator")
        if name == "test":
            sub.add_argument("--no-model", action="store_true",
                             help="Static checks only (no model calls)")


def _print_list(rows, as_json: bool) -> int:
    if as_json:
        _emit(rows)
        return 0
    if not rows:
        print("No agents yet. Try: forge agents create --template coding")
        return 0
    print("{0:<26} {1:<9} {2:<10} {3}".format(
        "NAME", "VERSION", "STATE", "PURPOSE"))
    for row in rows:
        print("{0:<26} {1:<9} {2:<10} {3}".format(
            row["name"][:26], row["version"], row["state"],
            row["purpose"][:60]))
    return 0


def run(args) -> int:
    as_json = bool(getattr(args, "json", False))
    store = getattr(args, "store", "") or DEFAULT_STORE
    sub = getattr(args, "agents_subcommand", "") or "list"

    if sub == "templates":
        rows = [{"template": name,
                 "name": TEMPLATES[name]["name"],
                 "purpose": TEMPLATES[name]["purpose"],
                 "capabilities": TEMPLATES[name]["capabilities"],
                 "tools": TEMPLATES[name]["tools"]}
                for name in TEMPLATE_NAMES]
        if as_json:
            _emit(rows)
        else:
            for row in rows:
                print("{0:<16} {1}".format(row["template"], row["purpose"]))
        return 0

    try:
        engine = load_engine(store)
    except (EngineError, SpecError) as exc:
        print("Cannot read the agent store: {0}".format(exc))
        return 2

    def persist() -> None:
        engine.save(store)

    try:
        if sub == "list":
            return _print_list(engine.list(), as_json)

        if sub == "create":
            if getattr(args, "spec", ""):
                payload = json.loads(
                    Path(args.spec).read_text(encoding="utf-8"))
                if getattr(args, "name", ""):
                    payload["name"] = args.name
                if getattr(args, "purpose", ""):
                    payload["purpose"] = args.purpose
                package = engine.create(payload, actor=args.actor)
            else:
                template = getattr(args, "template", "")
                if not template:
                    print("Provide --template NAME or --spec FILE "
                          "(templates: {0})".format(
                              ", ".join(TEMPLATE_NAMES)))
                    return 2
                overrides = {}
                if getattr(args, "name", ""):
                    overrides["name"] = args.name
                if getattr(args, "purpose", ""):
                    overrides["purpose"] = args.purpose
                package = engine.create(template=template,
                                        overrides=overrides,
                                        actor=args.actor)
            result = engine.validate(package.name, actor=args.actor)
            persist()
            payload = {"agent": package.name,
                       "version": str(package.version),
                       "state": package.state,
                       "package_id": package.package_id(),
                       "validation": result}
            if as_json:
                _emit(payload)
            else:
                print("Created {0} v{1} ({2})".format(
                    package.name, package.version, package.state))
                print("  package: {0}".format(package.package_id()))
                print("  valid:   {0}".format(result["valid"]))
                for finding in result["findings"]:
                    print("  [{0}] {1}".format(finding["severity"],
                                               finding["message"]))
                print("  next:    forge agents test {0}".format(
                    package.name))
            return 0 if result["valid"] else 1

        if sub == "show":
            payload = engine.status(args.name)
            if as_json:
                _emit(payload)
            else:
                manifest = payload["manifest"]
                print("{0} v{1} [{2}]".format(
                    manifest["name"], manifest["version"],
                    manifest["state"]))
                print("  purpose:      {0}".format(
                    payload["spec"]["purpose"]))
                print("  capabilities: {0}".format(
                    ", ".join(payload["spec"]["capabilities"])))
                print("  tools:        {0}".format(
                    ", ".join(payload["spec"]["tools"]) or "none"))
                print("  gates:        {0}".format(", ".join(
                    payload["spec"]["verification"]["required_gates"])))
                print("  runnable:     {0}".format(payload["runnable"]))
            return 0

        if sub == "validate":
            result = engine.validate(args.name, actor=args.actor)
            persist()
            if as_json:
                _emit(result)
            else:
                print("{0}: {1}".format(
                    args.name, "valid" if result["valid"] else "INVALID"))
                for finding in result["findings"]:
                    print("  [{0}] {1}".format(finding["severity"],
                                               finding["message"]))
            return 0 if result["valid"] else 1

        if sub == "test":
            report = engine.test(
                args.name, actor=args.actor,
                include_behavioural=not getattr(args, "no_model", False))
            persist()
            if as_json:
                _emit(report)
            else:
                print("{0}: benchmark {1} ({2}/{3})".format(
                    args.name, "PASSED" if report.get("passed")
                    else "FAILED", report.get("passed_count", 0),
                    report.get("total", 0)))
                for check in report.get("failures", ()):
                    print("  FAIL {0}: {1}".format(check["name"],
                                                   check["details"]))
                if report.get("passed"):
                    print("  next: forge agents enable {0}".format(
                        args.name))
            return 0 if report.get("passed") else 1

        if sub in ("enable", "disable", "pause", "retire"):
            action = getattr(engine, sub)
            result = action(args.name, actor=args.actor)
            persist()
            if as_json:
                _emit(result)
            else:
                print("{0}: {1}".format(result["agent"], result["state"]))
            return 0

        if sub == "versions":
            entries = engine.versions(args.name)
            if as_json:
                _emit(entries)
            else:
                for entry in entries:
                    print("{0:<9} {1:<9} {2}".format(
                        entry["version"], entry["level"], entry["note"]))
            return 0

    except (EngineError, SpecError, LifecycleError, VersionError) as exc:
        if as_json:
            _emit({"error": str(exc)})
        else:
            print("Refused: {0}".format(exc))
        return 1
    except (OSError, ValueError) as exc:
        if as_json:
            _emit({"error": str(exc)})
        else:
            print("Failed: {0}".format(exc))
        return 2

    print("Unknown subcommand: {0}".format(sub))
    return 2
