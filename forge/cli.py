from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sys

from forge.core.supervisor import Supervisor
from forge.intelligence.analyzer import ProjectAnalyzer
from forge.intelligence.report import generate_report
from forge.self_development import ForgeSelfAnalyzer, SelfDevelopmentLoop


def _emit_json(payload) -> None:
    print(json.dumps(payload, indent=2, default=str))


def _run_blender(args) -> int:
    """CLI entry for procedural Blender scenes."""
    from forge.tools.blender import BlenderTool

    tool = BlenderTool()
    subcommand = getattr(args, "blender_subcommand", "check") or "check"

    if subcommand == "example":
        spec = tool.example()
        if getattr(args, "out_file", ""):
            with open(args.out_file, "w", encoding="utf-8") as handle:
                json.dump(spec, handle, indent=2)
            print(f"Wrote example scene to {args.out_file}")
        elif args.json:
            _emit_json(spec)
        else:
            _emit_json(spec)
        return 0

    if subcommand == "render":
        report = tool.render_file(args.spec, args.out, timeout=args.timeout)
        if args.json:
            _emit_json(report)
        elif report["success"]:
            print(f"Rendered {report['spec_name']}:")
            for path in report["output_files"]:
                print(f"  {path}")
        else:
            print(f"Render failed: {report.get('error', 'unknown error')}")
        return 0 if report["success"] else 1

    report = tool.check()
    if args.json:
        _emit_json(report)
    elif report["available"]:
        print(f"Blender available: {report['blender']}")
        print(report.get("version", ""))
    else:
        print(report["hint"])
    return 0 if report["available"] else 1


def _run_higgsfield(args) -> int:
    """CLI entry for the Higgsfield generative-media API."""
    from forge.tools.higgsfield import HiggsfieldTool

    tool = HiggsfieldTool(timeout=getattr(args, "api_timeout", 30.0))
    subcommand = getattr(args, "higgsfield_subcommand", "status") or "status"

    if subcommand == "submit":
        params = {"prompt": args.prompt}
        for item in getattr(args, "param", []) or []:
            key, sep, value = item.partition("=")
            if not sep or not key.strip():
                print(f"Ignoring malformed --param {item!r} (want k=v)")
                continue
            params[key.strip()] = value
        report = tool.submit(args.model, params)
        if args.json:
            _emit_json(report)
        elif report.get("submitted"):
            print(f"Submitted {report['request_id']} "
                  f"(status: {report['status']})")
            print(f"  status: {report['status_url']}")
        else:
            print(f"Submit failed: {report.get('error', report)}")
        if not report.get("submitted"):
            return 1
        if getattr(args, "wait", False):
            return _run_higgsfield_wait(tool, report["request_id"], args)
        return 0

    if subcommand == "get":
        report = tool.get_status(args.request_id)
        return _print_higgsfield_report(report, args.json, "ok")

    if subcommand == "cancel":
        report = tool.cancel(args.request_id)
        return _print_higgsfield_report(report, args.json, "ok")

    if subcommand == "download":
        report = tool.download(args.url, args.out,
                               filename=getattr(args, "filename", None))
        if args.json:
            _emit_json(report)
        elif report.get("ok"):
            print(f"Downloaded {report['path']}")
        else:
            print(f"Download failed: {report.get('error')}")
        return 0 if report.get("ok") else 1

    report = tool.status()
    if args.json:
        _emit_json(report)
    elif report["configured"]:
        print(f"Higgsfield configured ({report['credential']})")
        print(f"  base: {report['base_url']}")
        print(f"  models: {', '.join(report['models'])}")
    else:
        print(report["hint"])
    return 0 if report["configured"] else 1


def _run_higgsfield_wait(tool, request_id, args) -> int:
    report = tool.wait(request_id, timeout=getattr(args, "timeout", 600.0),
                       interval=getattr(args, "interval", 3.0))
    return _print_higgsfield_report(report, args.json, "ok")


def _print_higgsfield_report(report, as_json, ok_key) -> int:
    if as_json:
        _emit_json(report)
        return 0 if report.get(ok_key) else 1
    if report.get(ok_key):
        _emit_json(report)
        return 0
    print(f"Higgsfield error: {report.get('error', report)}")
    return 1


