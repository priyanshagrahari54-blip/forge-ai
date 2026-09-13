from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sys

from pathlib import Path

from forge.core.portability import MINIMUM_PYTHON, MINIMUM_PYTHON_STRING
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


def _open_memory(args):
    """Open the long-term memory store for CLI use (default local db)."""
    from forge.control.db import Database
    from forge.memory import LongTermMemory

    db_path = getattr(args, "db", "") or ".forge/memory.db"
    project = getattr(args, "project", "") or os.path.basename(
        os.getcwd().rstrip("/")) or "default"
    return LongTermMemory(Database(db_path), project=project)


def _memory_type_or_none(args):
    from forge.memory import MemoryType

    raw = getattr(args, "type", "") or getattr(args, "memory_type", "")
    if not raw:
        return None
    return MemoryType.parse(raw)


def _run_memory(args) -> int:
    """CLI entry for the long-term memory store."""
    from forge.memory import MemoryType

    subcommand = getattr(args, "memory_subcommand", "list") or "list"

    try:
        if subcommand == "search":
            memory = _open_memory(args)
            results = memory.search(
                args.query,
                memory_type=_memory_type_or_none(args),
                k=getattr(args, "limit", 10),
            )
            if getattr(args, "json", False):
                _emit_json({"query": args.query,
                            "results": [r.to_dict() for r in results]})
            else:
                print(f"Memory search: {args.query!r}")
                for result in results:
                    record = result.record
                    print(f"  {record.id[:8]} [{record.memory_type}] "
                          f"score={result.score:.3f} "
                          f"conf={record.confidence:.2f} "
                          f"imp={record.importance:.2f}")
                    print(f"    {(record.summary or record.content)[:160]}")
                if not results:
                    print("  (no matches)")
            return 0

        if subcommand == "stats":
            memory = _open_memory(args)
            stats = memory.stats()
            if getattr(args, "json", False):
                _emit_json(stats)
            else:
                print("Forge Memory Stats")
                print(f"  project: {stats['project']}")
                print(f"  total: {stats['total']}  "
                      f"active={stats['active']} deleted={stats['deleted']} "
                      f"expired={stats['expired']} "
                      f"superseded={stats['superseded']}")
                print(f"  redactions: {stats['redactions']}  "
                      f"provenance events: {stats['provenance_events']}")
                print("  by type:")
                for memory_type, count in sorted(stats["by_type"].items()):
                    print(f"    {memory_type}: {count}")
            return 0

        if subcommand == "add":
            memory = _open_memory(args)
            result = memory.remember(
                _memory_type_or_none(args) or MemoryType.PROJECT,
                args.content,
                source=getattr(args, "source", "forge-cli") or "forge-cli",
                importance=getattr(args, "importance", 0.5),
                confidence=getattr(args, "confidence", 0.5),
                retention=getattr(args, "retention", "") or None,
            )
            if getattr(args, "json", False):
                _emit_json(result.to_dict())
                return 0 if result.stored else 1
            if result.stored:
                print(f"Stored {result.record.id} "
                      f"[{result.record.memory_type}]")
            else:
                print(f"Not stored ({result.status}): {result.reason}")
                return 1
            return 0

        if subcommand == "correct":
            memory = _open_memory(args)
            try:
                record = memory.correct(
                    args.memory_id, args.content,
                    source=getattr(args, "source", "forge-cli")
                    or "forge-cli",
                    reason="corrected via forge CLI")
            except Exception as exc:
                print(f"Correction failed: {exc}", file=sys.stderr)
                return 1
            if getattr(args, "json", False):
                _emit_json(record.to_dict())
            else:
                print(f"Corrected {args.memory_id} -> {record.id} "
                      f"(version {record.version})")
            return 0

        if subcommand == "delete":
            memory = _open_memory(args)
            deleted = memory.delete(
                args.memory_id, source="forge-cli",
                reason="deleted via forge CLI")
            if getattr(args, "json", False):
                _emit_json({"deleted": deleted, "id": args.memory_id})
                return 0 if deleted else 1
            print(f"{'Deleted' if deleted else 'Not found'}: {args.memory_id}")
            return 0 if deleted else 1

        # default: list
        memory = _open_memory(args)
        records = memory.list(
            memory_type=_memory_type_or_none(args),
            limit=getattr(args, "limit", 20),
        )
        if getattr(args, "json", False):
            _emit_json({"records": [r.to_dict() for r in records]})
        else:
            print("Forge Memory (newest first)")
            for record in records:
                print(f"  {record.id[:8]} [{record.memory_type}] "
                      f"v{record.version} conf={record.confidence:.2f} "
                      f"imp={record.importance:.2f} "
                      f"retention={record.retention} src={record.source}")
                print(f"    {(record.summary or record.content)[:200]}")
            if not records:
                print("  (empty)")
        return 0
    except (ValueError, KeyError) as exc:
        print(f"memory: {exc}", file=sys.stderr)
        return 2


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
        "python_ok": sys.version_info >= MINIMUM_PYTHON,
        "python_minimum": MINIMUM_PYTHON_STRING,
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
    python_verdict = (
        "ok (>=%s)" % MINIMUM_PYTHON_STRING if environment["python_ok"]
        else "TOO OLD - Forge needs >=%s" % MINIMUM_PYTHON_STRING)
    print(f"  Python: {environment['python']} {python_verdict}")
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
# -- Forge Server (A81) CLI -------------------------------------------------

