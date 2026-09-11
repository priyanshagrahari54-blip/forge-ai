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


def _run_research(args) -> int:
    """CLI entry for the secure research engine (``forge research``)."""
    from forge.research.secure_engine import SecureResearchEngine

    root = getattr(args, "root", ".") or "."
    subcommand = getattr(args, "research_subcommand", "status") or "status"
    as_json = bool(getattr(args, "json", False))

    if subcommand == "cache-clear":
        engine = SecureResearchEngine(root, build_intelligence=False)
        removed = engine.cache.clear()
        if as_json:
            _emit_json({"cleared": removed})
        else:
            print(f"Cleared {removed} cached research entr"
                  f"{'y' if removed == 1 else 'ies'}.")
        return 0

    if subcommand == "plan":
        engine = SecureResearchEngine(root, build_intelligence=False)
        plan = engine.plan(args.question, allow_web=not args.no_web)
        if as_json:
            _emit_json(plan.to_dict())
        else:
            print(f"Intent: {plan.intent}")
            print(f"Terms: {', '.join(plan.terms)}")
            print(f"Identifiers: {', '.join(plan.identifiers) or '-'}")
            print(f"Libraries: {', '.join(plan.libraries) or '-'}")
            print(f"Sources: {' -> '.join(plan.source_order)}")
            print("Sub-queries:")
            for query in plan.sub_queries:
                print(f"  - {query}")
        return 0

    if subcommand == "query":
        engine = SecureResearchEngine(root)
        report = engine.research(
            args.question,
            allow_web=not args.no_web,
            sources=tuple(args.sources or ()),
            user_notes=tuple(args.note or ()),
            user_files=tuple(args.file or ()),
            allow_model_knowledge=bool(args.allow_model_knowledge) or None,
            max_results=args.limit,
            use_cache=not args.no_cache,
        )
        if as_json:
            _emit_json(report)
        else:
            print(f"Question: {report['question']}")
            print(f"Intent: {report['plan']['intent']}   "
                  f"Confidence (coverage heuristic): {report['confidence']}")
            print()
            print(report["answer"])
            print()
            counts = report["provenance_counts"]
            print("Provenance: " + ", ".join(
                f"{key}={value}" for key, value in counts.items()))
            print("Sources:")
            for outcome in report["sources"]:
                line = (f"  {outcome['source']:<20} {outcome['state']:<14} "
                        f"results={outcome['result_count']}")
                if outcome.get("error"):
                    line += f"  ({outcome['error'][:80]})"
                print(line)
            if report["results"]:
                print("Results:")
                for index, item in enumerate(report["results"], start=1):
                    flag = "" if item["verified"] else " [UNVERIFIED]"
                    print(f"  [{index}] {item['provenance']}{flag} "
                          f"{item['cite']}")
                    print(f"      {item['title'][:100]}")
                    print(f"      {item['snippet'][:200]}")
            if report["web_failed"]:
                print("WARNING: web research failed; nothing was invented "
                      "to fill the gap.")
        return 0 if (report["results"] or not report["web_failed"]) else 1

    engine = SecureResearchEngine(root, build_intelligence=False)
    status = engine.status()
    if as_json:
        _emit_json(status)
    else:
        print("Forge Research Engine")
        print(f"Root: {status['root']}")
        print(f"Web research: {'enabled' if status['config']['web_enabled'] else 'disabled'}"
              f" (https_only={status['security']['https_only']}, "
              f"timeout={status['security']['timeout_seconds']}s, "
              f"max_bytes={status['security']['max_bytes']}, "
              f"max_redirects={status['security']['max_redirects']})")
        print(f"Allowed hosts: {len(status['security']['host_allowlist'])}")
        print(f"Model knowledge: configured={status['model_knowledge']['configured']} "
              f"allowed={status['model_knowledge']['allowed_by_config']} — "
              f"{status['model_knowledge']['policy']}")
        print("Sources:")
        for name, info in status["sources"].items():
            print(f"  {name:<20} available={info['available']!s:<5} "
                  f"provenance={info['provenance']}")
        cache = status["cache"]
        print(f"Cache: {cache['entries']} entries, ttl={cache['ttl_seconds']}s, "
              f"dir={cache['directory'] or '(memory only)'}")
        if status["config"]["rejected"]:
            print("Rejected config entries:")
            for item in status["config"]["rejected"]:
                print(f"  - {item}")
        print("Usage: forge research query \"<question>\" [--no-web] [--json]")
    return 0


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

    research_parser = subparsers.add_parser(
        "research",
        help="Secure, provenance-tracked research over project + web",
        description="Research technical topics, APIs, docs, libraries, "
        "errors and project questions. Every result is labeled "
        "LOCAL_SOURCE / REAL_WEB_RESULT / USER_PROVIDED / MODEL_KNOWLEDGE; "
        "web access is HTTPS-only and SSRF-guarded; failed web research "
        "is reported, never replaced by model knowledge.",
    )
    research_parser.add_argument("--root", default=".",
                                 help="Project root (default: cwd)")
    research_parser.add_argument("--json", action="store_true",
                                 help="Emit machine-readable JSON")
    research_subs = research_parser.add_subparsers(dest="research_subcommand")
    research_subs.add_parser("status", help="Show sources, policy, cache")
    research_subs.add_parser("cache-clear", help="Clear the research cache")
    research_plan_parser = research_subs.add_parser(
        "plan", help="Show the query plan without running sources")
    research_plan_parser.add_argument("question")
    research_plan_parser.add_argument("--no-web", action="store_true")
    research_query_parser = research_subs.add_parser(
        "query", help="Run a research query")
    research_query_parser.add_argument("question")
    research_query_parser.add_argument("--no-web", action="store_true",
                                       help="Local sources only")
    research_query_parser.add_argument(
        "--source", dest="sources", action="append", default=[],
        help="Restrict to a source (repeatable): project_files, "
             "local_docs, repository_metadata, official_docs, "
             "configured_web, user_provided")
    research_query_parser.add_argument(
        "--note", action="append", default=[],
        help="User-provided note (repeatable); labeled USER_PROVIDED")
    research_query_parser.add_argument(
        "--file", action="append", default=[],
        help="User-provided file inside the project (repeatable)")
    research_query_parser.add_argument(
        "--allow-model-knowledge", action="store_true",
        help="Also include clearly-labeled MODEL_KNOWLEDGE (opt-in)")
    research_query_parser.add_argument("--limit", type=int, default=None)
    research_query_parser.add_argument("--no-cache", action="store_true")
    for _sub in (research_plan_parser, research_query_parser):
        _sub.add_argument("--json", action="store_true",
                          default=argparse.SUPPRESS,
                          help="Emit machine-readable JSON")

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
            raise SystemExit(2) from None
        except Exception as exc:  # Tk raises TclError without a display
            print(f"Cannot open the desktop window: {exc}\n"
                  f"Headless machine? Use `forge serve` (browser) or "
                  f"`forge run` (terminal) instead.", file=sys.stderr)
            raise SystemExit(2) from None

    elif args.command == "analyze":
        analyzer = ProjectAnalyzer(".")
        analysis = analyzer.analyze()
        print(generate_report(analysis))

    elif args.command == "models":
        _run_models(args)

    elif args.command == "blender":
        raise SystemExit(_run_blender(args))

    elif args.command == "higgsfield":
        raise SystemExit(_run_higgsfield(args))

    elif args.command == "research":
        raise SystemExit(_run_research(args))

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