def _run_models(args) -> None:
    """Render the default Model Fabric's registry to stdout."""
    from forge.models import ALL_CAPABILITIES, ModelFabric

    fabric = ModelFabric.from_defaults()
    subcommand = getattr(args, "models_subcommand", "list")

    if subcommand == "capabilities" or getattr(args, "capabilities", False):
        if args.json:
            print(json.dumps({"capabilities": list(ALL_CAPABILITIES)}, indent=2))
        else:
            print("Capabilities")
            for capability in ALL_CAPABILITIES:
                print(f"  {capability}")
        return

    if subcommand == "health":
        if args.json:
            print(json.dumps({"models": fabric.health(), "providers": fabric.provider_health()}, indent=2))
        else:
            print("Model Health")
            for name, state in fabric.health().items():
                print(
                    f"  {name}: {state['health']} (reliability={state['reliability']:.2f}, "
                    f"latency={state['latency_ms']:.1f}ms, available={state['available']})"
                )
        return

    if subcommand == "providers":
        if args.json:
            print(json.dumps({"providers": fabric.providers.snapshot()}, indent=2))
        else:
            print("Providers")
            for info in fabric.providers.snapshot():
                print(
                    f"  {info['name']}: kind={info['kind']} local={info['local']} "
                    f"free={info['free']} model={info['model'] or '-'}"
                )
        return

    if subcommand == "test":
        # Bounded, local self-check: route a trivial coding request. This never
        # fabricates output; the deterministic fallback refuses synthesis if no
        # real model is reachable. A placeholder answer is reported honestly
        # as such instead of a passing self-test.
        from forge.models import ModelRequest, is_fallback_response
        response = fabric.generate(ModelRequest(prompt="reply ok", capability="coding"))
        used_fallback = False
        try:
            used_fallback = is_fallback_response(
                fabric, response.model or "", response.provider or "")
        except Exception:
            used_fallback = False
        warning = ("answered by the offline placeholder: no real model is "
                   "reachable, so tasks cannot produce code. "
                   "Run `forge doctor`.") if used_fallback else ""
        if args.json:
            print(json.dumps({**response.to_dict(),
                              "used_fallback": used_fallback,
                              "warning": warning or None}, indent=2))
        else:
            print("Model Fabric self-test")
            print(f"  success={response.success and not used_fallback} model={response.model or '-'} provider={response.provider or '-'}")
            if response.error:
                print(f"  error={response.error}")
            if warning:
                print(f"  WARNING: {warning}")
        return

    # default: list
    models = fabric.models()
    capability_filter = getattr(args, "capability", None)
    if capability_filter:
        models = [model for model in models if model.supports(capability_filter)]

    if args.json:
        print(json.dumps({
            "models": [model.to_dict() for model in models],
            "capabilities": fabric.capabilities(),
        }, indent=2))
        return

    print("Models")
    for model in models:
        caps = ",".join(model.capabilities) or "-"
        cost = "free" if model.free else (f"${model.cost_per_token:.7f}/tok" if model.cost_per_token else "paid")
        origin = "local" if model.local else "remote"
        print(
            f"  {model.name}\n"
            f"    provider={model.provider} caps={caps}\n"
            f"    {cost} {origin} health={model.health.status} "
            f"reliability={model.reliability:.2f} latency={model.latency_ms:.1f}ms "
            f"context={model.context_window}"
        )


def _build_fabric(args) -> "object":
    """Build the Model Fabric from config file / env / CLI overrides."""
    from forge.models import FabricConfig, ModelFabric

    config_path = getattr(args, "config", "") or None
    config = FabricConfig.load(config_path)
    if getattr(args, "ollama_url", ""):
        config.ollama_url = args.ollama_url
    if getattr(args, "ollama_model", ""):
        config.ollama_model = args.ollama_model
    config.validate()
    return ModelFabric.from_defaults(config)


def _run_doctor(args) -> int:
    """Diagnose why tasks would fail; exit 0 when ready, 1 otherwise."""
    from forge.models import check_fabric_readiness

    fabric = _build_fabric(args)
    report = check_fabric_readiness(
        fabric, probe_network=not getattr(args, "offline", False),
        timeout=5.0)

    environment = {
        "python": platform.python_version(),
        "python_ok": sys.version_info >= (3, 11),
        "platform": platform.platform(),
        "cwd": os.getcwd(),
        "ollama_binary": shutil.which("ollama") or "",
        "openai_key_configured": bool(os.environ.get("OPENAI_API_KEY")),
        "git_binary": shutil.which("git") or "",
    }

    if getattr(args, "json", False):
        print(json.dumps({"ready": report.ready,
                          "environment": environment,
                          "readiness": report.to_dict()}, indent=2))
        return 0 if report.ready else 1

    print("Forge Doctor")
    print(f"  Python: {environment['python']} "
          f"({'ok (>=3.11)' if environment['python_ok'] else 'TOO OLD - Forge needs >=3.11'})")
    print(f"  Platform: {environment['platform']}")
    print(f"  Ollama binary: {environment['ollama_binary'] or 'not found on PATH'}")
    print("  OPENAI_API_KEY: "
          + ("configured" if environment["openai_key_configured"] else "not set"))
    print(f"  Git: {environment['git_binary'] or 'not found on PATH'}")
    print()
    if report.ready:
        print(f"READY - usable models: {', '.join(report.usable_models)}")
    else:
        print("NOT READY - tasks cannot produce code until this is fixed.")
    for check in report.checks:
        mark = "ok" if check.ok else "FAIL"
        print(f"  [{mark}] {check.name}: {check.detail}")
        if not check.ok and check.remediation:
            print(f"         fix: {check.remediation}")
    return 0 if report.ready else 1