SERVER_DEFAULT_HOST = "127.0.0.1"
SERVER_DEFAULT_PORT = 8300
SERVER_DEFAULT_DB = ".forge/server/server.db"


def _server_parse_projects(specs, parser) -> dict:
    projects: dict = {}
    for spec in specs or []:
        name, _, root = spec.partition("=")
        if not name or not root:
            parser.error("--project must look like ID=ROOT")
        projects[name] = root
    return projects


def _server_db_path(args) -> str:
    return getattr(args, "db", "") or SERVER_DEFAULT_DB


def _server_base_url(args) -> str:
    url = getattr(args, "url", "") or ""
    if url:
        return url.rstrip("/")
    host = getattr(args, "host", "") or SERVER_DEFAULT_HOST
    port = int(getattr(args, "port", 0) or SERVER_DEFAULT_PORT)
    return f"http://{host}:{port}/api/v1"


def _server_token_file(args) -> str:
    db = Path(_server_db_path(args))
    return str(db.parent / "token")


def _server_resolve_token(args) -> str:
    token = getattr(args, "token", "") or ""
    if token:
        return token
    token = os.environ.get("FORGE_SERVER_TOKEN", "")
    if token:
        return token
    path = Path(_server_token_file(args))
    try:
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    return ""


def _server_http_get(url: str, token: str, timeout: float = 5.0):
    """GET a Forge Server endpoint; returns (status_code, payload)."""
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, method="GET")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            try:
                return int(response.status), json.loads(body)
            except ValueError:
                return int(response.status), {"raw": body}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return int(exc.code), json.loads(body)
        except ValueError:
            return int(exc.code), {"raw": body}
    except Exception as exc:  # URLError, timeout, connection refused
        return 0, {"error": str(exc)}


