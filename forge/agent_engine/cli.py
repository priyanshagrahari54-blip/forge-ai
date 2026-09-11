"""CLI for the Agent Creation Engine: ``forge agents`` (A81).

Commands:

    forge agents                        list created agents (default)
    forge agents create NAME            create from a template
    forge agents templates              list built-in templates
    forge agents show NAME              full detail for one agent
    forge agents validate NAME          re-validate a version
    forge agents test NAME              run the agent's benchmark suite
    forge agents enable NAME            enable (only from tested)
    forge agents pause NAME             pause a running agent
    forge agents resume NAME            resume a paused agent
    forge agents disable NAME           disable (re-entry requires re-test)
    forge agents retire NAME            retire permanently
    forge agents versions NAME          version history
    forge agents export NAME            print the structured package
    forge agents update NAME --spec F   publish a new version

Every command supports ``--json``. The store lives under
``.forge/agents`` by default (override with ``--agents-dir``).
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any


def _emit_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, default=str, sort_keys=True))


def _manager(args: argparse.Namespace) -> Any:
    from forge.agent_engine.manager import AgentManager

    return AgentManager(root=getattr(args, "agents_dir", "") or ".forge/agents",
                        workspace=getattr(args, "root", "") or ".")


def _print_error(exc: Exception, as_json: bool) -> int:
    if as_json:
        _emit_json({"ok": False, "error": str(exc)})
    else:
        print(f"error: {exc}", file=sys.stderr)
    return 1


def _run_agents_list(args: argparse.Namespace) -> int:
    manager = _manager(args)
    agents = manager.list_agents()
    if getattr(args, "json", False):
        _emit_json({"agents": agents})
        return 0
    if not agents:
        print("No agents created yet. Try `forge agents templates` and "
              "`forge agents create <name> --template coding`.")
        return 0
    print("Agents")
    for agent in agents:
        mark = "*" if agent["runnable"] else " "
        print(f" {mark} {agent['name']}  "
              f"v{agent['version']}  {agent['lifecycle']}  "
              f"template={agent['template'] or '-'}  "
              f"tools={','.join(agent['tools'])}")
    return 0


def _run_agents_create(args: argparse.Namespace) -> int:
    manager = _manager(args)
    try:
        if getattr(args, "spec", ""):
            with open(args.spec, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            payload["name"] = args.name
            if getattr(args, "purpose", ""):
                payload["purpose"] = args.purpose
            result = manager.create(args.name, spec=payload,
                                    template=getattr(args, "template", ""),
                                    created_by=getattr(args, "actor",
                                                       "cli"))
        else:
            result = manager.create(
                args.name, template=args.template,
                purpose=getattr(args, "purpose", ""),
                created_by=getattr(args, "actor", "cli"),
                overrides=getattr(args, "override_dict", None))
    except Exception as exc:
        return _print_error(exc, getattr(args, "json", False))
    if getattr(args, "json", False):
        _emit_json({"ok": True, **result})
    else:
        agent = result["agent"]
        print(f"Created agent {agent['name']!r} v{agent['version']} "
              f"({result['current_lifecycle']})")
        print(f"  purpose: {agent['spec']['purpose']}")
        print(f"  tools: {', '.join(agent['spec']['tools'])}")
        print(f"  permissions: {', '.join(agent['permissions'])}")
    return 0


def _run_agents_update(args: argparse.Namespace) -> int:
    manager = _manager(args)
    try:
        with open(args.spec, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        payload["name"] = args.name
        result = manager.create_version(
            args.name, payload, created_by=getattr(args, "actor", "cli"),
            changelog=getattr(args, "changelog", ""),
            operator_confirmed=getattr(args, "confirm_escalation", False))
    except Exception as exc:
        return _print_error(exc, getattr(args, "json", False))
    if getattr(args, "json", False):
        _emit_json({"ok": True, **result})
    else:
        agent = result["agent"]
        print(f"Published {agent['name']!r} v{agent['version']} "
              f"({result['current_lifecycle']})")
        print(f"  changelog: {agent['changelog'] or '-'}")
        print(f"  permissions: {', '.join(agent['permissions'])}")
    return 0


def _run_agents_test(args: argparse.Namespace) -> int:
    manager = _manager(args)
    try:
        result = manager.test(args.name, version=getattr(args, "version", None))
    except Exception as exc:
        return _print_error(exc, getattr(args, "json", False))
    if getattr(args, "json", False):
        _emit_json({"ok": True, **result})
    else:
        benchmark = result["benchmark"]
        print(f"Benchmark {benchmark['benchmark']!r} for {args.name} "
              f"v{benchmark['version']}:")
        for scenario in benchmark["scenarios"]:
            mark = "pass" if scenario["passed"] else "FAIL"
            print(f"  [{mark}] {scenario['id']}: {scenario['detail']}")
        print(f"  score={benchmark['score']:.2f} "
              f"({benchmark['passed']}/{benchmark['total']})")
        print(f"  lifecycle -> {result['current_lifecycle']}")
        if not benchmark["meets_requirements"]:
            print("  verification requirements NOT met; the agent stays "
                  "at `validated` until fixed.")
    return 0


def _run_agents_lifecycle(args: argparse.Namespace) -> int:
    manager = _manager(args)
    operation = args.agents_subcommand  # validate|enable|pause|resume|disable|retire
    try:
        result = getattr(manager, operation)(args.name)
    except Exception as exc:
        return _print_error(exc, getattr(args, "json", False))
    if getattr(args, "json", False):
        _emit_json({"ok": True, "operation": operation,
                    "name": args.name,
                    "lifecycle": result["current_lifecycle"]})
    else:
        print(f"{args.name}: {result['current_lifecycle']}")
    return 0


def _run_agents_show(args: argparse.Namespace) -> int:
    manager = _manager(args)
    try:
        result = manager.show(args.name,
                              version=getattr(args, "version", None))
    except Exception as exc:
        return _print_error(exc, getattr(args, "json", False))
    if getattr(args, "json", False):
        _emit_json(result)
        return 0
    agent = result["agent"]
    spec = agent["spec"]
    print(f"Agent: {agent['name']} v{agent['version']} "
          f"[{agent['lifecycle']}]")
    print(f"  purpose: {spec['purpose']}")
    print(f"  template: {agent['template'] or '-'}")
    print(f"  capabilities: {', '.join(spec['capabilities'])}")
    print(f"  tools: {', '.join(spec['tools'])}")
    print(f"  permissions: {', '.join(agent['permissions'])}")
    print(f"  model: {', '.join(spec['model']['capabilities'])} "
          f"(prefer {spec['model']['preference']}, "
          f"context>={spec['model']['min_context_tokens']})")
    print(f"  memory: enabled={spec['memory']['enabled']} "
          f"entries<={spec['memory']['max_entries']} "
          f"retention={spec['memory']['retention']}")
    print(f"  verification: benchmark={spec['verification']['benchmark']} "
          f"min_score={spec['verification']['min_score']}")
    print(f"  limits: tokens/request<={spec['limits']['max_tokens_per_request']} "
          f"requests<={spec['limits']['max_requests']} "
          f"wall<={spec['limits']['max_wall_seconds']}s "
          f"files<={spec['limits']['max_files_written']}")
    if spec["limits"]["working_dirs"]:
        print(f"  working dirs: {', '.join(spec['limits']['working_dirs'])}")
    print(f"  integrity: {result['integrity']['detail']}")
    return 0


def _run_agents_versions(args: argparse.Namespace) -> int:
    manager = _manager(args)
    try:
        versions = manager.versions(args.name)
    except Exception as exc:
        return _print_error(exc, getattr(args, "json", False))
    if getattr(args, "json", False):
        _emit_json({"versions": versions})
        return 0
    for entry in versions:
        mark = "*" if entry["is_current"] else " "
        print(f" {mark} v{entry['version']}  {entry['lifecycle']}  "
              f"by {entry['created_by']}  {entry['changelog'] or '-'}")
    return 0


def _run_agents_templates(args: argparse.Namespace) -> int:
    from forge.agent_engine.templates import template_catalog

    catalog = template_catalog()
    if getattr(args, "json", False):
        _emit_json({"templates": catalog})
        return 0
    print("Agent templates")
    for template in catalog:
        print(f"  {template['id']}: {template['description']}")
        print(f"    tools: {', '.join(template['tools'])}")
    return 0


def _run_agents_export(args: argparse.Namespace) -> int:
    manager = _manager(args)
    try:
        package = manager.export_package(args.name,
                                         version=getattr(args, "version", None))
    except Exception as exc:
        return _print_error(exc, getattr(args, "json", False))
    if getattr(args, "out", ""):
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(package, handle, indent=2, sort_keys=True)
        print(f"Exported {args.name} to {args.out}")
    else:
        _emit_json(package)
    return 0


def _run_agents(args: argparse.Namespace) -> int:
    subcommand = getattr(args, "agents_subcommand", "list") or "list"
    handlers = {
        "list": _run_agents_list,
        "create": _run_agents_create,
        "update": _run_agents_update,
        "validate": _run_agents_lifecycle,
        "enable": _run_agents_lifecycle,
        "pause": _run_agents_lifecycle,
        "resume": _run_agents_lifecycle,
        "disable": _run_agents_lifecycle,
        "retire": _run_agents_lifecycle,
        "test": _run_agents_test,
        "show": _run_agents_show,
        "versions": _run_agents_versions,
        "templates": _run_agents_templates,
        "export": _run_agents_export,
    }
    handler = handlers.get(subcommand)
    if handler is None:
        print(f"Unknown agents subcommand: {subcommand!r}", file=sys.stderr)
        return 2
    return handler(args)


def add_agents_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``agents`` command tree on an argparse subparsers."""
    agents_parser = subparsers.add_parser(
        "agents",
        help="Manage created agents (Agent Creation Engine)",
        description="First-party Agent Creation Engine: create specialized "
        "agents from structured specifications, move them through the "
        "validated lifecycle, benchmark them, and version them. Agents "
        "never self-grant permissions or self-enable.",
    )
    agents_parser.add_argument("--agents-dir", default="",
                               help="Agent store directory "
                               "(default: .forge/agents)")
    agents_parser.add_argument("--root", default=".",
                               help="Workspace root for benchmarks "
                               "(default: .)")
    agents_parser.add_argument("--json", action="store_true",
                               help="Emit machine-readable JSON")

    agents_subs = agents_parser.add_subparsers(dest="agents_subcommand")

    _list = agents_subs.add_parser("list", help="List created agents")
    _list.add_argument("--json", action="store_true", default=argparse.SUPPRESS)

    templates = agents_subs.add_parser("templates",
                                       help="List built-in templates")
    templates.add_argument("--json", action="store_true",
                           default=argparse.SUPPRESS)

    create = agents_subs.add_parser("create",
                                    help="Create an agent from a template "
                                    "or a spec file")
    create.add_argument("name")
    create.add_argument("--template", default="coding",
                        help="Template id (default: coding)")
    create.add_argument("--purpose", default="",
                        help="Override the template purpose")
    create.add_argument("--spec", default="",
                        help="Path to a JSON specification file "
                        "(takes precedence over --template)")
    create.add_argument("--actor", default="cli",
                        help="Who is creating the agent (audit)")
    create.add_argument("--json", action="store_true",
                        default=argparse.SUPPRESS)

    update = agents_subs.add_parser("update",
                                    help="Publish a new version from a "
                                    "spec file")
    update.add_argument("name")
    update.add_argument("--spec", required=True,
                        help="Path to the new version's JSON specification")
    update.add_argument("--changelog", default="",
                        help="What changed in this version")
    update.add_argument("--confirm-escalation", action="store_true",
                        help="Operator-confirmed permission escalation "
                        "(the only way a grant may ever grow)")
    update.add_argument("--actor", default="cli")
    update.add_argument("--json", action="store_true",
                        default=argparse.SUPPRESS)

    test = agents_subs.add_parser("test",
                                  help="Run the agent's benchmark suite")
    test.add_argument("name")
    test.add_argument("--version", type=int, default=None)
    test.add_argument("--json", action="store_true",
                      default=argparse.SUPPRESS)

    for _name in ("validate", "enable", "pause", "resume", "disable",
                  "retire"):
        _parser = agents_subs.add_parser(
            _name,
            help=f"{_name.capitalize()} the agent")
        _parser.add_argument("name")
        _parser.add_argument("--json", action="store_true",
                             default=argparse.SUPPRESS)

    show = agents_subs.add_parser("show", help="Show one agent in detail")
    show.add_argument("name")
    show.add_argument("--version", type=int, default=None)
    show.add_argument("--json", action="store_true",
                      default=argparse.SUPPRESS)

    versions = agents_subs.add_parser("versions",
                                      help="Show an agent's version history")
    versions.add_argument("name")
    versions.add_argument("--json", action="store_true",
                          default=argparse.SUPPRESS)

    export = agents_subs.add_parser("export",
                                    help="Print the structured agent package")
    export.add_argument("name")
    export.add_argument("--version", type=int, default=None)
    export.add_argument("--out", default="",
                        help="Write the package JSON to a file")