def _run_task(args) -> int:
    """Run one autonomous task through the Supervisor; exit 0 when accepted."""
    from forge.models import describe_no_model_error, fabric_has_real_model
    from forge.security.permissions import OperationMode

    fabric = _build_fabric(args)
    try:
        has_real = fabric_has_real_model(fabric)
    except Exception:
        has_real = True
    if not has_real and not getattr(args, "force", False):
        message = describe_no_model_error(fabric=fabric)
        if getattr(args, "json", False):
            print(json.dumps({"accepted": False, "error": message,
                              "fallback_only": True}, indent=2))
        else:
            print(message, file=sys.stderr)
        return 2

    try:
        mode = OperationMode(args.mode)
    except ValueError:
        print(f"Unknown mode: {args.mode!r} "
              f"(expected safe|assisted|autonomous|locked)", file=sys.stderr)
        return 2

    approved = bool(getattr(args, "approve", False))
    if (mode == OperationMode.ASSISTED and not approved
            and not getattr(args, "force", False)):
        # Assisted mode requires an approver for every write and `forge run`
        # has no interactive approver, so the first write would fail. Fail
        # fast with guidance instead of running a doomed pipeline.
        message = ("Assisted mode needs an approver for every write, but "
                   "`forge run` cannot prompt for approval.\n"
                   "Re-run with --approve (pre-approves writes; DENY still "
                   "blocks them), use --mode autonomous, or approve each "
                   "write in the desktop/cockpit app.")
        if getattr(args, "json", False):
            print(json.dumps({"accepted": False, "error": message,
                              "needs_approval": True}, indent=2))
        else:
            print(message, file=sys.stderr)
        return 2

    supervisor = Supervisor(args.project, root=args.root)
    result = supervisor.run(
        args.requirement,
        approved=approved,
        fabric=fabric,
        max_debug_retries=max(0, int(getattr(args, "max_debug_retries", 3))),
        mode=mode,
    )
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2, default=str))
        return 0 if result.get("accepted") else 1

    status = "ACCEPTED" if result.get("accepted") else "FAILED"
    print(f"Task {status}")
    print(f"  requirement: {args.requirement[:120]}")
    print(f"  stages: {' -> '.join(result.get('stages', []))}")
    if result.get("selected_model"):
        print(f"  model: {result.get('selected_model')} "
              f"(provider={result.get('selected_provider', '-')})")
    if result.get("files"):
        print(f"  files: {', '.join(result.get('files', []))}")
    for gate in result.get("gates", []):
        name = gate.get("gate", "?") if isinstance(gate, dict) else gate
        passed = gate.get("passed", "?") if isinstance(gate, dict) else "?"
        print(f"  gate {name}: {'pass' if passed else 'FAIL'}")
    if result.get("error"):
        print(f"  error: {result.get('error')}")
    print(f"  duration: {result.get('duration_seconds', 0):.1f}s "
          f"rollback={result.get('rollback', False)}")
    return 0 if result.get("accepted") else 1


def _agents_engine(args):
    from forge.agents.creation import AgentCreationEngine

    store = getattr(args, "store", "") or ".forge/agent-engine.json"
    try:
        return AgentCreationEngine(store_path=store)
    except ValueError as exc:
        print(f"Agent store error: {exc}", file=sys.stderr)
        raise SystemExit(2)