def _run_server_start(args, parser) -> int:
    import secrets as _secrets

    from forge.server import ForgeServer, ServerConfig

    projects = _server_parse_projects(
        getattr(args, "projects", []), parser)
    if not projects:
        root = Path.cwd()
        projects = {root.name or "forge": str(root)}
    token = getattr(args, "token", "") or ""
    generated = False
    if not token:
        token = _secrets.token_urlsafe(32)
        generated = True
    db_path = _server_db_path(args)
    if generated:
        # Persist the bootstrap token next to the database so
        # `forge server status`/`health` can find it. Best effort:
        # POSIX permissions are tightened where supported (not Windows).
        token_path = Path(_server_token_file(args))
        try:
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.write_text(token, encoding="utf-8")
            try:
                os.chmod(str(token_path), 0o600)
            except (OSError, AttributeError):
                pass
        except OSError as exc:
            print(f"warning: could not write token file: {exc}",
                  file=sys.stderr)
    profile = getattr(args, "profile", "") or "assisted"
    if profile not in ("safe", "assisted", "autonomous", "locked"):
        parser.error("--profile must be safe|assisted|autonomous|locked")
    config = ServerConfig(
        db_path=db_path,
        host=getattr(args, "host", "") or SERVER_DEFAULT_HOST,
        port=int(getattr(args, "port", 0) or SERVER_DEFAULT_PORT),
        projects=projects,
        bootstrap_token=token,
        profile=profile,
        max_workers=max(1, int(getattr(args, "workers", 0) or 4)),
    )
    server = ForgeServer(config)
    print("Forge Server bootstrap token (admin):")
    print(f"  {token}")
    print(f"Token file: {_server_token_file(args)}")
    print("Keep it private; FORGE_SERVER_TOKEN overrides it for clients.")
    server.run_uvicorn()
    return 0


def _run_server_status(args) -> int:
    base = _server_base_url(args)
    token = _server_resolve_token(args)
    status_code, payload = _server_http_get(f"{base}/status", token)
    if getattr(args, "json", False):
        _emit_json({"reachable": status_code != 0,
                    "http_status": status_code, "payload": payload})
        return 0 if status_code == 200 else 1
    if status_code == 0:
        print(f"Forge Server is not running at {base}")
        print(f"  ({payload.get('error', 'unreachable')})")
        return 1
    if status_code == 401:
        print(f"Forge Server at {base} refused the credentials.")
        print("Pass --token, set FORGE_SERVER_TOKEN, or run from the "
              "same directory as the server's --db (token file).")
        return 1
    if status_code != 200:
        print(f"Forge Server at {base} returned HTTP {status_code}.")
        return 1
    workers = payload.get("workers", {})
    queue = payload.get("queue", {})
    tasks = payload.get("tasks", {})
    print("Forge Server: running")
    print(f"  endpoint:  {base.rsplit('/api', 1)[0]}")
    print(f"  version:   {payload.get('version', '?')} "
          f"(boot {payload.get('boot_id', '?')})")
    print(f"  uptime:    {float(payload.get('uptime_seconds', 0)):.1f}s  "
          f"profile: {payload.get('profile', '?')}")
    print(f"  workers:   {workers.get('busy', 0)}/{workers.get('max', 0)} "
          f"busy (alive={workers.get('alive', False)})")
    print(f"  queue:     {queue.get('depth', 0)} queued, "
          f"{queue.get('leased', 0)} leased")
    nonzero = {key: value for key, value in tasks.items() if value}
    print(f"  tasks:     {nonzero or 'none'}")
    print(f"  projects:  {payload.get('projects', 0)} registered, "
          f"{payload.get('active_sessions', 0)} active session(s), "
          f"{payload.get('pending_approvals', 0)} pending approval(s)")
    return 0


