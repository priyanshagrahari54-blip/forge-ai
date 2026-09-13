"""``forge agents`` — the Agent Creation Engine on the command line.

Every subcommand is a thin, honest rendering of the engine: it prints what
the engine reports and exits non-zero when the engine refuses. Nothing is
pre-marked as passed, and no subcommand bypasses validation, the
lifecycle, the grant ledger, or the PolicyGate.

.. code-block:: text

    forge agents                                  list agents
    forge agents templates                        first-party templates
    forge agents create --template coding --name exporter
    forge agents validate exporter
    forge agents grant exporter write_file
    forge agents test exporter
    forge agents enable exporter
    forge agents run exporter "add CSV export" --approve
    forge agents disable exporter
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from forge.agents.engine.core import AgentCreationEngine
from forge.agents.engine.errors import AgentEngineError

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2

#: Lifecycle subcommands that map straight onto an engine method.
STATE_COMMANDS = {
    "validate": "validate",
    "test": "test",
    "enable": "enable",
    "disable": "disable",
    "pause": "pause",
    "resume": "resume",
    "retire": "retire",
}


def _emit(payload: Any) -> None:
    print(json.dumps(payload, indent=2, default=str))


def _actor(args: Any) -> str:
    return (getattr(args, "actor", "") or "").strip() or "cli"


def _engine(args: Any, *, with_model: bool = False) -> AgentCreationEngine:
    """Build an engine for ``--root``, optionally with a Model Fabric."""
    root = getattr(args, "root", "") or "."
    fabric = None
    if with_model:
        from forge.models import FabricConfig, ModelFabric

        config = FabricConfig.load(getattr(args, "config", "") or None)
        config.validate()
        fabric = ModelFabric.from_defaults(config)
    return AgentCreationEngine(root, fabric=fabric)


def _print_summary(agent: dict) -> None:
    print("%-20s %-10s v%-8s %s" % (agent["name"], agent["state"],
                                    agent["version"],
                                    ",".join(agent["capabilities"])))
    print("    tools: %s" % (", ".join(agent["tools"]) or "none"))
    print("    operations: %s (ceiling %s)"
          % (", ".join(agent["operations"]) or "none",
             agent["mode_ceiling"]))
    print("    purpose: %s" % agent["purpose"])


def _print_checks(report: dict) -> None:
    print("Validation %s (%d checks)"
          % ("PASSED" if report["passed"] else "FAILED",
             len(report.get("checks", []))))
    for check in report.get("checks", []):
        print("  [%s] %s: %s" % ("ok" if check["passed"] else "FAIL",
                                 check["name"], check["detail"]))


def _print_benchmark(report: dict) -> None:
    print("Benchmark %s — %s" % (
        "PASSED" if report["passed"] else "FAILED", report.get("reason", "")))
    print("  executed=%d passed=%d failed=%d skipped=%d pass_rate=%.2f "
          "(min %.2f)"
          % (report.get("executed", 0), report.get("passed_scenarios", 0),
             report.get("failed_scenarios", 0),
             report.get("skipped_scenarios", 0),
             report.get("pass_rate", 0.0), report.get("min_pass_rate", 0.0)))
    for scenario in report.get("scenarios", []):
        print("  [%-7s] %s: %s" % (scenario["status"].upper(),
                                   scenario["name"], scenario["detail"]))
    missing = report.get("missing_required") or []
    if missing:
        print("  required but not passed: %s" % ", ".join(missing))


def run_agents_cli(args: Any) -> int:
    """Dispatch one ``forge agents`` invocation. Returns an exit code."""
    command = getattr(args, "agents_subcommand", "list") or "list"
    as_json = bool(getattr(args, "json", False))

    try:
        if command == "templates":
            engine = AgentCreationEngine(getattr(args, "root", "") or ".")
            templates = engine.templates()
            if as_json:
                _emit({"templates": templates})
                return EXIT_OK
            print("Agent templates")
            for template in templates:
                print("  %-18s %s" % (template["id"], template["title"]))
                print("      %s" % template["description"])
                print("      tools: %s" % ", ".join(template["tools"]))
                print("      ceiling: %s (%s)"
                      % (template["mode_ceiling"],
                         ", ".join(template["operations"])))
            return EXIT_OK

        with_model = bool(getattr(args, "with_model", False))
        if command == "run" and not getattr(args, "offline", False):
            # A run without a Model Fabric can only fail, so build one
            # unless the operator explicitly asked to stay offline.
            with_model = True
        engine = _engine(args, with_model=with_model)
        actor = _actor(args)

        if command == "list":
            agents = engine.list(state=getattr(args, "state", "") or "")
            if as_json:
                _emit({"agents": agents, "counts": {
                    "total": len(agents)}})
                return EXIT_OK
            if not agents:
                print("No agents defined under %s" % engine.root)
                print("Create one with: forge agents create "
                      "--template coding --name my-agent")
                return EXIT_OK
            print("Agents in %s" % engine.root)
            for agent in agents:
                _print_summary(agent)
            return EXIT_OK

        if command == "create":
            name = (getattr(args, "name", "") or "").strip()
            if not name:
                print("create needs --name", file=sys.stderr)
                return EXIT_USAGE
            spec_path = getattr(args, "spec", "") or ""
            if spec_path:
                payload = json.loads(
                    Path(spec_path).read_text(encoding="utf-8"))
                agent = engine.create_from_dict(
                    payload, actor=actor,
                    grant=bool(getattr(args, "grant", False)))
            else:
                template = getattr(args, "template", "") or ""
                if not template:
                    print("create needs --template or --spec",
                          file=sys.stderr)
                    return EXIT_USAGE
                agent = engine.create_from_template(
                    template, name, purpose=getattr(args, "purpose", "") or "",
                    actor=actor, grant=bool(getattr(args, "grant", False)))
            if as_json:
                _emit(agent)
                return EXIT_OK
            print("Created agent %r (state: %s, version %s)"
                  % (agent["name"], agent["state"], agent["version"]))
            print("  package: %s"
                  % (Path(engine.root) / ".forge" / "agents" / agent["name"]))
            if not getattr(args, "grant", False):
                print("  no permissions granted yet: forge agents grant %s "
                      "<operation>" % agent["name"])
            print("  next: forge agents validate %s" % agent["name"])
            return EXIT_OK

        name = (getattr(args, "agent", "") or "").strip()
        if not name:
            print("%s needs an agent name" % command, file=sys.stderr)
            return EXIT_USAGE

        if command == "show":
            detail = engine.detail(name)
            if as_json:
                _emit(detail)
                return EXIT_OK
            summary = detail["summary"]
            print("Agent %s" % summary["name"])
            print("  state: %s   version: %s   template: %s"
                  % (summary["state"], summary["version"],
                     summary["template"] or "-"))
            print("  purpose: %s" % summary["purpose"])
            print("  capabilities: %s" % ", ".join(summary["capabilities"]))
            print("  tools: %s" % (", ".join(summary["tools"]) or "none"))
            permissions = detail["permissions"]
            print("  granted: %s"
                  % (", ".join(permissions["granted"]) or "none"))
            print("  not granted: %s"
                  % (", ".join(permissions["not_granted"]) or "none"))
            print("  mode ceiling: %s" % permissions["mode_ceiling"])
            print("  allowed paths: %s"
                  % (", ".join(permissions["allowed_paths"]) or "any"))
            print("  denied paths: %s"
                  % (", ".join(permissions["denied_paths"]) or "none"))
            print("  memory: %s" % detail["memory"])
            print("  usage: %d run(s) last hour, %d active"
                  % (detail["usage"]["runs_last_hour"],
                     detail["usage"]["active_runs"]))
            print("  allowed transitions: %s"
                  % (", ".join(detail["allowed_transitions"]) or "none"))
            print("  versions: %s"
                  % ", ".join(item["version"]
                              for item in detail["versions"]))
            return EXIT_OK

        if command == "validate":
            report = engine.validate(name, actor=actor)
            if as_json:
                _emit(report)
            else:
                _print_checks(report)
            return EXIT_OK if report["passed"] else EXIT_FAILURE

        if command == "test":
            report = engine.test(
                name, actor=actor,
                include_model_checks=not getattr(args, "no_model_checks",
                                                 False))
            if as_json:
                _emit(report)
            else:
                _print_benchmark(report)
            return EXIT_OK if report["passed"] else EXIT_FAILURE

        if command in ("enable", "disable", "pause", "resume", "retire"):
            result = engine.set_state(name, _target_state(command),
                                      actor=actor)
            if as_json:
                _emit(result)
            else:
                print("Agent %r is now %s" % (name, result["state"]))
            return EXIT_OK

        if command == "run":
            task = getattr(args, "task", "") or ""
            if not task.strip():
                print("run needs a task", file=sys.stderr)
                return EXIT_USAGE
            result = engine.run(name, task, actor=actor,
                                approved=bool(getattr(args, "approve", False)))
            if as_json:
                _emit(result)
                return EXIT_OK if result["success"] else EXIT_FAILURE
            print("Run %s for %s: %s" % (result["run_id"], name,
                                         "OK" if result["success"]
                                         else "FAILED"))
            print("  stage: %s   model: %s (%s)"
                  % (result["stage"], result["model"] or "-",
                     result["provider"] or "-"))
            if result["output"]:
                print("  output: %s" % result["output"][:400])
            for action in result["actions"]:
                print("  [%s] %s %s" % ("ok" if action["allowed"] else "deny",
                                        action["tool"], action["path"]))
            for gate in result["gates"]:
                print("  [%s] %s: %s" % ("ok" if gate["passed"] else "FAIL",
                                         gate["name"], gate["details"]))
            if result["rolled_back"]:
                print("  rolled back: writes were reverted")
            if result["error"]:
                print("  error: %s" % result["error"])
            return EXIT_OK if result["success"] else EXIT_FAILURE

        if command == "permissions":
            permissions = engine.permissions(name)
            if as_json:
                _emit(permissions)
                return EXIT_OK
            print("Permissions for %s" % name)
            print("  ceiling (spec): %s"
                  % (", ".join(permissions["ceiling"]) or "none"))
            print("  granted: %s"
                  % (", ".join(permissions["granted"]) or "none"))
            print("  not granted: %s"
                  % (", ".join(permissions["not_granted"]) or "none"))
            print("  always blocked: %s"
                  % ", ".join(permissions["blocked_always"]))
            print("  mode ceiling: %s" % permissions["mode_ceiling"])
            return EXIT_OK

        if command in ("grant", "revoke"):
            operation = (getattr(args, "operation", "") or "").strip()
            if command == "grant" and getattr(args, "all_spec", False):
                created = engine.grant_spec(name, actor=actor)
                if as_json:
                    _emit({"granted": created})
                else:
                    print("Granted %d operation(s) from the spec: %s"
                          % (len(created),
                             ", ".join(item["operation"]
                                       for item in created) or "none"))
                return EXIT_OK
            if not operation:
                print("%s needs an operation" % command, file=sys.stderr)
                return EXIT_USAGE
            if command == "grant":
                record = engine.grant(name, operation, actor=actor,
                                      reason=getattr(args, "reason", "")
                                      or "")
                if as_json:
                    _emit(record)
                else:
                    print("Granted %r to %s by %s"
                          % (operation, name, record["granted_by"]))
                return EXIT_OK
            record = engine.revoke(name, operation, actor=actor,
                                   reason=getattr(args, "reason", "") or "")
            if as_json:
                _emit(record)
            else:
                print("Revoked %r from %s by %s"
                      % (operation, name, record["by"]))
            return EXIT_OK

        if command == "versions":
            versions = engine.versions(name)
            if as_json:
                _emit({"versions": versions})
                return EXIT_OK
            print("Versions of %s" % name)
            for record in versions:
                print("  v%-8s %s  %s  %s"
                      % (record["version"], record["fingerprint"][:12],
                         record["recorded_by"] or "-", record["notes"]))
            return EXIT_OK

        if command == "history":
            history = engine.history(name,
                                     limit=int(getattr(args, "limit", 10)))
            if as_json:
                _emit({"history": history})
                return EXIT_OK
            print("Run history for %s" % name)
            if not history:
                print("  no runs recorded")
            for entry in history:
                print("  %s %-7s stage=%-12s actions=%d refusals=%d %s"
                      % (entry["run_id"], "OK" if entry["success"] else "FAIL",
                         entry["stage"], entry["actions"], entry["refusals"],
                         entry["error"][:60]))
            return EXIT_OK

        if command == "update":
            spec_path = getattr(args, "spec", "") or ""
            if not spec_path:
                print("update needs --spec", file=sys.stderr)
                return EXIT_USAGE
            from forge.agents.engine.spec import validate_spec

            payload = json.loads(Path(spec_path).read_text(encoding="utf-8"))
            result = engine.update_spec(name, validate_spec(payload),
                                        actor=actor,
                                        notes=getattr(args, "notes", "") or "")
            if as_json:
                _emit(result)
                return EXIT_OK
            print("Agent %r is now v%s (%s change); state: %s"
                  % (name, result["version"], result["change"],
                     result["state"]))
            if result.get("revoked_operations"):
                print("  revoked by the change: %s"
                      % ", ".join(result["revoked_operations"]))
            print("  re-validate and re-test before enabling again")
            return EXIT_OK

        if command == "delete":
            result = engine.delete(name, actor=actor)
            if as_json:
                _emit(result)
            else:
                print("Deleted agent %r" % name)
            return EXIT_OK

        print("Unknown agents subcommand: %s" % command, file=sys.stderr)
        return EXIT_USAGE
    except AgentEngineError as exc:
        print("%s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return EXIT_FAILURE
    except (OSError, ValueError) as exc:
        print("Cannot complete %s: %s" % (command, exc), file=sys.stderr)
        return EXIT_FAILURE


def _target_state(command: str) -> str:
    from forge.agents.engine.lifecycle import AgentState

    return {"enable": AgentState.ENABLED, "disable": AgentState.DISABLED,
            "pause": AgentState.PAUSED, "resume": AgentState.ENABLED,
            "retire": AgentState.RETIRED}[command]


def build_parser(subparsers: Any, command: str = "agents") -> None:
    """Attach the engine's subcommand tree to an argparse parser.

    ``command`` is parameterised because the top-level name is a shared
    namespace: another engine may already own ``agents``, and two parsers
    registering the same name make every CLI entry point fail with
    "conflicting subparser".
    """
    parser = subparsers.add_parser(
        command, help="Create and manage Forge agents (specification engine)",
        description="Create, validate, benchmark, enable, and run "
                    "specialized Forge agents from structured "
                    "specifications.")
    parser.add_argument("--root", default=".",
                        help="Project root holding .forge/agents")
    parser.add_argument("--actor", default="",
                        help="Operator name recorded with every action")
    parser.add_argument("--json", action="store_true",
                        help="Emit machine-readable JSON")
    subs = parser.add_subparsers(dest="agents_subcommand")

    subs.add_parser("list", help="List agents")
    subs.add_parser("templates", help="List first-party agent templates")

    create = subs.add_parser("create", help="Create an agent package")
    create.add_argument("--name", required=True)
    create.add_argument("--template", default="",
                        help="coding | research | security | "
                             "game-development | os-development | "
                             "documentation")
    create.add_argument("--purpose", default="")
    create.add_argument("--spec", default="",
                        help="Path to a JSON specification (instead of "
                             "--template)")
    create.add_argument("--grant", action="store_true",
                        help="Also grant every operation the spec declares")

    for command, help_text in (
            ("show", "Show one agent in detail"),
            ("validate", "Run validation checks"),
            ("enable", "Enable a tested agent"),
            ("disable", "Disable an agent"),
            ("pause", "Pause an enabled agent"),
            ("resume", "Resume a paused agent"),
            ("retire", "Retire an agent (final)")):
        item = subs.add_parser(command, help=help_text)
        item.add_argument("agent")

    test = subs.add_parser("test", help="Benchmark an agent")
    test.add_argument("agent")
    test.add_argument("--no-model-checks", action="store_true",
                      help="Skip scenarios that need a reachable model")
    test.add_argument("--with-model", action="store_true",
                      help="Build the Model Fabric from configuration so "
                           "model scenarios can run")
    test.add_argument("--config", default="",
                      help="Model Fabric configuration file")

    run = subs.add_parser("run", help="Run one agent task")
    run.add_argument("agent")
    run.add_argument("task")
    run.add_argument("--approve", action="store_true",
                     help="Approve approval-required operations up front")
    run.add_argument("--offline", action="store_true",
                     help="Do not build a Model Fabric (runs will fail "
                          "honestly without one)")
    run.add_argument("--with-model", action="store_true",
                     help="Build the Model Fabric from configuration")
    run.add_argument("--config", default="",
                     help="Model Fabric configuration file")

    permissions = subs.add_parser("permissions",
                                  help="Show effective permissions")
    permissions.add_argument("agent")

    grant = subs.add_parser("grant", help="Grant an operation (operator)")
    grant.add_argument("agent")
    grant.add_argument("operation", nargs="?", default="")
    grant.add_argument("--all-spec", action="store_true",
                       help="Grant every operation the spec declares")
    grant.add_argument("--reason", default="")

    revoke = subs.add_parser("revoke", help="Revoke an operation")
    revoke.add_argument("agent")
    revoke.add_argument("operation")
    revoke.add_argument("--reason", default="")

    versions = subs.add_parser("versions", help="List recorded versions")
    versions.add_argument("agent")

    history = subs.add_parser("history", help="Show recorded runs")
    history.add_argument("agent")
    history.add_argument("--limit", default="10")

    update = subs.add_parser("update",
                             help="Apply a new specification (new version)")
    update.add_argument("agent")
    update.add_argument("--spec", required=True)
    update.add_argument("--notes", default="")

    delete = subs.add_parser("delete", help="Delete a retired agent package")
    delete.add_argument("agent")

    # Accept the global flags after the subcommand as well
    # (`forge agents list --json`), keeping the parent-level values when
    # they are absent here.
    for sub in subs.choices.values():
        sub.add_argument("--root", default=argparse.SUPPRESS,
                         help="Project root holding .forge/agents")
        sub.add_argument("--actor", default=argparse.SUPPRESS,
                         help="Operator name recorded with every action")
        sub.add_argument("--json", action="store_true",
                         default=argparse.SUPPRESS,
                         help="Emit machine-readable JSON")