def _load_spec_file(spec_path: str):
    """Load a JSON spec file with bounds and clean errors."""
    try:
        size = os.path.getsize(spec_path)
    except OSError as exc:
        raise ValueError("cannot read spec file %r: %s" % (spec_path, exc))
    if size > 64 * 1024:
        raise ValueError("spec file %r exceeds 64KB" % (spec_path,))
    try:
        with open(spec_path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as exc:
        raise ValueError("cannot parse spec file %r: %s" % (spec_path, exc))
    if not isinstance(payload, dict):
        raise ValueError("spec file %r must hold a JSON object"
                         % (spec_path,))
    return payload


def _agents_overrides(items) -> dict:
    overrides: dict = {}
    for item in items or []:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            print(f"Ignoring malformed --set {item!r} (want k=v or "
                  f"section.k=v)", file=sys.stderr)
            continue
        try:
            parsed = json.loads(value)
        except ValueError:
            parsed = value
        section, dot, sub = key.strip().partition(".")
        if dot and section in ("model_requirements", "memory_policy",
                               "verification_requirements",
                               "resource_limits"):
            overrides.setdefault(section, {})[sub] = parsed
        else:
            overrides[key.strip()] = parsed
    return overrides


def _agents_common(parser) -> None:
    """Flags accepted after any `forge agents <subcommand>` (parent
    values apply unless the subcommand overrides them)."""
    parser.add_argument("--json", action="store_true",
                        default=argparse.SUPPRESS)
    parser.add_argument("--store", default=argparse.SUPPRESS)
    parser.add_argument("--actor", default=argparse.SUPPRESS)


def _run_agents(args) -> int:
    """First-party Agent Creation Engine commands (specs → packages)."""
    from forge.agents.mediation import GatedAgentRuntime, MediationError

    subcommand = getattr(args, "agents_subcommand", "") or "list"
    as_json = getattr(args, "json", False)

    if subcommand == "templates":
        from forge.agents.creation import AgentCreationEngine

        templates = AgentCreationEngine().templates()
        if as_json:
            _emit_json({"templates": templates})
        else:
            print("Agent templates")
            for template in templates:
                print(f"  {template['id']} (role={template['role']})")
                print(f"    {template['purpose']}")
                print(f"    capabilities={','.join(template['capabilities'])}")
                print(f"    tools={','.join(template['tools'])}")
        return 0

    engine = _agents_engine(args)
    actor = getattr(args, "actor", "") or "cli"

    def show_package(package) -> int:
        if as_json:
            _emit_json(package.to_dict())
        else:
            manifest = package.manifest()
            print(f"Agent {manifest['name']} v{manifest['version']} "
                  f"[{manifest['state']}]")
            print(f"  identity={manifest['identity']} "
                  f"role={manifest['role'] or '-'} "
                  f"template={manifest['template'] or '-'}")
            print(f"  grants={manifest['grants']} "
                  f"spec={manifest['spec_hash']}")
            for pos, entry in enumerate(
                    package.spec.get("permissions", [])):
                print(f"  permission[{pos}] {entry.get('resource')}/"
                      f"{entry.get('operation')} "
                      f"scope={entry.get('scope')} "
                      f"risk={entry.get('risk')}")
            for pos, grant in enumerate(package.grants or []):
                granted = grant.get("permission", {})
                print(f"  grant[{pos}] {granted.get('resource')}/"
                      f"{granted.get('operation')} "
                      f"scope={granted.get('scope')} "
                      f"by={grant.get('approver')}")
        return 0

    try:
        if subcommand == "list":
            packages = [package.manifest() for package in engine.list()]
            if as_json:
                _emit_json({"agents": packages})
            elif not packages:
                print("No agents. Create one: "
                      "forge agents create --template coding --name NAME")
            else:
                print("Agents")
                for manifest in packages:
                    print(f"  {manifest['name']} v{manifest['version']} "
                          f"[{manifest['state']}] "
                          f"template={manifest['template'] or '-'} "
                          f"grants={manifest['grants']}")
            return 0

        if subcommand == "create":
            spec_path = getattr(args, "spec", "")
            if spec_path:
                payload = _load_spec_file(spec_path)
                if getattr(args, "name", ""):
                    payload["name"] = args.name
                package = engine.create_from_spec(
                    payload, created_by=actor)
            else:
                if not getattr(args, "template", "") \
                        or not getattr(args, "name", ""):
                    print("forge agents create needs --template TEMPLATE "
                          "--name NAME (or --spec FILE)", file=sys.stderr)
                    return 2
                package = engine.create_from_template(
                    args.template, args.name, created_by=actor,
                    overrides=_agents_overrides(getattr(args, "set", [])))
            if not as_json:
                print(f"Created agent {package.name} (state=created)")
            return show_package(package)

        if subcommand == "show":
            return show_package(engine.get(args.name))

        if subcommand == "validate":
            report = engine.validate(args.name, actor=actor)
            if as_json:
                _emit_json(report)
            elif report["valid"]:
                print(f"Agent {args.name} validated "
                      f"(state={report.get('state')})")
            else:
                print(f"Agent {args.name} is invalid:")
                for issue in report["issues"]:
                    print(f"  - {issue}")
                return 1
            return 0

        if subcommand == "test":
            report = engine.benchmark(args.name, actor=actor)
            if as_json:
                _emit_json(report)
            else:
                print(f"Benchmark {args.name}: {report['passed_count']}/"
                      f"{report['executed']} passed "
                      f"(score={report['score']}, "
                      f"min={report['min_score']}, "
                      f"state={report.get('state')})")
                for check in report["checks"]:
                    print(f"  [{check['status']}] {check['name']}"
                          + (f": {check['detail']}" if check["detail"]
                             else ""))
            return 0 if report["passed"] else 1

        if subcommand in ("enable", "pause", "resume", "disable",
                          "retire"):
            package = getattr(engine, subcommand)(args.name, actor=actor)
            if not as_json:
                print(f"Agent {args.name} -> {package.state}")
            return show_package(package)

        if subcommand == "grant":
            expected = getattr(args, "expect_json", "")
            if expected:
                try:
                    expected = json.loads(expected)
                except ValueError as exc:
                    print(f"Bad --expect-json: {exc}", file=sys.stderr)
                    return 2
                if not isinstance(expected, dict):
                    print("Bad --expect-json: want a JSON object",
                          file=sys.stderr)
                    return 2
            else:
                expected = None
            grant = engine.grant_permission(
                args.name, args.index,
                approver=getattr(args, "approver", "") or actor,
                expected=expected)
            if as_json:
                _emit_json({"agent": args.name, "grant": grant})
            else:
                permission = grant["permission"]
                print(f"Granted {permission['resource']}/"
                      f"{permission['operation']} scope="
                      f"{permission['scope']} to {args.name} "
                      f"(approver={grant['approver']})")
            return 0

        if subcommand == "revoke":
            revoked = engine.revoke_permission(
                args.name, args.index,
                approver=getattr(args, "approver", "") or actor)
            if as_json:
                _emit_json({"agent": args.name, "revoked": revoked})
            else:
                print(f"Revoked grant #{args.index} from {args.name} "
                      f"(approver={revoked.get('approver', actor)})")
            return 0

        if subcommand == "update":
            spec_path = getattr(args, "spec", "")
            if not spec_path:
                print("forge agents update needs --spec FILE "
                      "(full replacement spec)", file=sys.stderr)
                return 2
            payload = _load_spec_file(spec_path)
            package = engine.update(
                args.name, payload, actor=actor,
                reason=getattr(args, "reason", "") or "")
            if not as_json:
                print(f"Agent {args.name} updated to v{package.version} "
                      f"(state={package.state})")
            return show_package(package)

        if subcommand == "version":
            record = engine.publish_version(
                args.name, kind=getattr(args, "kind", "patch"),
                notes=getattr(args, "notes", ""), actor=actor)
            if as_json:
                _emit_json({"agent": args.name, "release": record})
            else:
                print(f"Agent {args.name} -> v{record['version']} "
                      f"({record['kind']})")
            return 0

        if subcommand == "run":
            from forge.models import ModelFabric
            from forge.runtime.defaults import create_default_runtime
            from forge.security.policy_gate import PolicyGate
            from forge.security.permissions import (
                OperationMode, PermissionManager)
            from forge.tools.checkpoint import CheckpointManager

            package = engine.get(args.name)
            permissions = PermissionManager(mode=OperationMode.ASSISTED)
            runtime = GatedAgentRuntime(
                fabric=ModelFabric.from_defaults(),
                policy_gate=PolicyGate(permissions),
                tool_runtime=create_default_runtime(permissions, "."),
                memory_root=".forge/agent-memory", project_root=".",
                checkpoint_manager=CheckpointManager("."))
            # Tests-required specs run the harness's fixed pytest suite
            # through the attached runtime; callers supply no command.
            try:
                report = runtime.run(
                    package, args.requirement, actor=actor)
            except MediationError as exc:
                if as_json:
                    _emit_json({"agent": args.name, "success": False,
                                "code": exc.code, "error": str(exc)})
                else:
                    print(f"Run refused ({exc.code}): {exc}")
                return 1
            if as_json:
                _emit_json(report)
            else:
                print(f"Run {report['run_id']} success={report['success']} "
                      f"model={report['model'] or '-'}")
                print(report["output"][:2000])
            return 0 if report["success"] else 1
    except ValueError as exc:
        if as_json:
            _emit_json({"error": str(exc)})
        else:
            print(f"Agents error: {exc}", file=sys.stderr)
        return 1
    print(f"Unknown agents subcommand: {subcommand!r}", file=sys.stderr)
    return 2


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="forge",
        description="Forge AI software engineering system",
    )

    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("status")

    run_parser = subparsers.add_parser(
        "run",
        help="Run one autonomous task",
        description="Execute a single requirement end to end "
        "(model -> code -> tests -> review -> acceptance). Fails fast with "
        "an actionable diagnosis when no code model is available.",
    )
    run_parser.add_argument("requirement", help="What to implement, fix, or change")
    run_parser.add_argument("--root", default=".",
                            help="Repository root to work in (default: .)")
    run_parser.add_argument("--project", default="forge-ai",
                            help="Project name for reporting (default: forge-ai)")
    run_parser.add_argument("--mode", default="assisted",
                            help="Permission mode: safe|assisted|autonomous|locked "
                            "(default: assisted)")
    run_parser.add_argument("--approve", action="store_true",
                            help="Pre-approve writes (still never overrides DENY)")
    run_parser.add_argument("--max-debug-retries", type=int, default=3)
    run_parser.add_argument("--config", default="",
                            help="Fabric config file (.forge/models.yaml|.json); "
                            "defaults are layered over OLLAMA_URL/OLLAMA_MODEL/"
                            "OPENAI_API_KEY env vars")
    run_parser.add_argument("--ollama-url", default="")
    run_parser.add_argument("--ollama-model", default="")
    run_parser.add_argument("--force", action="store_true",
                            help="Bypass the no-model and assisted-approval "
                            "pre-flight gates (the run then fails honestly "
                            "at the first unsatisfiable step)")
    run_parser.add_argument("--json", action="store_true",
                            help="Emit machine-readable JSON")

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Diagnose why tasks would fail",
        description="Probe the Model Fabric (Ollama reachability, pulled "
        "models, provider credentials) and the local environment, then print "
        "actionable fixes. Exit 0 when tasks can run.",
    )
    doctor_parser.add_argument("--config", default="")
    doctor_parser.add_argument("--ollama-url", default="")
    doctor_parser.add_argument("--ollama-model", default="")
    doctor_parser.add_argument("--offline", action="store_true",
                               help="Skip live endpoint probes")
    doctor_parser.add_argument("--json", action="store_true")

    desktop_parser = subparsers.add_parser(
        "desktop",
        help="Launch the native desktop app",
        description="Open the Forge AI Desktop GUI (Tkinter, no server or "
        "browser needed). Requires a display; on headless machines use "
        "`forge serve` or `forge run` instead.",
    )
    desktop_parser.add_argument(
        "--project", dest="projects", action="append", default=[],
        metavar="ID=ROOT",
        help="Register a project (repeatable). Without any, the app asks "
        "for a folder on first run.",
    )
    desktop_parser.add_argument("--db", default="",
                                help="Control-plane database path.")
    desktop_parser.add_argument("--actor", default="desktop")

    task_parser = subparsers.add_parser("plan")
    task_parser.add_argument("request")

    subparsers.add_parser("analyze")

    # Model Fabric commands
    models_parser = subparsers.add_parser(
        "models",
        help="Inspect the Model Fabric",
        description="Show registered models, providers, capabilities, and "
        "health. Built from the default fabric (local fallback + Ollama + "
        "optional configured providers).",
    )
    models_subparsers = models_parser.add_subparsers(dest="models_subcommand")
    for _sub_name, _sub_help in (
            ("list", "List registered models (default)"),
            ("health", "Show model/provider health"),
            ("providers", "Show registered providers"),
            ("capabilities", "Show the capability vocabulary"),
            ("test", "Run a bounded local self-check")):
        _sub = models_subparsers.add_parser(_sub_name, help=_sub_help)
        # Accept --json after the subcommand too (`models test --json`).
        # SUPPRESS keeps the parent-level flag intact when absent here.
        _sub.add_argument("--json", action="store_true",
                          default=argparse.SUPPRESS,
                          help="Emit machine-readable JSON")
    models_parser.add_argument(
        "--capability",
        "-c",
        help="Only list models that support this capability",
    )
    models_parser.add_argument(
        "--capabilities",
        action="store_true",
        help="Print the supported capability vocabulary instead",
    )
    models_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON",
    )

    # Agent Creation Engine: specs become lifecycle-gated agent packages
    agents_parser = subparsers.add_parser(
        "agents",
        help="Create and manage specialized agents",
        description="First-party Agent Creation Engine: build agents from "
        "structured specs or templates, benchmark them, and move them "
        "through created -> validated -> tested -> enabled. Only enabled "
        "agents run, and only through the mediated runtime.",
    )
    agents_subs = agents_parser.add_subparsers(dest="agents_subcommand")
    _agents_list = agents_subs.add_parser("list",
                                         help="List created agents")
    _agents_common(_agents_list)
    _agents_templates = agents_subs.add_parser(
        "templates", help="List first-party templates")
    _agents_common(_agents_templates)
    create_parser = agents_subs.add_parser(
        "create", help="Create an agent from a template or spec file")
    create_parser.add_argument("--template", default="",
                               help="Template id (see: forge agents "
                               "templates)")
    create_parser.add_argument("--name", default="",
                               help="Agent name ([a-z][a-z0-9_-]{2,48})")
    create_parser.add_argument("--spec", default="",
                               help="JSON spec file (alternative to "
                               "--template)")
    create_parser.add_argument("--set", action="append", default=[],
                               metavar="k=v",
                               help="Template override; section.key=v for "
                               "nested objects (repeatable)")
    _agents_common(create_parser)
    show_parser = agents_subs.add_parser("show",
                                         help="Show one agent package")
    show_parser.add_argument("name")
    _agents_common(show_parser)
    validate_parser = agents_subs.add_parser(
        "validate", help="Validate a created agent")
    validate_parser.add_argument("name")
    _agents_common(validate_parser)
    test_parser = agents_subs.add_parser(
        "test", help="Benchmark a validated agent")
    test_parser.add_argument("name")
    _agents_common(test_parser)
    for _op in ("enable", "pause", "resume", "disable", "retire"):
        _op_parser = agents_subs.add_parser(
            _op, help=f"Move an agent to {_op}d/retired state"
            if _op != "retire" else "Retire an agent (terminal)")
        _op_parser.add_argument("name")
        _agents_common(_op_parser)
    del _op, _op_parser
    grant_parser = agents_subs.add_parser(
        "grant", help="Grant one requested permission (operator only)")
    grant_parser.add_argument("name")
    grant_parser.add_argument("index", type=int,
                              help="Index into the spec's permissions list")
    grant_parser.add_argument("--approver", default="",
                              help="Approver identity (default: --actor); "
                              "never the agent itself")
    grant_parser.add_argument("--expect-json", default="",
                              help="JSON of the reviewed permission entry; "
                              "refuses when the spec moved under it")
    _agents_common(grant_parser)
    revoke_parser = agents_subs.add_parser(
        "revoke", help="Revoke one grant (operator only)")
    revoke_parser.add_argument("name")
    revoke_parser.add_argument("index", type=int,
                               help="Index into the agent's grant list")
    revoke_parser.add_argument("--approver", default="",
                               help="Approver identity (default: --actor); "
                               "never the agent itself")
    _agents_common(revoke_parser)
    update_parser = agents_subs.add_parser(
        "update", help="Replace the spec (resets lifecycle to created)")
    update_parser.add_argument("name")
    update_parser.add_argument("--spec", default="",
                               help="Full replacement spec JSON file")
    update_parser.add_argument("--reason", default="",
                               help="Reason recorded in version history")
    _agents_common(update_parser)
    version_parser = agents_subs.add_parser(
        "version", help="Publish a new agent version")
    version_parser.add_argument("name")
    version_parser.add_argument("--kind", default="patch",
                                choices=["major", "minor", "patch"])
    version_parser.add_argument("--notes", default="")
    _agents_common(version_parser)
    run_parser = agents_subs.add_parser(
        "run", help="Run an enabled agent through the mediated runtime")
    run_parser.add_argument("name")
    run_parser.add_argument("--requirement", required=True)
    _agents_common(run_parser)
    agents_parser.add_argument("--store", default="",
                               help="Agent store path (default: "
                               ".forge/agent-engine.json)")
    agents_parser.add_argument("--actor", default="cli",
                               help="Identity recorded for lifecycle "
                               "transitions")
    agents_parser.add_argument("--json", action="store_true",
                               help="Emit machine-readable JSON")

    # Blender: procedural 3D scenes rendered headlessly
    blender_parser = subparsers.add_parser(
        "blender",
        help="Build and render procedural Blender scenes",
        description="Validate scene specs and render them with headless "
        "Blender (no GUI). Reports honestly when Blender is missing.",
    )
    blender_subs = blender_parser.add_subparsers(dest="blender_subcommand")
    _blender_check = blender_subs.add_parser(
        "check", help="Check Blender availability")
    _blender_check.add_argument("--json", action="store_true",
                                default=argparse.SUPPRESS)
    example_parser = blender_subs.add_parser(
        "example", help="Print a starter scene spec")
    example_parser.add_argument("--json", action="store_true",
                                default=argparse.SUPPRESS)
    example_parser.add_argument("--out", dest="out_file", default="",
                                help="Write the example spec to FILE")
    render_parser = blender_subs.add_parser(
        "render", help="Render a scene spec JSON file")
    render_parser.add_argument("spec", help="Scene spec JSON file")
    render_parser.add_argument("--out", default="renders",
                               help="Output directory (default: renders)")
    render_parser.add_argument("--timeout", type=float, default=600.0,
                               help="Render timeout in seconds")
    render_parser.add_argument("--json", action="store_true",
                               default=argparse.SUPPRESS)
    blender_parser.add_argument("--json", action="store_true",
                                help="Emit machine-readable JSON")

    # Higgsfield: generative media API client
    higgsfield_parser = subparsers.add_parser(
        "higgsfield",
        help="Generate media with the Higgsfield API",
        description="Submit and track Higgsfield image/video generations. "
        "Credentials come from HF_API_KEY_ID / HF_API_KEY_SECRET.",
    )
    higgsfield_subs = higgsfield_parser.add_subparsers(
        dest="higgsfield_subcommand")
    _hf_status = higgsfield_subs.add_parser(
        "status", help="Show configuration")
    _hf_status.add_argument("--json", action="store_true",
                            default=argparse.SUPPRESS)
    submit_parser = higgsfield_subs.add_parser(
        "submit", help="Submit a generation")
    submit_parser.add_argument("--model", default="soul-standard-image",
                               help="Model alias or API path")
    submit_parser.add_argument("--prompt", required=True,
                               help="Generation prompt")
    submit_parser.add_argument("--param", action="append", default=[],
                               metavar="k=v",
                               help="Extra model parameter (repeatable)")
    submit_parser.add_argument("--wait", action="store_true",
                               help="Poll until a terminal state")
    submit_parser.add_argument("--timeout", type=float, default=600.0)
    submit_parser.add_argument("--interval", type=float, default=3.0)
    submit_parser.add_argument("--json", action="store_true",
                               default=argparse.SUPPRESS)
    get_parser = higgsfield_subs.add_parser(
        "get", help="Fetch one status snapshot")
    get_parser.add_argument("request_id")
    get_parser.add_argument("--json", action="store_true",
                            default=argparse.SUPPRESS)
    cancel_parser = higgsfield_subs.add_parser(
        "cancel", help="Cancel a queued request")
    cancel_parser.add_argument("request_id")
    cancel_parser.add_argument("--json", action="store_true",
                               default=argparse.SUPPRESS)
    download_parser = higgsfield_subs.add_parser(
        "download", help="Download a completed output URL")
    download_parser.add_argument("url")
    download_parser.add_argument("--json", action="store_true",
                                 default=argparse.SUPPRESS)
    download_parser.add_argument("--out", default="downloads")
    download_parser.add_argument("--filename", default=None)
    higgsfield_parser.add_argument("--json", action="store_true",
                                   help="Emit machine-readable JSON")
    higgsfield_parser.add_argument("--api-timeout", type=float,
                                   default=30.0)

    # Self-development commands
    subparsers.add_parser("self-analyze")

    improve_parser = subparsers.add_parser("self-improve")
    improve_parser.add_argument(
        "--iterations",
        "-i",
        type=int,
        default=1,
        help="Maximum number of self-improvement iterations to run",
    )

    subparsers.add_parser("self-status")

    serve_parser = subparsers.add_parser(
        "serve",
        help="Run the browser cockpit server (local development)",
        description="Start the Forge cockpit API + web UI. Local-dev "
        "auth only; do not expose to untrusted networks.",
    )
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8000)
    serve_parser.add_argument(
        "--project", dest="projects", action="append", default=[],
        metavar="ID=ROOT",
        help="Register a project (repeatable). Defaults to the "
        "current directory.",
    )
    serve_parser.add_argument("--db", default="",
                             help="Control-plane database path.")

    args = parser.parse_args()

    if args.command == "status":
        supervisor = Supervisor("forge-ai")
        print("Forge AI")
        print("Version: 0.1.0")
        print(f"Project: {supervisor.state.project_name}")
        print(f"Status: {supervisor.state.status.value}")
        print(f"Iteration: {supervisor.state.iteration}")

    elif args.command == "plan":
        supervisor = Supervisor("forge-ai")
        plan = supervisor.create_plan(args.request)
        print("Forge Plan\n")
        for step in plan:
            print(f"{step.id}. {step.description}")

    elif args.command == "run":
        raise SystemExit(_run_task(args))

    elif args.command == "doctor":
        raise SystemExit(_run_doctor(args))

    elif args.command == "desktop":
        from forge.desktop_app.app import launch

        projects: dict[str, str] = {}
        for spec in args.projects:
            name, _, root = spec.partition("=")
            if not name or not root:
                parser.error("--project must look like ID=ROOT")
            projects[name] = root
        try:
            launch(projects or None, db_path=args.db, actor=args.actor)
        except ImportError as exc:
            print(f"The desktop app needs stdlib tkinter: {exc}",
                  file=sys.stderr)
            raise SystemExit(2)
        except Exception as exc:  # Tk raises TclError without a display
            print(f"Cannot open the desktop window: {exc}\n"
                  f"Headless machine? Use `forge serve` (browser) or "
                  f"`forge run` (terminal) instead.", file=sys.stderr)
            raise SystemExit(2)

    elif args.command == "analyze":
        analyzer = ProjectAnalyzer(".")
        analysis = analyzer.analyze()
        print(generate_report(analysis))

    elif args.command == "models":
        _run_models(args)

    elif args.command == "agents":
        raise SystemExit(_run_agents(args))

    elif args.command == "blender":
        raise SystemExit(_run_blender(args))

    elif args.command == "higgsfield":
        raise SystemExit(_run_higgsfield(args))

    elif args.command == "self-analyze":
        analyzer = ForgeSelfAnalyzer(".")
        res = analyzer.analyze()
        findings = res.get("findings", [])
        print("Forge Self Analysis")
        print(f"Root: {res.get('root')}")
        print(f"Total Findings: {len(findings)}")
        print("\nStructured Findings:")
        for f in findings:
            print(
                f"[{f['id']}] [{f['severity'].upper()}] ({f['category']}) "
                f"{f['description']} (Files: {', '.join(f.get('affected_files', []))})"
            )

    elif args.command == "self-improve":
        loop = SelfDevelopmentLoop(".")
        iterations = max(1, args.iterations)
        print(f"Starting Forge Self-Improvement Loop (iterations: {iterations})...")
        results = loop.run(max_iterations=iterations)
        print(f"Completed {len(results)} self-improvement iteration(s).")
        for idx, r in enumerate(results, 1):
            status = "ACCEPTED" if r.accepted else "REJECTED"
            print(f"Iteration {idx}: {status}")
            if r.rejection_reason:
                print(f"  Reason: {r.rejection_reason}")

    elif args.command == "self-status":
        loop = SelfDevelopmentLoop(".")
        st = loop.status()
        print("Forge Self-Development Status")
        print(f"Root: {st['root']}")
        print(f"Current Loop Iteration: {st['iteration_count']}")
        print(f"Total Runs in History: {st['total_runs_in_history']}")
        print(f"Accepted Runs: {st['accepted_runs']}")
        print(f"Rejected Runs: {st['rejected_runs']}")

    elif args.command == "serve":
        from forge.api.server import run as serve

        projects: dict[str, str] = {}
        for spec in args.projects:
            name, _, root = spec.partition("=")
            if not name or not root:
                parser.error("--project must look like ID=ROOT")
            projects[name] = root
        serve(host=args.host, port=args.port,
              projects=projects or None, db_path=args.db)


    else:
        parser.print_help()


if __name__ == "__main__":
    main()