def _run_server_health(args) -> int:
    base = _server_base_url(args)
    token = _server_resolve_token(args)
    status_code, payload = _server_http_get(f"{base}/health", token)
    if getattr(args, "json", False):
        _emit_json({"reachable": status_code != 0,
                    "http_status": status_code, "payload": payload})
        return 0 if status_code == 200 else 1
    if status_code == 0:
        print(f"Forge Server is not running at {base}")
        print(f"  ({payload.get('error', 'unreachable')})")
        return 1
    if status_code != 200:
        print(f"Forge Server at {base} returned HTTP {status_code}.")
        if status_code == 401:
            print("Pass --token or set FORGE_SERVER_TOKEN.")
        return 1
    overall = payload.get("status", "unknown")
    print(f"Forge Server health: {overall.upper()}")
    server_info = payload.get("server", {})
    print(f"  version:   {server_info.get('version', '?')} "
          f"(boot {server_info.get('boot_id', '?')})")
    print(f"  uptime:    {float(server_info.get('uptime_seconds', 0)):.1f}s")
    print(f"  auth mode: {payload.get('auth_mode', '?')}  "
          f"profile: {payload.get('profile', '?')}")
    for name, component in sorted(
            (payload.get("components") or {}).items()):
        state = component if isinstance(component, dict) else {}
        mark = "ok" if state.get("ok", True) else "FAIL"
        details = ", ".join(
            f"{key}={value}" for key, value in sorted(state.items())
            if key != "ok" and value is not None)
        print(f"  {name:<10} {mark}{('  ' + details) if details else ''}")
    for warning in payload.get("warnings", []):
        print(f"  warning: {warning}")
    return 0 if overall in ("ok", "degraded") else 1


def _run_server(args, parser) -> int:
    subcommand = getattr(args, "server_subcommand", "") or "start"
    if subcommand == "start":
        return _run_server_start(args, parser)
    if subcommand == "status":
        return _run_server_status(args)
    if subcommand == "health":
        return _run_server_health(args)
    parser.error(f"Unknown server subcommand: {subcommand!r}")
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
    # Long-term memory
    memory_parser = subparsers.add_parser(
        "memory",
        help="Inspect and manage long-term memory",
        description="Project-scoped durable memory across sessions and tasks. "
        "Secrets are redacted before storage; retrieval is relevance-ranked. "
        "Defaults to the local database .forge/memory.db.",
    )
    memory_subs = memory_parser.add_subparsers(dest="memory_subcommand")
    memory_subs.add_parser("list", help="List recent memories (default)")

    _mem_search = memory_subs.add_parser(
        "search", help="Search memories by relevance")
    _mem_search.add_argument("query", help="Free-text query")

    memory_subs.add_parser("stats", help="Show memory statistics")

    _mem_add = memory_subs.add_parser("add", help="Store a new memory item")
    _mem_add.add_argument("content", help="Text to remember")
    _mem_add.add_argument("--source", default="",
                          help="Who/what produced this memory")
    _mem_add.add_argument("--importance", type=float, default=0.5,
                          help="Importance 0..1 (default 0.5)")
    _mem_add.add_argument("--confidence", type=float, default=0.5,
                          help="Confidence 0..1 (default 0.5)")
    _mem_add.add_argument(
        "--retention", default="",
        help="Retention policy: ephemeral|session|task|project|persistent")

    _mem_correct = memory_subs.add_parser(
        "correct", help="Correct an existing memory item")
    _mem_correct.add_argument("memory_id", help="Memory id to correct")
    _mem_correct.add_argument("content", help="Corrected text")
    _mem_correct.add_argument("--source", default="")

    _mem_delete = memory_subs.add_parser("delete", help="Delete a memory item")
    _mem_delete.add_argument("memory_id", help="Memory id to delete")

    def _add_memory_common(sub):
        for flag, kwargs in (
                ("--db", {"default": "", "help":
                          "SQLite database path (default .forge/memory.db)"}),
                ("--project", {"default": "", "help":
                               "Project id (default: current directory name)"}),
                ("--type", {"default": "", "help":
                            "Filter by memory type"}),
                ("--limit", {"type": int, "default": 20,
                             "help": "Maximum number of results"}),
                ("--json", {"action": "store_true",
                            "default": argparse.SUPPRESS,
                            "help": "Emit machine-readable JSON"})):
            sub.add_argument(flag, **kwargs)

    memory_parser.add_argument("--db", default="",
                               help="SQLite database path")
    memory_parser.add_argument("--project", default="", help="Project id")
    memory_parser.add_argument("--type", default="",
                               help="Filter by memory type")
    memory_parser.add_argument("--limit", type=int, default=20,
                               help="Maximum number of results")
    memory_parser.add_argument("--json", action="store_true",
                               help="Emit machine-readable JSON")
    for _sub in memory_subs.choices.values():
        _add_memory_common(_sub)

    # Specification-driven agent engine (A82). It registers as
    # "agent-engine" rather than "agents" because the merged Forge Server
    # work already owns the "agents" name; two parsers claiming the same
    # name make every CLI entry point die with "conflicting subparser".
    from forge.agents.engine.cli import build_parser as build_agents_parser

    build_agents_parser(subparsers, command="agent-engine")

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

    # Forge Server (A81): standalone task backend
    server_parser = subparsers.add_parser(
        "server",
        help="Run and manage the standalone Forge Server",
        description="Start the Forge Server backend (task queue, "
        "background workers, persistent events, reconnect recovery), or "
        "query a running one with the status/health subcommands. "
        "Local-dev auth only; do not expose to untrusted networks.",
    )
    server_parser.add_argument(
        "--host", default="",
        help=f"Bind address (default: {SERVER_DEFAULT_HOST}).")
    server_parser.add_argument(
        "--port", type=int, default=0,
        help=f"Bind port (default: {SERVER_DEFAULT_PORT}).")
    server_parser.add_argument(
        "--db", default="",
        help=f"Server database path (default: {SERVER_DEFAULT_DB}).")
    server_parser.add_argument(
        "--project", dest="projects", action="append", default=[],
        metavar="ID=ROOT",
        help="Register a project (repeatable). Defaults to the "
        "current directory.")
    server_parser.add_argument(
        "--workers", type=int, default=0,
        help="Background worker threads (default: 4).")
    server_parser.add_argument(
        "--profile", default="",
        help="A33 permission profile: safe|assisted|autonomous|locked "
        "(default: assisted).")
    server_parser.add_argument(
        "--token", default="",
        help="Bootstrap admin token (generated and stored next to the "
        "database when omitted).")
    server_subs = server_parser.add_subparsers(dest="server_subcommand")
    for _name, _help in (
            ("start", "Run the server (default when no subcommand)"),
            ("status", "Show live status of a running server"),
            ("health", "Show the health report of a running server")):
        _sub = server_subs.add_parser(_name, help=_help)
        # SUPPRESS keeps parent-level values intact when the flag is
        # given before the subcommand (`forge server --port X status`).
        _sub.add_argument("--host", default=argparse.SUPPRESS)
        _sub.add_argument("--port", type=int, default=argparse.SUPPRESS)
        _sub.add_argument("--db", default=argparse.SUPPRESS,
                          help="Server database path (locates the "
                          "token file).")
        _sub.add_argument("--token", default=argparse.SUPPRESS,
                          help="Bearer token (or set FORGE_SERVER_TOKEN).")
        if _name == "start":
            _sub.add_argument("--project", dest="projects", action="append",
                              default=argparse.SUPPRESS, metavar="ID=ROOT")
            _sub.add_argument("--workers", type=int,
                              default=argparse.SUPPRESS)
            _sub.add_argument("--profile", default=argparse.SUPPRESS)
        else:
            _sub.add_argument("--url", default="",
                              help="Full API base URL "
                              "(default: built from --host/--port).")
            _sub.add_argument("--json", action="store_true",
                              help="Emit machine-readable JSON")

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

    elif args.command == "agents":
        raise SystemExit(_run_agents(args))
    elif args.command == "memory":
        raise SystemExit(_run_memory(args))

    elif args.command == "agent-engine":
        from forge.agents.engine.cli import run_agents_cli

        raise SystemExit(run_agents_cli(args))

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

    elif args.command == "server":
        raise SystemExit(_run_server(args, parser))

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
