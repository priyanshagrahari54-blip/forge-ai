from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sys
import threading
import time
import uuid

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


def _assistant_stacks(args):
    """Build the standalone A84 assistant stack (thin-client local state).

    No control plane here: task submission is intentionally absent, so code
    requests answer as *proposals* through the fabric and say so. The db path
    mirrors the memory CLI convention (``--db``, default ``.forge/assistant.db``).
    """
    from forge.assistant.behavior import AssistantBehavior
    from forge.assistant.context import ContextEngine
    from forge.assistant.core import AssistantCore
    from forge.assistant.memory import PersonalMemoryService
    from forge.assistant.personalization import PreferenceProfile
    from forge.assistant.sessions import SessionLedger
    from forge.control.db import Database
    from forge.improvement.proposals import ImprovementEngine
    from forge.learning.operational import OperationalLedger
    from forge.learning.preferences import PreferenceObserver
    from forge.learning.routing import RoutingPriors
    from forge.memory import LongTermMemory
    from forge.models.fabric import ModelFabric
    from forge.models.teams import ModelTeam
    from forge.patterns.graph import PatternGraph
    from forge.prompt_intelligence.pipeline import PromptIntelligence
    from forge.prompt_intelligence.strategies import PromptStrategyLedger
    from forge.prompt_intelligence.versions import PromptLedger
    from forge.research.deep import DeepResearchEngine
    from forge.tools.intelligence.planner import ToolPlanner
    from forge.tools.intelligence.registry import builtin_registry
    from forge.tools.intelligence.verification import ToolVerifier
    from forge.verification.critic import Critic
    from forge.verification.loop import CritiqueLoop
    from forge.verification.verifier import EvidenceVerifier

    project = getattr(args, "project", "") or os.environ.get(
        "FORGE_ASSISTANT_PROJECT", "") or os.path.basename(
        os.getcwd().rstrip("/")) or "personal"
    db = Database(getattr(args, "db", "") or ".forge/assistant.db")
    ledger = SessionLedger(db)
    patterns = PatternGraph(db)
    engine = LongTermMemory(db, project=project)
    observer = PreferenceObserver()
    memory = PersonalMemoryService(engine, pattern_graph=patterns,
                                    preference_observer=observer)
    fabric = ModelFabric.from_defaults()
    core = AssistantCore(
        ledger=ledger, memory_service=memory,
        context_engine=ContextEngine(memory_service=memory, ledger=ledger),
        prompt_intelligence=PromptIntelligence(
            strategy_ledger=PromptStrategyLedger(db)),
        prompt_ledger=PromptLedger(db),
        behavior=AssistantBehavior(),
        tool_planner=ToolPlanner(builtin_registry()),
        deep_research=DeepResearchEngine(root=os.getcwd()),
        critique_loop=CritiqueLoop(Critic(fabric=fabric), EvidenceVerifier()),
        model_team=ModelTeam(fabric), fabric=fabric,
        improvement_engine=ImprovementEngine(
            operational=OperationalLedger(db),
            strategy_ledger=PromptStrategyLedger(db),
            routing_priors=RoutingPriors(db), fabric=fabric),
        profile_provider=lambda: PreferenceProfile.from_memory(
            engine, project=project),
        default_project=project)
    return core, memory, engine, ledger, patterns, db, project


def _run_assistant(args) -> int:
    """CLI entry for the A84 personal-assistant plane (standalone mode)."""
    sub = getattr(args, "assistant_subcommand", "status") or "status"
    as_json = getattr(args, "json", False)
    core, memory, engine, ledger, patterns, _db, project = _assistant_stacks(args)

    if sub == "ask":
        session_id = getattr(args, "session", "") or "cli"
        response = core.respond(session_id, args.message, project=project,
                                allow_web=bool(getattr(args, "web", False)))
        payload = response.to_dict()
        if as_json:
            _emit_json(payload)
        else:
            print(payload["text"])
            print(f"\n[channel: {payload['provenance']} | triage: "
                  f"{payload['kind']} | prompt quality: "
                  f"{payload['quality'].get('verdict', 'n/a')} | session: "
                  f"{payload['session_id']}]")
        return 0

    if sub == "sessions":
        rows = ledger.list(limit=getattr(args, "limit", 20))
        if as_json:
            _emit_json([r.to_dict(include_history=False) for r in rows])
        else:
            for row in rows:
                print(f"{row.id}  {row.state:<8} {row.title[:60]}")
            if not rows:
                print("No assistant sessions yet.")
        return 0

    if sub == "memory":
        mem_sub = getattr(args, "assistant_memory_subcommand", "inspect")
        if mem_sub == "search":
            hits = memory.recall(args.query, k=10, project=project)
            rows = [_memory_row_dict(h) for h in hits]
        elif mem_sub == "inspect":
            view = memory.inspect(project=project,
                                  limit=getattr(args, "limit", 20))
            rows = [{"id": e.get("id", ""), "memory_type": e.get("type", ""),
                     "content": (e.get("content") or "")[:200],
                     "importance": e.get("importance", 0.0)}
                    for e in view.get("entries", [])]
        elif mem_sub == "forget":
            done = memory.forget(args.memory_id, project=project)
            rows = {"forgotten": bool(done),
                    "note": "irreversible by design (purge + refs cleaned)"}
            if as_json:
                _emit_json(rows)
            else:
                print("Forgotten." if done else "Nothing matched that id.")
            return 0
        elif mem_sub == "clear":
            try:
                out = memory.clear_long_term(
                    confirm=getattr(args, "confirm", ""), project=project)
            except ValueError as exc:
                out = {"removed": 0, "refused": str(exc)[:300]}
            if as_json:
                _emit_json(out)
            else:
                if out.get("refused"):
                    print(f"Refused: {out['refused']}")
                else:
                    print(f"Cleared: {out.get('removed', 0)} records")
            return 0
        else:
            raise SystemExit("Usage: forge assistant memory "
                             "search|inspect|forget|clear")
        if as_json:
            _emit_json(rows)
        else:
            for row in rows:
                print(f"{row['id'][:16]}  {row['memory_type']:<18} "
                      f"{row['content'][:70]}")
            if not rows:
                print("No matching memories — nothing was stored by default.")
        return 0

    if sub == "tools":
        from forge.tools.intelligence.registry import builtin_registry
        registry = builtin_registry()
        rows = [{"name": d.name, "capability": d.capability,
                 "serves": list(d.serves), "risk": d.risk,
                 "permission": [d.permission_resource,
                                d.permission_operation],
                 "availability": registry.availability(d.name)}
                for d in sorted(registry.all(), key=lambda d: d.name)]
        if as_json:
            _emit_json({"tools": rows,
                        "honesty": "registry descriptions are NOT "
                                   "permissions; the standalone CLI has no "
                                   "execution path at all"})
        else:
            for row in rows:
                print(f"{row['name']:<16} {row['risk']:<8} "
                      f"{row['availability'].get('state','?'):<12} "
                      f"{row['capability']} -> "
                      f"{', '.join(row['serves'])}")
        return 0

    if sub == "scale":
        from forge.models.registry import ModelRegistry  # noqa: F401
        rows = []
        for model in core.fabric.models():
            scale = model.scale
            rows.append({"name": model.name, "provider": model.provider,
                         "model_class": model.model_class or "undeclared",
                         "parameters": scale.to_dict().get(
                             "parameter_count_label", "unknown"),
                         "disclosed": scale.disclosed,
                         "band": scale.band, "moe": scale.is_moe,
                         "available": model.available})
        if as_json:
            _emit_json({"models": rows,
                        "legend": "disclosed=false means NOT DISCLOSED; Forge "
                                  "never invents parameter counts and never "
                                  "claims to host provider weights"})
        else:
            print(f"{'MODEL':<28} {'CLASS':<12} {'PARAMETERS':<14} "
                  f"{'BAND':<9} AVAIL")
            for row in rows:
                print(f"{row['name'][:27]:<28} {row['model_class']:<12} "
                      f"{(row['parameters'] if row['disclosed'] else 'unknown')[:13]:<14} "
                      f"{row['band']:<9} {'yes' if row['available'] else 'no'}")
            print("\nDisclosed scale only — unknown means the provider did "
                  "not say; it never means small.")
        return 0

    if sub == "learning":
        operational = getattr(core.improvement, "operational", None)
        stats = {"operational": {
                     "events": operational.count() if operational else 0},
                 "patterns": patterns.summary()}
        if as_json:
            _emit_json(stats)
        else:
            print("Patterns:", stats["patterns"])
            print("Learning can only propose; it never self-applies.")
        return 0

    if sub == "status":
        from forge.capabilities.reality import capability_snapshot
        snap = capability_snapshot()
        mine = [c for c in snap["capabilities"]
                if c["capability_id"] in (
                    "personal-assistant-core", "personal-memory",
                    "prompt-intelligence", "tool-intelligence",
                    "deep-research", "safe-research-networks",
                    "pattern-graph", "operational-learning",
                    "cross-model-teams", "critic-verifier",
                    "personalization", "long-context", "context-quality",
                    "improvement-loop", "massive-model-routing")]
        if as_json:
            _emit_json({"capabilities": mine, "project": project,
                        "honesty_contract": snap["honesty_contract"]})
        else:
            for row in mine:
                print(f"{row['capability_id']:<26} {row['status']}")
            print("\nLIVE = wired here; READY/ARCHITECTURE = needs the "
                  "infrastructure named in --json details.")
        return 0

    raise SystemExit("Usage: forge assistant ask|sessions|memory|tools|scale|"
                     "learning|status")


def _memory_row_dict(hit):
    record = getattr(hit, "record", hit)
    return {"id": str(getattr(record, "id", "")),
            "memory_type": str(getattr(record, "memory_type", "")),
            "content": str(getattr(record, "content", ""))[:200],
            "importance": round(float(getattr(record, "importance", 0.0)
                                      or 0.0), 3)}


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


def _build_runtime(args):
    """Build the Native Model Runtime from config file / env / CLI overrides.

    The runtime is model *execution infrastructure*: it is separate from the
    Model Fabric (routing) and from the AI Engine (orchestration). Network
    access stays off unless explicitly requested, so these commands are safe
    to run anywhere.
    """
    from forge.core.resource_governor import ResourceGovernor, select_profile
    from forge.runtime.model_runtime import (BUILTIN_BACKENDS, ModelRuntime,
                                             RuntimeConfig)

    config = RuntimeConfig.load(getattr(args, "config", "") or None)
    if getattr(args, "allow_network", False):
        config.allow_network = True
    extra_dirs = list(getattr(args, "model_dir", []) or [])
    if extra_dirs:
        config.model_dirs = tuple(list(config.model_dirs) + extra_dirs)
    backend = getattr(args, "backend", "") or ""
    if backend:
        if backend not in BUILTIN_BACKENDS:
            print(f"Unknown backend {backend!r}; built-in backends: "
                  f"{', '.join(BUILTIN_BACKENDS)}", file=sys.stderr)
            raise SystemExit(2)
        if backend not in config.backends:
            config.backends = tuple(list(config.backends) + [backend])
        config.default_backend = backend
    config.validate()
    # Unified resource governor: explicit --profile > FORGE_RESOURCE_PROFILE
    # > auto-detection. A g560-class device refuses local model loads.
    profile = select_profile(getattr(args, "profile", "") or "")
    governor = ResourceGovernor(profile)
    return ModelRuntime.from_defaults(config, governor=governor)


def _runtime_status_text(status) -> str:
    """Render a runtime status snapshot (never any prompt/response content)."""
    runtime = status["runtime"]
    config = runtime["config"]
    lines = ["Forge Native Model Runtime"]
    lines.append(f"  version: {runtime['version']} "
                 f"({'closed' if runtime['closed'] else 'running'})")
    lines.append(f"  default backend: {config['default_backend']}")
    lines.append(f"  network access: "
                 f"{'enabled' if config['allow_network'] else 'disabled'}")
    lines.append(f"  timeout bound: {config['timeout_seconds']:.1f}s "
                 f"(max {config['max_timeout_seconds']:.1f}s)")
    dirs = ", ".join(config["model_dirs"]) or "(none)"
    lines.append(f"  model dirs: {dirs}")
    lines.append("  backends:")
    for info in status["backends"]:
        lines.append(f"    {info['name']}: kind={info['kind']} "
                     f"available={info['available']} "
                     f"network={info['requires_network']}")
        if info["detail"]:
            lines.append(f"      {info['detail']}")
    lines.append("  health:")
    for item in status["health"]:
        lines.append(f"    {item['backend']}: {item['status']} "
                     f"models={item['models_available']} "
                     f"loaded={item['models_loaded']} "
                     f"gen={item['generations']} fail={item['failures']} "
                     f"timeouts={item['timeouts']}")
        if item["error"]:
            lines.append(f"      error: {item['error']}")
    governor = status.get("governor")
    if governor:
        gprofile = governor["profile"]
        lines.append(
            f"  governor: {gprofile['name']} "
            f"(workers={gprofile['max_workers']}, "
            f"model_loading={'allowed' if gprofile['model_loading_allowed'] else 'DENIED'}, "
            f"network={gprofile['network_policy']})")
    resources = status["resources"]
    lines.append("  resources:")
    lines.append(f"    cpu={resources['cpu_count']} "
                 f"memory={resources['memory_total_mb']}MB "
                 f"available={resources['memory_available_mb']}MB")
    lines.append(f"    python={resources['python_version']} "
                 f"platform={resources['platform']}")
    lines.append(f"    models known={resources['models_known']} "
                 f"loaded={resources['models_loaded']} "
                 f"in-flight={resources['in_flight']}")
    lines.append(f"  models: {status['models']['total']} known, "
                 f"{status['models']['loaded']} loaded")
    return "\n".join(lines)


def _runtime_self_test(runtime, backend: str, as_json: bool,
                       offline: bool) -> int:
    """``forge runtime test`` — verify the runtime's own guarantees.

    This exercises the *real* runtime code paths (routing, bounded timeouts,
    cancellation, streaming, error classification, redaction, config
    handling) against an in-process loopback backend. It is a contract test
    of the execution layer, **not** proof that a real neural model is
    installed or that real inference works — and it says so. A backend that
    cannot serve is reported as such rather than being papered over with a
    synthetic success.
    """
    from forge.runtime import model_runtime as mr

    class _Loopback(mr.ModelBackend):
        """In-process backend used only to exercise runtime guarantees."""

        name = "selftest-loopback"
        kind = mr.BackendKind.CUSTOM.value
        description = "Transient loopback backend for `forge runtime test`."
        local = True
        requires_network = False

        def __init__(self, mode: str = "ok", delay: float = 0.0) -> None:
            self.mode = mode
            self.delay = delay
            self.calls = 0

        def available(self):
            return (True, "Loopback backend for the runtime self-check.")

        def health(self, probe: bool = True) -> "object":
            return mr.RuntimeHealth(
                backend=self.name, kind=self.kind,
                status=mr.RuntimeState.READY.value, checked_at=0.0,
                detail="Loopback backend for the runtime self-check.")

        def list_models(self):
            return [mr.RuntimeModel(
                model_id=mr.RuntimeModel.make_id(self.name, "loopback"),
                name="loopback", backend=self.name, size_bytes=0,
                format="loopback", local=True, loaded=False,
                metadata={"source": "self-test"})]

        def load_model(self, model, token=None):
            model.loaded = True
            return model

        def _delay(self, token) -> None:
            if self.delay <= 0:
                return
            deadline = time.monotonic() + self.delay
            while time.monotonic() < deadline:
                if token is not None:
                    token.raise_if_cancelled()
                time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))

        def generate(self, request, token=None):
            self.calls += 1
            self._delay(token)
            if self.mode == "boom":
                raise mr.BackendUnavailableError("loopback failure")
            if self.mode == "protocol":
                return "not-a-RuntimeResponse"
            if self.mode == "leak":
                raise mr.ModelRuntimeError(
                    "auth failed with api_key=sk-abcdefgh1234567890")
            marker = "selftest-" + request.request_id
            return mr.RuntimeResponse(
                text=marker, success=True, model=request.model,
                input_tokens=0, output_tokens=0)

        def stream(self, request, token=None):
            self.calls += 1
            if self.mode == "boom":
                raise mr.BackendUnavailableError("loopback stream failure")
            for index in range(3):
                if token is not None:
                    token.raise_if_cancelled()
                yield mr.RuntimeChunk(
                    text="c%d " % index, request_id=request.request_id)

    checks = []

    def _check(name, detail, ok, expected=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail,
                       "expected": expected})
        return bool(ok)

    probe = _Loopback()
    runtime.register_backend(probe, replace=True)

    def _req(**kwargs):
        kwargs.setdefault("model", "selftest-loopback:loopback")
        kwargs.setdefault("backend", probe.name)
        kwargs.setdefault("timeout", 5.0)
        return mr.RuntimeRequest(**kwargs)

    # 1. A successful generation returns exactly what the backend produced.
    marker_id = "selftest-fixed-" + uuid.uuid4().hex[:8]
    response = runtime.generate(_req(prompt="ping", request_id=marker_id))
    _check("generation returns backend output verbatim",
           "text={0!r} success={1} error_kind={2!r}".format(
               response.text, response.success, response.error_kind),
           response.success and response.text == "selftest-" + marker_id)

    # 2. generate() never raises for an operational failure.
    boom = _Loopback(mode="boom")
    runtime.register_backend(boom, replace=True)
    failed = runtime.generate(_req(prompt="ping", backend=boom.name))
    _check("backend failure becomes a structured result, not an exception",
           "success={0} error_kind={1!r} finish_reason={2!r}".format(
               failed.success, failed.error_kind, failed.finish_reason),
           not failed.success
           and failed.error_kind == mr.ErrorKind.UNAVAILABLE.value
           and failed.finish_reason == mr.FinishReason.ERROR.value)

    # 3. A backend that breaks the protocol is classified, not swallowed.
    broken = _Loopback(mode="protocol")
    runtime.register_backend(broken, replace=True)
    bad = runtime.generate(_req(prompt="ping", backend=broken.name))
    _check("protocol violation is classified as 'protocol'",
           "error_kind={0!r}".format(bad.error_kind),
           not bad.success and bad.error_kind == mr.ErrorKind.PROTOCOL.value)

    # 4. Secrets in an error message are redacted before they are kept.
    leak = _Loopback(mode="leak")
    runtime.register_backend(leak, replace=True)
    leaked = runtime.generate(_req(prompt="ping", backend=leak.name))
    clean = "sk-abcdefgh1234567890" not in (leaked.error or "")
    _check("secrets in error text are redacted",
           "error={0!r}".format(leaked.error),
           not leaked.success and clean)

    # 5. The timeout bound is enforced against a slow backend.
    slow = _Loopback(delay=5.0)
    runtime.register_backend(slow, replace=True)
    started = time.perf_counter()
    timed_out = runtime.generate(_req(prompt="ping", backend=slow.name,
                                      timeout=0.3))
    elapsed = time.perf_counter() - started
    _check("timeout bound is enforced",
           "timed_out={0} error_kind={1!r} elapsed={2:.2f}s".format(
               timed_out.timed_out, timed_out.error_kind, elapsed),
           not timed_out.success and timed_out.timed_out
           and timed_out.error_kind == mr.ErrorKind.TIMEOUT.value
           and elapsed < 4.5)

    # 6. Cancellation is honoured and reported as cancelled, never timeout.
    cancel_backend = _Loopback(delay=5.0)
    runtime.register_backend(cancel_backend, replace=True)
    request = _req(prompt="ping", backend=cancel_backend.name, timeout=30.0)
    holder = {}
    worker = threading.Thread(
        target=lambda: holder.update(
            {"response": runtime.generate(request)}), daemon=True)
    worker.start()
    time.sleep(0.15)
    cancelled_ok = runtime.cancel(request.request_id)
    worker.join(5.0)
    cancelled = holder.get("response")
    _check("cancellation is reported as cancelled, not timeout",
           "cancel() returned {0}; cancelled={1} timed_out={2} "
           "error_kind={3!r}".format(
               cancelled_ok, cancelled is not None and cancelled.cancelled,
               cancelled is not None and cancelled.timed_out,
               cancelled.error_kind if cancelled else None),
           cancelled_ok and cancelled is not None and cancelled.cancelled
           and not cancelled.timed_out
           and cancelled.error_kind == mr.ErrorKind.CANCELLED.value)

    # 7. A duplicate request id is rejected rather than silently aliased.
    dup_id = "selftest-dup-" + uuid.uuid4().hex[:8]
    blocker = _Loopback(delay=5.0)
    runtime.register_backend(blocker, replace=True)
    blocking = _req(prompt="ping", backend=blocker.name, request_id=dup_id,
                    timeout=30.0)
    box = {}
    thread = threading.Thread(
        target=lambda: box.update({"response": runtime.generate(blocking)}),
        daemon=True)
    thread.start()
    time.sleep(0.15)
    duplicate = runtime.generate(_req(prompt="ping", backend=blocker.name,
                                      request_id=dup_id, timeout=1.0))
    runtime.cancel(dup_id)
    thread.join(5.0)
    _check("duplicate request id is rejected as a conflict",
           "error_kind={0!r}".format(duplicate.error_kind),
           not duplicate.success
           and duplicate.error_kind == mr.ErrorKind.CONFLICT.value)

    # 8. Streaming yields real chunks and a consistent final response.
    streamer = _Loopback()
    runtime.register_backend(streamer, replace=True)
    collected = []
    stream = runtime.stream(_req(prompt="ping", backend=streamer.name))
    for chunk in stream:
        collected.append(chunk.text)
    final = stream.response
    _check("streaming yields chunks and a consistent final response",
           "chunks={0!r} success={1} finish_reason={2!r}".format(
               collected, final.success, final.finish_reason),
           collected == ["c0 ", "c1 ", "c2 "] and final.success
           and final.text == "c0 c1 c2 ")

    # 9. A stream failure raises with a classified error, never a raw type.
    stream_boom = _Loopback(mode="boom")
    runtime.register_backend(stream_boom, replace=True)
    stream_error_kind = ""
    stream_raised = False
    try:
        for _chunk in runtime.stream(_req(prompt="ping",
                                          backend=stream_boom.name)):
            pass
    except mr.ModelRuntimeError as exc:
        stream_raised = True
        kind = getattr(exc, "kind", "")
        stream_error_kind = kind.value if isinstance(kind, mr.ErrorKind) \
            else str(kind)
    _check("stream failure raises a classified runtime error",
           "raised={0} error_kind={1!r}".format(stream_raised,
                                               stream_error_kind),
           stream_raised and stream_error_kind == "unavailable")

    # 10. Backend selection is explicit: an unknown backend is refused.
    unknown_kind = ""
    try:
        runtime.select_backend("selftest-does-not-exist")
    except mr.ModelRuntimeError as exc:
        kind = getattr(exc, "kind", "")
        unknown_kind = kind.value if isinstance(kind, mr.ErrorKind) else ""
    _check("unknown backend names are refused, never guessed",
           "error_kind={0!r}".format(unknown_kind),
           unknown_kind == mr.ErrorKind.NOT_FOUND.value)

    # 11. Config handling rejects non-finite timeouts instead of coercing.
    coerced = True
    try:
        mr.RuntimeConfig().clamp_timeout(float("nan"))
    except ValueError:
        coerced = False
    _check("non-finite timeouts are rejected, not silently clamped",
           "clamp_timeout(nan) raised ValueError={0}".format(not coerced),
           not coerced)

    # 12. Metrics reflect what actually happened during this self-check.
    # Assert on the invariants that matter rather than on a magic request
    # count that drifts whenever a check is added or removed: a real window,
    # both outcomes represented, real latency, and the specific failure
    # kinds this self-check deliberately provoked. Backend outcomes and
    # pre-backend refusals are reported separately, and both are checked.
    metrics = runtime.metrics()
    provoked = {"unavailable", "protocol", "timeout", "cancelled"}
    missing = sorted(provoked - set(metrics["error_kinds"]))
    refused = metrics["refusals"]
    _check("metrics report real recorded outcomes",
           "requests={0} successes={1} failures={2} success_rate={3} "
           "p50={4} error_kinds={5} refused={6} refusals={7}".format(
               metrics["requests"], metrics["successes"],
               metrics["failures"], metrics["success_rate"],
               metrics["latency_ms"]["p50"], metrics["error_kinds"],
               metrics["refused_requests"], refused),
           metrics["requests"] >= 1 and metrics["successes"] >= 2
           and metrics["failures"] >= 5
           and metrics["success_rate"] is not None
           and metrics["latency_ms"]["p50"] is not None
           and not missing
           and refused.get("conflict", 0) >= 1)

    # 13. In-flight bookkeeping is clean: nothing leaks after failures.
    _check("no in-flight requests leak after failures and cancellations",
           "in_flight={0}".format(runtime.in_flight()),
           not runtime.in_flight())

    for name in (probe.name, boom.name, broken.name, leak.name, slow.name,
                 cancel_backend.name, blocker.name, streamer.name,
                 stream_boom.name):
        try:
            runtime.unregister_backend(name)
        except mr.ModelRuntimeError:
            pass

    passed = sum(1 for item in checks if item["ok"])
    total = len(checks)
    # Separately, and honestly: can a *real* backend actually serve?
    real_ready = []
    real_detail = []
    for item in runtime.health(backend, probe=not offline):
        if item.backend.startswith("selftest-"):
            continue
        real_detail.append("{0}={1}".format(item.backend, item.status))
        if item.status == mr.RuntimeState.READY.value:
            real_ready.append(item.backend)

    if as_json:
        _emit_json({"checks": checks, "passed": passed, "total": total,
                    "ready_backends": real_ready,
                    "backend_states": real_detail})
        return 0 if passed == total else 1

    print("Runtime self-check")
    for item in checks:
        print("  [{0}] {1}".format("PASS" if item["ok"] else "FAIL",
                                   item["name"]))
        if not item["ok"]:
            print("        observed: {0}".format(item["detail"]))
    print("  {0}/{1} runtime contract checks passed".format(passed, total))
    print("")
    print("  Real inference backends: "
          + (", ".join(real_detail) if real_detail else "(none registered)"))
    if real_ready:
        print("  READY to serve real models: " + ", ".join(real_ready))
    else:
        print("  No backend can serve a real model right now. The checks "
              "above verify the runtime's guarantees using a loopback "
              "backend; they do not prove real inference works. Configure a "
              "serving backend or model_dirs and re-run.")
    return 0 if passed == total else 1


def _run_runtime(args) -> int:
    """``forge runtime`` — model execution infrastructure inspection."""
    from forge.runtime.model_runtime import ModelRuntimeError

    subcommand = getattr(args, "runtime_subcommand", "status") or "status"
    as_json = bool(getattr(args, "json", False))
    try:
        runtime = _build_runtime(args)
    except ValueError as exc:
        print(f"Runtime configuration error: {exc}", file=sys.stderr)
        return 2
    backend = getattr(args, "backend", "") or ""

    if subcommand == "models":
        if getattr(args, "discover", True):
            try:
                runtime.discover(backend)
            except ModelRuntimeError as exc:
                print(f"Discovery failed: {exc}", file=sys.stderr)
        models = runtime.models(backend)
        if as_json:
            _emit_json({"models": [model.to_dict() for model in models],
                        "discovery": runtime.discover(backend, refresh=False)})
            return 0
        print("Runtime models")
        if not models:
            print("  (none discovered - configure runtime.model_dirs or "
                  "enable a serving backend)")
        for model in models:
            print(f"  {model.model_id}")
            print(f"    backend={model.backend} format={model.format} "
                  f"size={model.size_bytes} loaded={model.loaded}")
            metadata = {key: value for key, value in model.metadata.items()
                        if key not in ("source",)}
            if metadata:
                print(f"    metadata={metadata}")
        return 0

    if subcommand == "health":
        probe = not getattr(args, "offline", False)
        items = [item.to_dict() for item in runtime.health(backend,
                                                           probe=probe)]
        if as_json:
            _emit_json({"health": items})
            return 0 if any(item["status"] == "ready" for item in items) else 1
        print("Runtime health")
        for item in items:
            print(f"  {item['backend']}: {item['status']} "
                  f"(probed={item['probed']} "
                  f"latency={item['latency_ms']:.1f}ms)")
            if item["detail"]:
                print(f"    {item['detail']}")
            if item["error"]:
                print(f"    error: {item['error']}")
        ready = [item["backend"] for item in items
                 if item["status"] == "ready"]
        if ready:
            print(f"READY - backends that can run inference: "
                  f"{', '.join(ready)}")
        else:
            print("NOT READY - no registered backend can run inference "
                  "right now. The runtime reports this instead of "
                  "fabricating output; enable a backend that can serve a "
                  "model (see `forge runtime backends`).")
        return 0 if ready else 1

    if subcommand == "backends":
        infos = [info.to_dict() for info in runtime.backends()]
        if as_json:
            _emit_json({"backends": infos})
            return 0
        print("Runtime backends")
        for info in infos:
            print(f"  {info['name']}: kind={info['kind']} local={info['local']} "
                  f"network={info['requires_network']} "
                  f"available={info['available']}")
            if info["description"]:
                print(f"    {info['description']}")
        return 0

    if subcommand == "metrics":
        payload = runtime.metrics(backend)
        if as_json:
            _emit_json(payload)
            return 0
        latency = payload["latency_ms"]
        print("Runtime metrics")
        print(f"  window: last {payload['window_size']} outcomes "
              f"({payload['requests']} recorded)")
        rate = payload["success_rate"]
        print(f"  success rate: {'n/a' if rate is None else format(rate, '.1%')}"
              f" ({payload['successes']} ok / {payload['failures']} failed)")
        print(f"  latency ms: p50={latency['p50']} p95={latency['p95']} "
              f"p99={latency['p99']} min={latency['min']} "
              f"max={latency['max']}")
        print(f"  retries: {payload['retried_requests']} request(s) retried, "
              f"{payload['retry_attempts']} attempt(s) total")
        if payload["error_kinds"]:
            print("  error kinds:")
            for kind, count in payload["error_kinds"].items():
                print(f"    {kind}: {count}")
        for name, entry in payload["by_backend"].items():
            entry_latency = entry["latency_ms"]
            entry_rate = entry["success_rate"]
            print(f"  {name}: {entry['requests']} request(s), "
                  f"success={'n/a' if entry_rate is None else format(entry_rate, '.1%')}, "
                  f"p50={entry_latency['p50']}ms p95={entry_latency['p95']}ms")
        return 0

    if subcommand == "test":
        return _runtime_self_test(runtime, backend, as_json,
                                  bool(getattr(args, "offline", False)))

    if subcommand in ("load", "unload"):
        target = getattr(args, "model", "")
        try:
            try:
                runtime.resolve_model(target, backend)
            except ModelRuntimeError:
                # A fresh CLI process has an empty model registry, so run
                # discovery first instead of failing on an unknown name.
                runtime.discover(backend)
            if subcommand == "load":
                model = runtime.load(target, backend)
                payload = {"loaded": True, "model": model.to_dict()}
            else:
                released = runtime.unload(target, backend)
                payload = {"loaded": False, "released": bool(released),
                           "model_id": target}
        except ModelRuntimeError as exc:
            if as_json:
                _emit_json({"loaded": False, "error": str(exc)})
            else:
                print(f"{subcommand.title()} failed: {exc}", file=sys.stderr)
            return 1
        if as_json:
            _emit_json(payload)
        else:
            if subcommand == "load":
                print(f"Loaded {payload['model']['model_id']}")
            else:
                print(f"Unloaded {target} "
                      f"(released={payload['released']})")
        return 0

    status = runtime.status(probe=bool(getattr(args, "probe", False)))
    if as_json:
        _emit_json(status)
        return 0
    print(_runtime_status_text(status))
    return 0


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


# -- Session 11: the inference fabric on the command line -------------------
#
# These commands drive the *real* inference path: model identity states,
# verification, residency, routing and generation. They never fabricate
# output: a refusal prints the honest terminal state (NEEDS_MODEL,
# RESOURCE_DENIED, POLICY_DENIED, ...) and exits non-zero.

#: ``forge models`` subcommands served by the Session-11 inference fabric.
INFERENCE_MODEL_SUBCOMMANDS = frozenset({
    "discover", "verify", "status", "load", "unload", "backends",
    "evidence", "create-reference"})


def _add_inference_flags(parser, *, suppress: bool = True) -> None:
    """Shared fabric-building flags (parent and subcommand level)."""
    default = argparse.SUPPRESS if suppress else None

    def add(*names, **kwargs):
        if suppress and "default" not in kwargs:
            kwargs["default"] = argparse.SUPPRESS
        parser.add_argument(*names, **kwargs)

    add("--reference-dir", action="append", default=default or [],
        metavar="DIR",
        help="Directory of local reference artifacts "
             "(.forgeref). Repeatable.")
    add("--model-dir", action="append", default=default or [], metavar="DIR",
        help="Directory the native backend may scan for artifacts. "
             "Repeatable. Nothing is ever downloaded.")
    add("--remote", action="append", default=default or [], metavar="PROVIDER",
        help="Remote provider id configured through FORGE_REMOTE_<ID>_* "
             "environment variables. Repeatable. Requires --allow-network.")
    add("--allow-network", action="store_true", default=bool(default) if not suppress else argparse.SUPPRESS,
        help="Permit outbound provider traffic (off by default).")
    add("--ollama-url", default=default or "", metavar="URL",
        help="Ollama endpoint (loopback only unless --allow-network).")
    add("--resource-profile", default=default or "", metavar="NAME",
        help="Resource governor profile: default | g560 (auto-detected "
             "when omitted).")
    add("--max-resident-mb", type=float, default=default or 0.0,
        metavar="MB", help="Resident model memory budget (0 = unbounded).")
    add("--max-slots", type=int, default=default or 2, metavar="N",
        help="Maximum simultaneously resident models.")
    add("--idle-seconds", type=float, default=default or 0.0, metavar="S",
        help="Unload models idle longer than this (0 = never).")
    add("--auto-verify", action="store_true",
        default=bool(default) if not suppress else argparse.SUPPRESS,
        help="Verify a model on first use (real fingerprint + probe).")
    add("--backend", default=default or "", metavar="ID",
        help="Restrict an operation to one backend id.")


def _build_inference(args):
    """Build the Session-11 :class:`InferenceFabric` from CLI flags/env.

    Discovery is lazy, no weight is downloaded, and no socket is opened
    unless ``--allow-network`` (or the runtime config) says so.
    """
    from forge.core.resource_governor import ResourceGovernor, select_profile
    from forge.models.engine import build_inference_fabric
    from forge.models.remote import RemoteProviderConfig

    reference_dirs = tuple(getattr(args, "reference_dir", None) or ())
    model_dirs = tuple(getattr(args, "model_dir", None) or ())
    allow_network = bool(getattr(args, "allow_network", False))
    ollama_url = str(getattr(args, "ollama_url", "") or "")

    runtime_config = None
    if model_dirs or ollama_url:
        from forge.runtime.model_runtime import RuntimeConfig

        runtime_config = RuntimeConfig.load()
        if model_dirs:
            merged = list(runtime_config.model_dirs)
            for item in model_dirs:
                if item not in merged:
                    merged.append(item)
            runtime_config.model_dirs = tuple(merged)
        if ollama_url:
            runtime_config.ollama_url = ollama_url
            if "ollama" not in runtime_config.backends:
                runtime_config.backends = tuple(
                    list(runtime_config.backends) + ["ollama"])
        runtime_config.validate()

    remotes = []
    for provider_id in (getattr(args, "remote", None) or ()):
        # A missing FORGE_REMOTE_<ID>_URL is a configuration error the
        # operator must see, not something to swallow.
        remotes.append(RemoteProviderConfig.from_env(str(provider_id)))

    governor = ResourceGovernor(select_profile(
        str(getattr(args, "resource_profile", "") or "")))
    return build_inference_fabric(
        governor=governor, runtime_config=runtime_config,
        reference_dirs=reference_dirs, allow_network=allow_network,
        remote_configs=tuple(remotes),
        max_resident_bytes=int(float(getattr(args, "max_resident_mb", 0.0)
                                     or 0.0) * 1024 * 1024),
        max_slots=max(1, int(getattr(args, "max_slots", 2) or 2)),
        idle_seconds=float(getattr(args, "idle_seconds", 0.0) or 0.0),
        auto_verify=bool(getattr(args, "auto_verify", False)))


def _print_identity(identity, *, indent: str = "  ") -> None:
    """One model identity, with the states that decide whether it is usable."""
    caps = ",".join(identity.get("capabilities") or ()) or "-"
    memory = identity.get("memory_requirements") or {}
    print("%s%s" % (indent, identity.get("model_id")))
    print("%s  backend=%s provider=%s placement=%s free=%s"
          % (indent, identity.get("backend_id"),
             identity.get("provider_id") or "-",
             identity.get("local_or_remote"), identity.get("free")))
    print("%s  availability=%s verification=%s"
          % (indent, identity.get("availability_state"),
             identity.get("verification_state")))
    print("%s  caps=%s context=%s params=%s bytes=%s format=%s"
          % (indent, caps, identity.get("context_limit"),
             identity.get("parameter_count")
             or identity.get("parameter_label") or "-",
             memory.get("weights_bytes") or identity.get("size_bytes") or "-",
             identity.get("artifact_format") or "-"))
    if identity.get("verification_method"):
        print("%s  verified_by=%s fingerprint=%s"
              % (indent, identity.get("verification_method"),
                 str(identity.get("artifact_fingerprint") or "-")[:16]))


def _run_models_inference(args) -> int:
    """``forge models <discover|verify|status|load|unload|backends|...>``."""
    from forge.models.backends import BackendError
    from forge.models.catalog import CatalogError
    from forge.models.reference_engine import (ReferenceArtifactWriter,
                                               ReferenceModelConfig)

    subcommand = getattr(args, "models_subcommand", "") or ""
    as_json = bool(getattr(args, "json", False))
    model_id = str(getattr(args, "model_id", "") or "")
    backend_id = str(getattr(args, "backend", "") or "")

    # Artifact creation is explicit and local: no download, no weights in the
    # repository, and an existing file is never overwritten without --force.
    if subcommand == "create-reference":
        destination = str(getattr(args, "path", "") or "")
        if not destination:
            print("A destination path is required.", file=sys.stderr)
            return 2
        config = ReferenceModelConfig(
            vocab_size=int(getattr(args, "vocab_size", 96) or 96),
            hidden_size=int(getattr(args, "hidden_size", 24) or 24),
            context_chars=int(getattr(args, "context_chars", 64) or 64),
            seed=int(getattr(args, "seed", 20260913) or 20260913),
            name=str(getattr(args, "name", "reference-clm")
                     or "reference-clm"))
        try:
            report = ReferenceArtifactWriter.write(
                destination, config, overwrite=bool(getattr(args, "force",
                                                            False)))
        except (BackendError, OSError) as exc:
            #: An existing artifact, an unwritable directory or a bad shape is
            #: a refusal with a reason -- never a traceback.
            print("Could not write the reference artifact: %s" % exc,
                  file=sys.stderr)
            return 1
        if as_json:
            _emit_json(report)
        else:
            shape = report.get("config") or {}
            print("Reference artifact written (deterministic, tiny, local)")
            print("  path=%s" % report.get("path"))
            print("  bytes=%s name=%s vocab=%s hidden=%s context_chars=%s "
                  "seed=%s"
                  % (report.get("size_bytes"), shape.get("name"),
                     shape.get("vocab_size"), shape.get("hidden_size"),
                     shape.get("context_chars"), shape.get("seed")))
            print("  fingerprint=%s" % report.get("fingerprint"))
            print("  trained=%s" % report.get("trained"))
            print("  NOTE: this is a first-party reference network. It proves "
                  "the inference path end to end; it has no agentic "
                  "capability and produces character-level text.")
        return 0

    try:
        fabric = _build_inference(args)
    except (ValueError, BackendError) as exc:
        print("Inference configuration error: %s" % exc, file=sys.stderr)
        return 2

    try:
        # Every invocation is a fresh process, so the registry starts empty:
        # an operation on a named model discovers first unless told not to.
        if subcommand != "discover" and not bool(
                getattr(args, "no_discover", False)):
            fabric.catalog.discover(backend_id=backend_id)

        if subcommand == "discover":
            report = fabric.catalog.discover(backend_id=backend_id)
            payload = fabric.models_list(backend_id=backend_id,
                                         capability=str(getattr(
                                             args, "capability", "") or ""),
                                         usable_only=bool(getattr(
                                             args, "usable_only", False)))
            payload["discovery"] = report.to_dict()
            if as_json:
                _emit_json(payload)
                return 0
            print("Discovery: %s backend(s) probed, %s model(s) registered"
                  % (len(report.per_backend), payload["count"]))
            for identity in payload["models"]:
                _print_identity(identity)
            if not payload["models"]:
                print("  (none) - point --reference-dir or --model-dir at a "
                      "directory that holds artifacts, or configure a serving "
                      "backend. Nothing is downloaded automatically.")
            return 0

        if subcommand == "verify":
            payload = fabric.models_verify(model_id, backend_id=backend_id)
            if as_json:
                _emit_json(payload)
                return 0 if payload.get("verified") else 1
            print("Verification (%d/%d verified)"
                  % (payload.get("verified", 0), payload.get("count", 0)))
            for item in payload.get("results", []):
                print("  %s: verified=%s state=%s method=%s"
                      % (item.get("model_id"), item.get("verified"),
                         item.get("state"), item.get("method") or "-"))
                for check in item.get("checks", []):
                    print("    %s: %s%s (%.1fms)%s"
                          % (check.get("name"),
                             "skipped" if check.get("skipped")
                             else ("ok" if check.get("ok") else "FAILED"),
                             (" - " + str(check.get("detail")))
                             if check.get("detail") else "",
                             float(check.get("duration_ms") or 0.0),
                             "" if check.get("ok") or check.get("skipped")
                             else "  <-- verification blocked"))
                if item.get("error"):
                    print("    error: %s" % item["error"])
            if not payload.get("verified"):
                print("NOT VERIFIED - the model stays unselectable. Forge "
                      "never promotes CONFIGURED to READY without a real "
                      "fingerprint, health check and probe generation.")
                return 1
            return 0

        if subcommand == "status":
            payload = fabric.models_status(model_id)
            if as_json:
                _emit_json(payload)
                return 0
            if model_id:
                identity = payload.get("identity") or {}
                _print_identity(identity)
                resident = payload.get("resident")
                if resident:
                    print("  resident: state=%s refs=%s bytes=%s idle=%.3fs"
                          % (resident.get("state"), resident.get("refs"),
                             resident.get("size_bytes"),
                             float(resident.get("idle_seconds") or 0.0)))
                else:
                    print("  resident: no (not loaded)")
                    print("  note: CLI state is per-process. Discovery, "
                          "verification and residency that persist live in a "
                          "running Forge Server.")
                history = payload.get("history") or []
                if history:
                    print("  history:")
                    for item in history[-5:]:
                        print("    %s" % json.dumps(item, default=str)[:160])
            else:
                print("Model registry: %s model(s), %s verified, %s usable "
                      "(local=%s remote=%s)"
                      % (payload.get("total"), payload.get("verified"),
                         payload.get("usable"), payload.get("local"),
                         payload.get("remote")))
                residency = payload.get("resident") or {}
                stats = residency.get("stats") or {}
                print("Residency: %s/%s slots, %s/%s bytes, loads=%s "
                      "evictions=%s unloads=%s duplicate_loads_avoided=%s "
                      "in_use=%s"
                      % (stats.get("slots"), stats.get("max_slots"),
                         stats.get("resident_bytes"), stats.get("max_bytes"),
                         stats.get("loads"), stats.get("evictions"),
                         stats.get("unloads"),
                         stats.get("duplicate_loads_avoided"),
                         stats.get("in_use")))
                for refusal in stats.get("refusals") or []:
                    print("  refusal: %s %s" % (refusal.get("code"),
                                                refusal.get("message")))
                for identity in payload.get("models") or []:
                    print("  %s availability=%s verification=%s backend=%s"
                          % (identity.get("model_id"),
                             identity.get("availability_state"),
                             identity.get("verification_state"),
                             identity.get("backend_id")))
                unverified = [item.get("model_id")
                              for item in payload.get("models") or []
                              if item.get("verification_state") != "verified"]
                if unverified:
                    print("  note: %s unverified. Each CLI invocation builds a "
                          "fresh registry, so verification is not carried over; "
                          "run `forge models verify %s` (or ask a running "
                          "Forge Server, whose registry is long-lived)."
                          % (len(unverified), unverified[0]))
            return 0

        if subcommand == "load":
            if not model_id:
                print("A model id is required: forge models load MODEL_ID",
                      file=sys.stderr)
                return 2
            payload = fabric.models_load(model_id, backend_id=backend_id)
            if as_json:
                _emit_json(payload)
                return 0 if payload.get("loaded") else 1
            if payload.get("loaded"):
                print("Loaded %s on backend %s in %.1fms (%s bytes resident)"
                      % (payload.get("model_id"), payload.get("backend_id"),
                         float(payload.get("duration_ms") or 0.0),
                         payload.get("size_bytes")))
                decision = payload.get("resource_decision") or {}
                print("  resource: allowed=%s profile=%s reason=%s"
                      % (decision.get("allowed"), decision.get("profile"),
                         decision.get("reason") or "-"))
                return 0
            print("REFUSED to load %s: %s (%s)"
                  % (model_id, payload.get("error"),
                     payload.get("error_code")), file=sys.stderr)
            return 1

        if subcommand == "unload":
            if not model_id:
                print("A model id is required: forge models unload MODEL_ID",
                      file=sys.stderr)
                return 2
            payload = fabric.models_unload(model_id,
                                           force=bool(getattr(args, "force",
                                                              False)))
            if as_json:
                _emit_json(payload)
                return 0 if payload.get("unloaded") else 1
            if payload.get("unloaded"):
                print("Unloaded %s%s"
                      % (model_id,
                         " (deferred: in use)" if payload.get("deferred")
                         else ""))
                return 0
            print("Not unloaded: %s" % (payload.get("reason")
                                        or payload.get("error")
                                        or "not resident"), file=sys.stderr)
            print("  note: each CLI invocation builds a fresh residency cache, "
                  "so a model loaded by a previous command is already gone. "
                  "Residency that outlives a command lives in a running Forge "
                  "Server (POST /api/v1/models/load).", file=sys.stderr)
            return 1

        if subcommand == "backends":
            probe = not bool(getattr(args, "offline", False))
            statuses = fabric.backends(probe=probe)
            if as_json:
                _emit_json({"backends": statuses})
                return 0
            print("Backends (probed=%s)" % probe)
            for status in statuses:
                print("  %s: kind=%s configured=%s reachable=%s ready=%s "
                      "local=%s network=%s"
                      % (status.get("backend_id"), status.get("kind"),
                         status.get("configured"), status.get("reachable"),
                         status.get("ready"), status.get("local"),
                         status.get("requires_network")))
                if status.get("detail"):
                    print("    %s" % str(status["detail"])[:200])
                if status.get("error"):
                    print("    error: %s" % str(status["error"])[:200])
            return 0

        if subcommand == "evidence":
            payload = fabric.evidence(limit=int(getattr(args, "limit", 50)
                                                or 50))
            if as_json:
                _emit_json(payload)
                return 0
            print("Inference evidence (self-improvement feed, proposal only)")
            for key in ("requests", "success_rate", "neural_results",
                        "deterministic_results", "stale_results",
                        "cancelled_results", "proposals"):
                if key in payload:
                    print("  %s: %s" % (key, payload[key]))
            for item in (payload.get("recent") or [])[:5]:
                print("  - %s" % json.dumps(item, default=str)[:180])
            if not payload.get("requests"):
                print("  (empty) no inference has run in this process. The "
                      "long-lived evidence feed belongs to a running Forge "
                      "Server: GET /api/v1/inference/status.")
            return 0

        print("Unknown models subcommand: %r" % subcommand, file=sys.stderr)
        return 2
    except CatalogError as exc:
        print("Model catalog error: %s (%s)" % (exc.message, exc.code),
              file=sys.stderr)
        return 1
    except BackendError as exc:
        print("Backend error: %s (%s)" % (exc.message, exc.code),
              file=sys.stderr)
        return 1
    finally:
        try:
            fabric.cancel_all("cli-exit")
        except Exception:
            pass


def _read_prompt(args) -> str:
    """Prompt text from the argument, --prompt, --prompt-file or stdin."""
    text = ""
    positional = getattr(args, "prompt", None)
    if isinstance(positional, (list, tuple)):
        text = " ".join(str(item) for item in positional).strip()
    elif positional:
        text = str(positional).strip()
    if not text:
        text = str(getattr(args, "prompt_text", "") or "").strip()
    path = str(getattr(args, "prompt_file", "") or "")
    if path:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read().strip()
        except OSError as exc:
            raise ValueError("cannot read --prompt-file %s: %s" % (path, exc))
    if not text and not sys.stdin.isatty():
        text = sys.stdin.read().strip()
    return text


def _run_infer(args) -> int:
    """``forge infer`` - one real generation, locally or via a Forge Server."""
    from forge.models.request import ModelRequest

    as_json = bool(getattr(args, "json", False))
    strict_neural = bool(getattr(args, "strict_neural", False))
    server_url = str(getattr(args, "server", "") or "").strip()

    try:
        prompt = _read_prompt(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if not prompt:
        print("A prompt is required: forge infer \"...\" "
              "(or --prompt-file / stdin).", file=sys.stderr)
        return 2

    context = str(getattr(args, "context", "") or "")
    context_file = str(getattr(args, "context_file", "") or "")
    if context_file:
        try:
            with open(context_file, "r", encoding="utf-8", errors="replace") \
                    as handle:
                context = handle.read()
        except OSError as exc:
            print("cannot read --context-file: %s" % exc, file=sys.stderr)
            return 2

    kwargs = {
        "context": context,
        "capability": str(getattr(args, "capability", "") or ""),
        "model": str(getattr(args, "model", "") or ""),
        "backend": str(getattr(args, "backend", "") or ""),
        "task": str(getattr(args, "task", "") or ""),
        "task_id": str(getattr(args, "task_id", "") or ""),
        "attempt_id": str(getattr(args, "attempt_id", "") or ""),
        "classification": str(getattr(args, "classification", "") or ""),
        "max_output_tokens": getattr(args, "max_tokens", None),
        "temperature": getattr(args, "temperature", None),
        "timeout": getattr(args, "timeout", None),
        "allow_deterministic": not bool(getattr(args, "no_deterministic",
                                                False)),
    }

    # -- remote: the G560 thin-client path (no local models at all) --------
    if server_url:
        from forge.server.client import (ForgeServerClient,
                                         ForgeServerClientError)

        token = str(getattr(args, "token", "") or os.environ.get(
            "FORGE_SERVER_TOKEN", "") or "")
        client = ForgeServerClient(server_url, token=token,
                                   timeout=float(getattr(args, "timeout", None)
                                                 or 60.0) + 10.0)
        try:
            if bool(getattr(args, "stream", False)):
                chunks = []

                def _delta(text, event):
                    chunks.append(text)
                    if not as_json:
                        sys.stdout.write(text)
                        sys.stdout.flush()

                outcome = client.stream(prompt, on_delta=_delta,
                                        wait=float(getattr(args, "wait", 2.0)
                                                   or 2.0), **kwargs)
                if not as_json:
                    print("")
                body = dict(outcome)
                body.pop("text", None)
                body.update({
                    "request_id": outcome.get("request_id"),
                    "model_id": outcome.get("model_id"),
                    "backend_id": outcome.get("backend_id"),
                    "state": outcome.get("state"),
                    "success": not bool(outcome.get("error_code")),
                    "neural": bool(outcome.get("model_id"))
                    and "deterministic" not in str(outcome.get("backend_id")),
                    "complete": outcome.get("complete"),
                    "events": outcome.get("events"),
                    "chars": outcome.get("chars"),
                    "error": outcome.get("error"),
                    "error_code": outcome.get("error_code"),
                    "text": outcome.get("text"),
                    "via": "server",
                    "server": client.base_url,
                })
                #: The server's own provenance wins over the client's guess.
                for key in ("neural", "state", "success", "finish_reason",
                            "latency_ms", "verification_state",
                            "availability_state", "output_scan"):
                    if outcome.get(key) is not None:
                        body[key] = outcome[key]
                body["_echoed"] = bool(outcome.get("echoed")) and not as_json
            else:
                body = client.generate(prompt, **kwargs)
                body["via"] = "server"
                body["server"] = client.base_url
        except ForgeServerClientError as exc:
            if as_json:
                _emit_json({"success": False, "error": exc.message,
                            "error_code": exc.code, "via": "server",
                            "http_status": exc.http_status})
            else:
                print("Server refused the request: %s (%s)"
                      % (exc.message, exc.code), file=sys.stderr)
            return 1
        return _report_inference(body, as_json=as_json,
                                 explain=bool(getattr(args, "explain", False)),
                                 strict_neural=strict_neural)

    # -- local: the same fabric the agents and the server use ---------------
    from forge.models.backends import BackendError
    from forge.models.catalog import CatalogError

    try:
        fabric = _build_inference(args)
    except (ValueError, BackendError) as exc:
        print("Inference configuration error: %s" % exc, file=sys.stderr)
        return 2

    try:
        if not bool(getattr(args, "no_discover", False)):
            fabric.catalog.discover(backend_id=str(getattr(args, "backend", "")
                                                   or ""))
        if bool(getattr(args, "verify", False)):
            target = str(getattr(args, "model", "") or "")
            verifications = ([fabric.catalog.verify(target)] if target
                             else fabric.catalog.verify_all())
            if not verifications:
                print("Nothing to verify: no model is registered. Discover "
                      "one first (--reference-dir / --model-dir).",
                      file=sys.stderr)
                return 1
            for verification in verifications:
                if not as_json:
                    print("verify: %s verified=%s method=%s%s"
                          % (verification.model_id, verification.verified,
                             verification.method or "-",
                             (" error=%s" % verification.error)
                             if verification.error else ""))
                if (not verification.verified and strict_neural
                        and (not target or verification.model_id == target)):
                    print("Model %s is not verified; refusing to present its "
                          "output as trustworthy." % verification.model_id,
                          file=sys.stderr)
                    return 1
        request = ModelRequest(
            prompt=prompt,
            context=str(kwargs.get("context") or ""),
            capability=str(kwargs.get("capability") or ""),
            task=str(kwargs.get("task") or ""),
            model=str(kwargs.get("model") or ""),
            backend=str(kwargs.get("backend") or ""),
            max_output_tokens=kwargs.get("max_output_tokens"),
            temperature=kwargs.get("temperature"),
            timeout=kwargs.get("timeout"),
            classification=str(kwargs.get("classification") or ""),
            require_verified=not bool(getattr(args, "allow_unverified", False)),
            allow_deterministic=bool(kwargs.get("allow_deterministic", True)),
            task_id=str(kwargs.get("task_id") or ""),
            attempt_id=str(kwargs.get("attempt_id") or ""),
        )
        if bool(getattr(args, "stream", False)):
            handle = fabric.stream(request,
                                   task_id=str(kwargs.get("task_id") or ""),
                                   attempt_id=str(kwargs.get("attempt_id") or ""))
            echoed = 0
            for event in handle.events():
                delta = str(getattr(event, "delta", "") or "")
                if delta and not as_json:
                    sys.stdout.write(delta)
                    sys.stdout.flush()
                    echoed += len(delta)
            result = handle.wait(float(getattr(args, "timeout", None) or 120.0))
            if not as_json and echoed:
                print("")
            body = result.to_dict()
            body["text"] = result.text
            body["streamed"] = True
            body["stream"] = handle.stream.snapshot()
            #: The deltas were already written to stdout; printing the text
            #: again would look like the model said it twice.
            body["_echoed"] = bool(echoed)
        else:
            result = fabric.generate(request,
                                     task_id=str(kwargs.get("task_id") or ""),
                                     attempt_id=str(kwargs.get("attempt_id")
                                                    or ""))
            body = result.to_dict()
            body["text"] = result.text
        body["via"] = "local"
        return _report_inference(body, as_json=as_json,
                                 explain=bool(getattr(args, "explain", False)),
                                 strict_neural=strict_neural)
    except CatalogError as exc:
        print("Model catalog error: %s (%s)" % (exc.message, exc.code),
              file=sys.stderr)
        return 1
    except BackendError as exc:
        print("Backend error: %s (%s)" % (exc.message, exc.code),
              file=sys.stderr)
        return 1
    finally:
        try:
            fabric.cancel_all("cli-exit")
        except Exception:
            pass


def _report_inference(body, *, as_json: bool, explain: bool,
                      strict_neural: bool) -> int:
    """Print one inference outcome honestly, and choose the exit code.

    The provenance line is the point: which model, which backend, whether a
    neural network actually produced the text, and why it ended the way it
    did. A deterministic fallback is labelled as such, never as a model
    answer.
    """
    success = bool(body.get("success"))
    neural = bool(body.get("neural"))
    if as_json:
        _emit_json({key: value for key, value in body.items()
                    if not key.startswith("_")})
        if not success or (strict_neural and not neural):
            return 1
        return 0

    text = str(body.get("text") or "")
    if text and not body.get("_echoed"):
        print(text)
        print("")
    print("provenance: model=%s backend=%s neural=%s state=%s finish=%s "
          "latency=%.1fms"
          % (body.get("model_id") or "-", body.get("backend_id") or "-",
             neural, body.get("state") or "-", body.get("finish_reason") or "-",
             float(body.get("latency_ms") or 0.0)))
    if body.get("verification_state"):
        print("            verification=%s availability=%s"
              % (body.get("verification_state"),
                 body.get("availability_state")))
    if body.get("streamed"):
        stream = body.get("stream") or {}
        print("            stream: events=%s complete=%s chars=%s "
              "dropped_chars=%s truncated=%s ttft=%sms"
              % (stream.get("sequence"), stream.get("complete"),
                 stream.get("chars"), stream.get("dropped_chars"),
                 stream.get("truncated"),
                 stream.get("time_to_first_token_ms")))
    scan = body.get("output_scan") or {}
    if scan.get("flags"):
        print("            output scan flags: %s" % ", ".join(scan["flags"]))
    if success and not neural:
        print("NOTE: answered by the deterministic non-neural fallback. No "
              "model produced this text; it is not model output.")
    if not success:
        print("REFUSED/FAILED: state=%s error_code=%s"
              % (body.get("state"), body.get("error_code") or "-"),
              file=sys.stderr)
        if body.get("error"):
            print("  %s" % str(body["error"])[:400], file=sys.stderr)
    if explain:
        routing = body.get("routing") or {}
        print("routing: %s" % (routing.get("reason") or "-"))
        print("         selected=%s backend=%s state=%s score=%s"
              % (routing.get("selected_model") or "-",
                 routing.get("selected_backend") or "-",
                 routing.get("state") or "-", routing.get("score")))
        for item in (routing.get("rejected") or [])[:6]:
            print("         rejected: %s - %s"
                  % (item.get("model_id"), item.get("reason")))
        resource = body.get("resource_result") or {}
        print("resource: allowed=%s profile=%s device=%s model_loading=%s"
              % (resource.get("allowed"), resource.get("profile"),
                 resource.get("device_class"),
                 resource.get("model_loading_allowed")))
        for check in (resource.get("checks") or [])[:8]:
            print("          %s: ok=%s %s" % (check.get("name"),
                                              check.get("ok"),
                                              check.get("detail") or ""))
        policy = body.get("policy_result") or {}
        if policy:
            print("policy: %s" % json.dumps(policy, default=str)[:240])
        context = body.get("context") or {}
        if context:
            print("context: limit=%s budget=%s estimated=%s feasible=%s (%s)"
                  % (context.get("context_limit"), context.get("budget_tokens"),
                     context.get("estimated_tokens"), context.get("feasible"),
                     context.get("reason") or "-"))
        fallback = body.get("fallback") or {}
        ladder = fallback.get("ladder") or {}
        for step in (ladder.get("steps") or [])[:8]:
            print("ladder: %s %s/%s attempted=%s outcome=%s%s"
                  % (step.get("order"), step.get("tier"),
                     "neural" if step.get("neural") else "deterministic",
                     step.get("model_id"), step.get("outcome") or "-",
                     (" error=%s" % step.get("error_code"))
                     if step.get("error_code") else ""))
        for observation in (body.get("observations") or [])[-8:]:
            print("observed: %s -> %s%s"
                  % (observation.get("phase"), observation.get("state"),
                     (" (%s)" % observation.get("error_code"))
                     if observation.get("error_code") else ""))
    if not success:
        return 1
    if strict_neural and not neural:
        #: --strict-neural is a demand for model output, so a deterministic
        #: answer has to be refused out loud rather than exiting 1 silently.
        print("REFUSED: --strict-neural was set, but this answer came from "
              "the deterministic non-neural fallback (model=%s backend=%s). "
              "No model produced it; verify a model with `forge models "
              "verify` or point --reference-dir/--model-dir at one."
              % (body.get("model_id") or "-", body.get("backend_id") or "-"),
              file=sys.stderr)
        return 1
    return 0


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
    resource_profile = payload.get("resource_profile") or {}
    if resource_profile:
        print(f"  resources: profile={resource_profile.get('name', '?')} "
              f"workers<={resource_profile.get('max_workers', '?')} "
              f"model_loading="
              f"{'allowed' if resource_profile.get('model_loading_allowed') else 'DENIED'}")
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


def _run_server_login(args) -> int:
    from forge.server.client import ForgeServerClient, ForgeServerClientError

    base = _server_base_url(args)
    key = getattr(args, "key", "") or os.environ.get("FORGE_SERVER_TOKEN", "")
    client = ForgeServerClient(base)
    try:
        if key:
            result = client.login(key)
        elif client.token:
            result = {"session": {"token_source": "existing"}}
        else:
            print("Pass --key or set FORGE_SERVER_TOKEN.", file=sys.stderr)
            return 2
    except ForgeServerClientError as exc:
        print(f"Login failed ({exc.code}): {exc.message}", file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        _emit_json(result)
        return 0
    token = result.get("token", "")
    session = result.get("session", {})
    print("Forge Server session token (use as Bearer credential):")
    print(f"  {token}")
    print(f"Principal: {session.get('principal', '?')} "
          f"(role={session.get('role', '?')})")
    return 0


def _run_server(args, parser) -> int:
    subcommand = getattr(args, "server_subcommand", "") or "start"
    if subcommand == "start":
        return _run_server_start(args, parser)
    if subcommand == "status":
        return _run_server_status(args)
    if subcommand == "health":
        return _run_server_health(args)
    if subcommand == "login":
        return _run_server_login(args)
    parser.error(f"Unknown server subcommand: {subcommand!r}")
    return 2


# Durable record for fenced multi-agent runs. Control plane
# orchestrations write one store per run under <project>/.forge/tasks/
# (one file per orchestration); any orchestrator may also point
# straight at a single store file.
DEFAULT_TASKS_DIR = os.path.join(".forge", "tasks")


def _task_stores(store_arg):
    """Resolve the --store argument to [(run label, store path)].

    Accepts a single store file or a directory of per-run stores
    (the control plane default). An absent store resolves to [].
    """
    if store_arg:
        if os.path.isdir(store_arg):
            base = store_arg
        elif os.path.exists(store_arg):
            return [(os.path.splitext(os.path.basename(store_arg))[0],
                     store_arg)]
        else:
            return []
    else:
        base = DEFAULT_TASKS_DIR
    if not os.path.isdir(base):
        return []
    found = []
    for name in sorted(os.listdir(base)):
        if name.endswith(".db") and os.path.isfile(os.path.join(base, name)):
            found.append((name[:-3], os.path.join(base, name)))
    return found


def _run_tasks(args) -> int:
    """CLI entry for inspecting the fenced scheduler's durable record."""
    from forge.core.dag_scheduler import DAGScheduler

    store = getattr(args, "store", "") or ""
    subcommand = getattr(args, "tasks_subcommand", "list") or "list"
    as_json = bool(getattr(args, "json", False))

    stores = _task_stores(store)
    if not stores:
        shown = store or os.path.join(DEFAULT_TASKS_DIR, "<run>.db")
        if as_json:
            _emit_json({"store": shown, "exists": False, "tasks": []})
        else:
            print(f"No scheduler store at {shown}.")
            print("Fenced multi-agent runs (control plane orchestrations) "
                  "record tasks, attempts, and their event log here.")
        return 0

    if subcommand == "list":
        limit = max(0, int(getattr(args, "limit", 50) or 50))
        rows = []
        for label, path in stores:
            scheduler = DAGScheduler(path)
            for row in scheduler.task_rows(limit=limit):
                entry = dict(row)
                entry["run"] = label
                rows.append(entry)
        if as_json:
            _emit_json({"store": store or DEFAULT_TASKS_DIR,
                        "count": len(rows), "tasks": rows})
        else:
            if not rows:
                print(f"Stores under {store or DEFAULT_TASKS_DIR} hold "
                      f"no tasks yet.")
            else:
                run_width = max(len(r["run"]) for r in rows)
                task_width = max(len(r["task_id"]) for r in rows)
                print(f"{'RUN':<{run_width}}  "
                      f"{'TASK':<{task_width}}  {'STATE':<10}  ATT  ERROR")
                for row in rows:
                    print(f"{row['run']:<{run_width}}  "
                          f"{row['task_id']:<{task_width}}  "
                          f"{row['state']:<10}  "
                          f"{row['attempts']:<3}  "
                          f"{row['error'] or '-'}")
        return 0

    if subcommand == "show":
        found = None
        for label, path in stores:
            scheduler = DAGScheduler(path)
            task = scheduler.task_row(args.task)
            if task is not None:
                found = (label, scheduler, task)
                break
        if found is None:
            where = store or DEFAULT_TASKS_DIR
            print(f"No task {args.task!r} in {where}.", file=sys.stderr)
            return 2
        label, scheduler, task = found
        record = dict(task)
        record["run"] = label
        record["attempts_log"] = scheduler.attempts(task["task_id"])
        record["events"] = scheduler.events(task["task_id"], limit=20)
        if as_json:
            _emit_json(record)
        else:
            print(f"Run:      {label}")
            print(f"Task:     {task['task_id']}")
            print(f"State:    {task['state']}")
            if task.get("description"):
                print(f"Desc:     {task['description'][:200]}")
            print(f"Attempts: {task['attempts']}"
                  f"  deps={task.get('dependencies') or '-'}"
                  f"  timeout={task['timeout']}")
            if task.get("error"):
                print(f"Error:    {task['error'][:400]}")
            if task.get("output"):
                print(f"Output:   {task['output'][:400]}")
            logs = scheduler.attempts(task["task_id"])
            if logs:
                print("Attempt log (newest first):")
                for entry in logs:
                    print(f"  g{entry['generation']} {entry['state']:<10} "
                          f"{entry['reason'] or '-'}")
            events = scheduler.events(task["task_id"], limit=10)
            if events:
                print("Events (newest first):")
                for event in events:
                    print(f"  #{event['seq']} {event['state']:<10} "
                          f"{event['detail']}")
        return 0

    if subcommand == "events":
        limit = max(0, int(getattr(args, "limit", 50) or 50))
        wanted = getattr(args, "task", "") or ""
        grouped = []  # (run label, events)
        for label, path in stores:
            scheduler = DAGScheduler(path)
            events = scheduler.events(wanted, limit=limit)
            if events:
                grouped.append((label, events))
        flat = [event for _, events in grouped for event in events]
        if as_json:
            _emit_json({"store": store or DEFAULT_TASKS_DIR,
                        "count": len(flat),
                        "runs": {label: events
                                 for label, events in grouped}})
        else:
            if not grouped:
                print(f"No events recorded in "
                      f"{store or DEFAULT_TASKS_DIR}.")
            elif len(grouped) == 1:
                print(f"{'SEQ':>6}  {'TASK':<20}  {'STATE':<10}  DETAIL")
                for event in grouped[0][1]:
                    print(f"{event['seq']:>6}  "
                          f"{event['task_id']:<20}  "
                          f"{event['state']:<10}  {event['detail']}")
            else:
                for label, events in grouped:
                    print(f"Run {label}:")
                    for event in events:
                        print(f"  #{event['seq']:>5}  "
                              f"{event['task_id']:<20}  "
                              f"{event['state']:<10}  {event['detail']}")
        return 0

    parser.error(f"Unknown tasks subcommand: {subcommand!r}")
    return 2


def _run_training(args) -> int:
    """CLI entry for the agent training lab.

    Every command is honest about what actually happened: with no
    OPENAI_API_KEY, no collected data, or a policy refusal, the command
    reports the refusal — it never fabricates a training job or a
    trained model.
    """
    from forge.agents.training import AgentTrainingPipeline

    subcommand = getattr(args, "training_subcommand", "scan") or "scan"
    as_json = bool(getattr(args, "json", False))
    pipeline = AgentTrainingPipeline("cli")

    runs: list[dict] = []
    if getattr(args, "runs", ""):
        try:
            with open(args.runs, encoding="utf-8") as handle:
                loaded = json.load(handle)
        except (OSError, ValueError) as exc:
            print(f"Cannot read runs file {args.runs!r}: {exc}",
                  file=sys.stderr)
            return 2
        if not isinstance(loaded, list):
            print("Runs file must hold a JSON list of "
                  "{requirement, output, status, task_id} objects.",
                  file=sys.stderr)
            return 2
        runs = [entry for entry in loaded if isinstance(entry, dict)]

    if subcommand in ("scan", "export", "start") and runs:
        pipeline.collect_training_data(args.agent, runs)

    if subcommand == "scan":
        dataset = pipeline._datasets.get(args.agent)
        if dataset is None or dataset.size == 0:
            print(f"No training examples for agent {args.agent!r}."
                  + (" Pass --runs runs.json with real run outcomes."
                     if not runs else ""), file=sys.stderr)
            return 2
        from forge.agents.training import TrainingDataPolicy

        verdict = TrainingDataPolicy().evaluate(dataset, authorized=False)
        report = verdict["report"]
        if as_json:
            _emit_json({"agent": args.agent, "examples": dataset.size,
                        "upload_allowed": verdict["allowed"],
                        "reason": verdict["reason"], "scan": report})
        else:
            print(f"Agent:    {args.agent}")
            rate = dataset.success_rate
            print(f"Examples: {dataset.size} "
                  f"(success rate {rate:.0%})")
            print(f"Scan:     {report.get('clean', 0)} clean, "
                  f"{report.get('confidential_or_pii', 0)} "
                  f"PII/confidential, {report.get('secret', 0)} secret")
            if report.get("secret_example_indexes"):
                print(f"  secret example indexes: "
                      f"{report['secret_example_indexes']}")
            print(f"Upload:   "
                  f"{'allowed' if verdict['allowed'] else 'REFUSED'} — "
                  f"{verdict['reason']}")
        return 0

    if subcommand == "export":
        report = pipeline.export_dataset(args.agent)
        if "error" in report:
            print(report["error"], file=sys.stderr)
            return 2
        if as_json:
            _emit_json(report)
        else:
            tuner = report["fine_tuner_available"]
            tuner_note = ("OPENAI_API_KEY set" if tuner
                          else "OPENAI_API_KEY not set")
            print(f"Agent:    {report['agent']}")
            print(f"Examples: {report['examples']}")
            print("Fine-tuner available: "
                  f"{'yes' if tuner else 'no'} ({tuner_note})")
            policy = report["upload_policy"]
            verdict = "allowed" if policy["upload_allowed"] else "REFUSED"
            print(f"Upload policy: {policy['mode']} — {verdict}"
                  f" — {policy['reason']}")
            print("JSONL preview:")
            for line in report["jsonl_preview"].splitlines()[:5]:
                print(f"  {line}")
        return 0

    if subcommand == "start":
        if not runs:
            print("start needs --runs runs.json with collected examples.",
                  file=sys.stderr)
            return 2
        job = pipeline.start_fine_tuning(
            args.agent, model=getattr(args, "model", "") or "gpt-4o-mini",
            authorized=bool(getattr(args, "authorize", False)))
        if "error" in job:
            print(job["error"], file=sys.stderr)
            return 3
        if as_json:
            _emit_json(job)
        else:
            result = job.get("result", {})
            print(f"Agent:     {job['agent']}")
            print(f"Model:     {job['model']}")
            print(f"File id:   {job['file_id']}")
            print(f"Job:       {result.get('id', result)}")
            print("Fine-tuning was actually submitted to the provider; "
                  "check it with `forge training job <id>`.")
        return 0

    if subcommand == "job":
        status = pipeline.check_training_status(args.job_id)
        if "error" in status:
            print(status["error"], file=sys.stderr)
            return 3
        if as_json:
            _emit_json(status)
        else:
            print(f"Job:     {status.get('id', args.job_id)}")
            print(f"Status:  {status.get('status', 'unknown')}")
            if status.get("fine_tuned_model"):
                print(f"Model:   {status['fine_tuned_model']}")
            if status.get("error"):
                print(f"Error:   {status['error']}")
        return 0

    parser.error(f"Unknown training subcommand: {subcommand!r}")
    return 2


def _engineer_profile(name: str):
    """Resolve a profile by name, or None when none was asked for."""
    if not name:
        return None
    from forge.profiles import default_registry
    registry = default_registry()
    if not registry.has(name):
        raise SystemExit(
            "unknown profile %r; available: %s"
            % (name, ", ".join(sorted(registry.list()))))
    return registry.get(name)


def _run_engineer(args) -> int:
    """Drive the A83 engineering platform from the command line."""
    import json as _json
    from pathlib import Path

    root = Path(".")
    command = getattr(args, "engineer_command", "") or ""

    if command == "plan":
        from forge.architect import ProjectArchitect, PlanStore
        profile = _engineer_profile(args.profile)
        architect = ProjectArchitect(root, profiles=[profile] if profile else ())
        plan = architect.plan(args.requirement)
        store = PlanStore(root)
        store.save(plan)
        if args.json:
            print(_json.dumps(plan.to_dict(), indent=2, sort_keys=True))
        else:
            print(plan.render())
            print()
            print("saved as plan %s (draft); review it, then:" % plan.id)
            print("  forge engineer edit %s --field open_questions "
                  "--value '[\"...\"]'" % plan.id)
            print("  forge engineer approve %s --actor you" % plan.id)
        return 0

    if command == "list":
        from forge.architect import PlanStore
        entries = PlanStore(root).list()
        if not entries:
            print("no stored plans")
            return 0
        for entry in entries:
            print("%-14s %-10s rev %-3s %s" % (
                entry.get("id", "?"), entry.get("status", "?"),
                entry.get("revision", "?"),
                entry.get("requirement", "")[:60]))
        return 0

    if command == "show":
        from forge.architect import PlanStore
        plan = PlanStore(root).load(args.plan_id)
        if args.json:
            print(_json.dumps(plan.to_dict(), indent=2, sort_keys=True))
        else:
            print(plan.render())
        return 0

    if command == "approve":
        from forge.architect import PlanError, PlanStore
        store = PlanStore(root)
        try:
            approval = store.approve(args.plan_id, actor=args.actor)
        except PlanError as exc:
            print("refused: %s" % exc)
            return 1
        print("approved %s rev %d fingerprint %s" % (
            approval.plan_id, approval.revision, approval.fingerprint))
        return 0

    if command == "edit":
        from forge.architect import PlanError, PlanStore
        try:
            value = _json.loads(args.value)
        except ValueError as exc:
            print("--value must be valid JSON: %s" % exc)
            return 2
        store = PlanStore(root)
        try:
            plan = store.update(args.plan_id, {args.field: value},
                                actor=args.actor,
                                summary=args.summary or "edited via CLI")
        except PlanError as exc:
            print("refused: %s" % exc)
            return 1
        print("plan %s now at revision %d, status %s" % (
            plan.id, plan.revision, plan.status))
        print("any previous approval is no longer valid; re-approve before "
              "executing.")
        return 0

    if command == "execute":
        from forge.architect import NotApproved, PlanStore
        store = PlanStore(root)
        try:
            plan = store.checkout_for_execution(args.plan_id)
        except NotApproved as exc:
            print("refused: %s" % exc)
            return 1
        ready = plan.ready_tasks()
        print("plan %s is executing (fingerprint %s)" % (
            plan.id, plan.fingerprint()))
        if not ready:
            print("no tasks are ready yet")
            return 0
        print("execution frontier:")
        for task in ready:
            print("  %s  %s" % (task.id, task.title))
        return 0

    if command == "pipeline":
        from forge.engineering import EngineeringPipeline
        roles = tuple(item.strip() for item in args.roles.split(",")
                      if item.strip())
        profile = _engineer_profile(args.profile)
        report = EngineeringPipeline(root, profile=profile).run(roles=roles)
        print("pipeline: %s (%.0f ms, context revision %d)" % (
            report.status, report.duration_ms, report.context_revision))
        for stage in report.stages:
            print("  %-14s %-10s %6.0f ms%s" % (
                stage.role, stage.status, stage.duration_ms,
                " — %s" % stage.error if stage.error else ""))
        return 0 if report.status == "passed" else 1

    if command == "hardware":
        from forge.hardware import HardwareAgent, render
        agent = HardwareAgent(root, profile=_engineer_profile(args.profile))
        _report, matrix = agent.survey()
        if args.json:
            print(_json.dumps(matrix.to_dict(), indent=2, sort_keys=True,
                              default=str))
        else:
            print(render(matrix))
        return 0

    if command == "boot":
        from forge.vm import VmHarness, render
        profile = _engineer_profile(args.profile)
        harness = VmHarness(root, profile=profile)
        wanted = ()
        if args.scenario:
            wanted = tuple(item for item in harness.scenarios()
                           if item.name == args.scenario)
            if not wanted:
                print("no boot scenario named %r" % args.scenario)
                return 2
        report = harness.run(build=not args.no_build, scenarios=wanted)
        print(render(report))
        return 0 if report.ok else 1

    if command == "memory":
        from forge.knowledge import ProjectMemory, render
        memory = ProjectMemory(root)
        if args.add:
            entry = memory.add(args.add_kind, args.add, args.detail,
                               source=args.source)
            print("recorded %s %s" % (entry.kind, entry.id))
            return 0
        if args.search:
            hits = memory.search(args.search,
                                 kinds=(args.kind,) if args.kind else ())
            if not hits:
                print("nothing matched %r" % args.search)
                return 0
            for entry, score in hits:
                print("%.2f  [%s] %s" % (score, entry.kind, entry.title))
            return 0
        if args.path:
            for entry in memory.for_path(args.path):
                print("[%s] %s" % (entry.kind, entry.title))
            return 0
        print(render(memory))
        return 0

    if command == "tasks":
        from forge.models.taskmap import routing_table
        for item in routing_table():
            print("%-16s %-14s capability=%-14s window>=%-7d %s" % (
                item["kind"], item["model_class"], item["capability"],
                item["min_context_window"], item["reason"]))
        return 0

    if command == "stats":
        from forge.models.stats import StatsStore, render
        print(render(StatsStore(root).snapshot(), limit=args.limit))
        return 0

    if command == "agents":
        from forge.engineering import default_roster
        roster = default_roster()
        implemented = set(roster.implemented())
        for spec in roster.specs:
            print("%-14s %-11s mutating=%-5s %s" % (
                spec.role, "implemented" if spec.role in implemented
                else "not wired", spec.mutating, spec.responsibility[:60]))
        return 0

    print("nothing to do; try: forge engineer --help")
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

    # -- Session 11: the real inference fabric -----------------------------
    # Identity states, verification, residency and backend health. These
    # subcommands never invent availability: a model is `discovered` until a
    # real fingerprint + health + probe generation verifies it.
    _inference_subs = {
        "discover": "Discover models across backends (no downloads)",
        "verify": "Verify a model for real (fingerprint + health + probe)",
        "status": "Show registry/residency status (one model or all)",
        "load": "Load a model into bounded residency",
        "unload": "Unload a model (in-use models are protected)",
        "backends": "Show backend health (native/ollama/llama.cpp/remote)",
        "evidence": "Show the inference evidence feed (self-improvement)",
        "create-reference": "Write a tiny local reference artifact "
                            "(explicit, offline)",
    }
    for _sub_name, _sub_help in _inference_subs.items():
        _sub = models_subparsers.add_parser(_sub_name, help=_sub_help)
        _sub.add_argument("--json", action="store_true",
                          default=argparse.SUPPRESS,
                          help="Emit machine-readable JSON")
        _add_inference_flags(_sub)
        if _sub_name in ("verify", "status", "load", "unload", "evidence"):
            if _sub_name != "evidence":
                _sub.add_argument("model_id", nargs="?", default="",
                                  help="Model id (e.g. reference:reference-clm)")
            _sub.add_argument("--no-discover", action="store_true",
                              default=False, dest="no_discover",
                              help="Use only what is already registered")
        if _sub_name in ("unload", "create-reference"):
            _sub.add_argument("--force", action="store_true",
                              default=argparse.SUPPRESS,
                              help="Force the operation (override an "
                                   "existing artifact / in-use protection)")
        if _sub_name in ("discover", "verify"):
            # SUPPRESS: `forge models --capability X discover` keeps X.
            _sub.add_argument("--capability", "-c",
                              default=argparse.SUPPRESS,
                              help="Filter by capability")
        if _sub_name == "discover":
            _sub.add_argument("--usable-only", action="store_true",
                              default=False,
                              help="Only models that are ready and verified")
        if _sub_name == "backends":
            _sub.add_argument("--offline", action="store_true", default=False,
                              help="Do not probe backend health over the "
                                   "network")
        if _sub_name == "evidence":
            _sub.add_argument("--limit", type=int, default=50,
                              help="Maximum evidence records to show")
        if _sub_name == "create-reference":
            _sub.add_argument("path", help="Destination file path "
                                           "(.forgeref)")
            _sub.add_argument("--name", default="reference-clm",
                              help="Model name inside the artifact")
            _sub.add_argument("--vocab-size", type=int, default=96)
            _sub.add_argument("--hidden-size", type=int, default=24)
            _sub.add_argument("--context-chars", type=int, default=64)
            _sub.add_argument("--seed", type=int, default=20260913)
    _add_inference_flags(models_parser, suppress=False)

    # `forge infer` - one real generation (local fabric or a Forge Server).
    infer_parser = subparsers.add_parser(
        "infer",
        help="Run one real inference request",
        description="Generate text through the Session-11 inference fabric. "
                    "Local by default; --server delegates to a Forge Server "
                    "(the G560 thin-client path, no local models). Output "
                    "always reports its provenance: which model and backend "
                    "produced it, or that the deterministic non-neural "
                    "fallback answered. A refusal is printed as a refusal.",
    )
    infer_parser.add_argument("prompt", nargs="*", default=[],
                              help="Prompt text (or use --prompt-file/stdin)")
    infer_parser.add_argument("--prompt-file", default="", metavar="PATH",
                              help="Read the prompt from a file")
    infer_parser.add_argument("--context", default="", help="Context text")
    infer_parser.add_argument("--context-file", default="", metavar="PATH",
                              help="Read context from a file")
    infer_parser.add_argument("--capability", "-c", default="",
                              help="Required capability (hard filter)")
    infer_parser.add_argument("--model", "-m", default="",
                              help="Require a specific model id")
    infer_parser.add_argument("--task", default="", help="Task description")
    infer_parser.add_argument("--task-id", default="", dest="task_id",
                              help="Fencing task id")
    infer_parser.add_argument("--attempt-id", default="", dest="attempt_id",
                              help="Fencing attempt id")
    infer_parser.add_argument("--classification", default="",
                              help="Data classification (SECRET never leaves "
                                   "the machine)")
    infer_parser.add_argument("--max-tokens", type=int, default=None,
                              dest="max_tokens", help="Output token bound")
    infer_parser.add_argument("--temperature", type=float, default=None,
                              help="Sampling temperature")
    infer_parser.add_argument("--timeout", type=float, default=None,
                              help="Per-request timeout in seconds")
    infer_parser.add_argument("--stream", action="store_true", default=False,
                              help="Stream deltas as they are produced")
    infer_parser.add_argument("--wait", type=float, default=2.0,
                              help="Long-poll wait per stream batch "
                                   "(server mode)")
    infer_parser.add_argument("--no-deterministic", action="store_true",
                              default=False, dest="no_deterministic",
                              help="Refuse the non-neural fallback rung")
    infer_parser.add_argument("--allow-unverified", action="store_true",
                              default=False, dest="allow_unverified",
                              help="Let the router consider unverified models")
    infer_parser.add_argument("--verify", action="store_true", default=False,
                              help="Verify --model before generating")
    infer_parser.add_argument("--no-discover", action="store_true",
                              default=False, dest="no_discover",
                              help="Skip backend discovery")
    infer_parser.add_argument("--strict-neural", action="store_true",
                              default=False, dest="strict_neural",
                              help="Exit non-zero unless a real model "
                                   "answered")
    infer_parser.add_argument("--explain", action="store_true", default=False,
                              help="Print routing, resource, context, ladder "
                                   "and observation detail")
    infer_parser.add_argument("--server", default="", metavar="URL",
                              help="Delegate to a Forge Server (thin client)")
    infer_parser.add_argument("--token", default="",
                              help="Server API key/session token (or "
                                   "FORGE_SERVER_TOKEN)")
    infer_parser.add_argument("--json", action="store_true", default=False,
                              help="Emit machine-readable JSON")
    _add_inference_flags(infer_parser, suppress=False)
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

    # Native Model Runtime commands (model execution infrastructure)
    runtime_parser = subparsers.add_parser(
        "runtime",
        help="Inspect the Forge Native Model Runtime",
        description="Model execution infrastructure: discovery, metadata, "
        "load/unload, health, and resource reporting over explicitly "
        "selected backends. The runtime is not a model and not the AI "
        "Engine; it never fabricates output. Network access stays disabled "
        "unless --allow-network is passed.",
    )

    def _runtime_common(target):
        """Shared runtime flags, accepted before *and* after a subcommand.

        ``argparse.SUPPRESS`` keeps the parent-level value intact when the
        flag is absent from the subcommand, exactly like ``--json`` above.
        """
        target.add_argument(
            "--backend", default=argparse.SUPPRESS,
            help="Select one backend explicitly "
            "(native|ollama|llama_cpp|forge)")
        target.add_argument(
            "--config", default=argparse.SUPPRESS,
            help="Runtime config file (.forge/runtime.yaml|.json)")
        target.add_argument(
            "--model-dir", action="append", default=argparse.SUPPRESS,
            metavar="DIR",
            help="Add an explicit model directory for native discovery "
            "(repeatable)")
        target.add_argument(
            "--allow-network", action="store_true",
            default=argparse.SUPPRESS,
            help="Explicitly permit the runtime to contact a configured "
            "endpoint")
        target.add_argument(
            "--json", action="store_true", default=argparse.SUPPRESS,
            help="Emit machine-readable JSON")
        target.add_argument(
            "--profile", default=argparse.SUPPRESS,
            help="Resource profile: default|g560 "
                 "(default: auto-detect; FORGE_RESOURCE_PROFILE)")

    _runtime_common(runtime_parser)
    runtime_subs = runtime_parser.add_subparsers(dest="runtime_subcommand")
    for _rt_name, _rt_help in (("status", "Runtime status summary (default)"),
                               ("models", "List models the runtime knows"),
                               ("health", "Backend health checks"),
                               ("backends", "Registered execution backends"),
                               ("metrics", "Latency and reliability metrics"),
                               ("test", "Run a real end-to-end self-check"),
                               ("load", "Load a model"),
                               ("unload", "Unload a model")):
        _rt_sub = runtime_subs.add_parser(_rt_name, help=_rt_help)
        _runtime_common(_rt_sub)
        if _rt_name in ("load", "unload"):
            _rt_sub.add_argument(
                "model",
                help="Model id ('<backend>:<name>') or a bare model name")
    runtime_subs.choices["models"].add_argument(
        "--no-discover", dest="discover", action="store_false", default=True,
        help="List only what is already known (no backend query)")
    runtime_subs.choices["health"].add_argument(
        "--offline", action="store_true",
        help="Report last known state without probing backends")
    runtime_subs.choices["status"].add_argument(
        "--probe", action="store_true",
        help="Probe backends while building the status snapshot")

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


    # -- A84: personal-assistant plane (standalone mode) --------------------
    assistant_parser = subparsers.add_parser(
        "assistant",
        help="Personal assistant (A84): ask, sessions, memory, tools, scale",
        description="The persistent-assistant pipeline in standalone mode: "
        "session ledger, continuity, prompt intelligence, memory user "
        "controls, tool registry, research, declared model scale. Task "
        "execution (writes) requires the control plane/API — the CLI keeps "
        "its thin-client promise and answers as proposals here.",
    )
    assistant_parser.add_argument("--db", default="",
                                  help="Assistant state database "
                                       "(default .forge/assistant.db)")
    assistant_parser.add_argument("--project", default="",
                                  help="Memory project scope (default: "
                                       "current directory name)")
    assistant_subparsers = assistant_parser.add_subparsers(
        dest="assistant_subcommand")
    _ask = assistant_subparsers.add_parser(
        "ask", help="Send one message through the assistant pipeline")
    _ask.add_argument("message")
    _ask.add_argument("--session", default="", help="Assistant session id")
    _ask.add_argument("--web", action="store_true",
                      help="Permit live web corroboration for research")
    _sess = assistant_subparsers.add_parser(
        "sessions", help="List durable assistant sessions")
    _sess.add_argument("--limit", type=int, default=20)
    _amem = assistant_subparsers.add_parser(
        "memory", help="Memory user controls (inspect/search/forget/clear)")
    _amem_subs = _amem.add_subparsers(dest="assistant_memory_subcommand")
    _amem_s = _amem_subs.add_parser("search")
    _amem_s.add_argument("query")
    _amem_i = _amem_subs.add_parser("inspect")
    _amem_i.add_argument("--limit", type=int, default=20)
    _amem_f = _amem_subs.add_parser("forget")
    _amem_f.add_argument("memory_id")
    _amem_c = _amem_subs.add_parser("clear")
    _amem_c.add_argument("--confirm", default="",
                         help='Exact phrase: "FORGET EVERYTHING IN THIS SCOPE"')
    assistant_subparsers.add_parser(
        "tools", help="Tool capability registry (descriptions, not permissions)")
    assistant_subparsers.add_parser(
        "scale", help="Declared model scale catalog (never invented)")
    assistant_subparsers.add_parser(
        "learning", help="Pattern graph + learning-layer status")
    assistant_subparsers.add_parser(
        "status", help="Which A84 capabilities are live here (honesty view)")
    for _sub in assistant_subparsers.choices.values():
        _sub.add_argument("--json", action="store_true",
                          default=argparse.SUPPRESS,
                          help="Emit machine-readable JSON")
    for _sub in _amem_subs.choices.values():
        _sub.add_argument("--json", action="store_true",
                          default=argparse.SUPPRESS,
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
    _server_login = server_subs.add_parser(
        "login",
        help="Exchange an API key for a session token "
             "(challenge/response, replay-resistant)")
    _server_login.add_argument("--key", default="",
                               help="API key (fsk_...) or existing token; "
                                    "falls back to FORGE_SERVER_TOKEN")
    _server_login.add_argument("--url", default="",
                               help="Full API base URL "
                                    "(default: built from --host/--port).")
    _server_login.add_argument("--json", action="store_true",
                               help="Emit machine-readable JSON")

    # Fenced-scheduler task record (multi-agent orchestration runs)
    tasks_parser = subparsers.add_parser(
        "tasks",
        help="Inspect the fenced scheduler's durable task record",
        description="List tasks, show one task with its attempts and "
        "events, or stream the monotonically sequenced event log from "
        "the fenced DAG scheduler's store. Runs record here when the "
        "control plane (or any orchestrator with a store) executes "
        "multi-agent plans.",
    )
    tasks_parser.add_argument(
        "--store", default="",
        help=f"Scheduler store file or directory "
             f"(default: {DEFAULT_TASKS_DIR}/)")
    tasks_subparsers = tasks_parser.add_subparsers(dest="tasks_subcommand")
    _tasks_list = tasks_subparsers.add_parser(
        "list", help="List tasks with state and attempts (default)")
    _tasks_list.add_argument("--limit", type=int, default=50)
    _tasks_show = tasks_subparsers.add_parser(
        "show", help="Show one task, its attempts, and recent events")
    _tasks_show.add_argument("task")
    _tasks_events = tasks_subparsers.add_parser(
        "events", help="Stream the event log (newest first)")
    _tasks_events.add_argument("--task", default="",
                               help="Only events for this task id")
    _tasks_events.add_argument("--limit", type=int, default=50)
    for _tasks_sub in (_tasks_list, _tasks_show, _tasks_events):
        _tasks_sub.add_argument("--json", action="store_true",
                                default=argparse.SUPPRESS,
                                help="Emit machine-readable JSON")
        _tasks_sub.add_argument("--store", default=argparse.SUPPRESS,
                                help="Scheduler store file or directory "
                                     f"(default: {DEFAULT_TASKS_DIR}/)")
    tasks_parser.add_argument("--json", action="store_true",
                              default=argparse.SUPPRESS,
                              help="Emit machine-readable JSON")

    # Agent training lab (honest: reports refusals, never fake jobs)
    training_parser = subparsers.add_parser(
        "training",
        help="Inspect and run the agent training lab",
        description="Scan, export, and (with real data, policy consent, "
        "and OPENAI_API_KEY) start fine-tuning for a created agent. "
        "Every refusal — missing key, missing data, policy block — is "
        "reported; a training job is only claimed when one was actually "
        "submitted to the provider.",
    )
    training_subparsers = training_parser.add_subparsers(
        dest="training_subcommand")
    _tr_scan = training_subparsers.add_parser(
        "scan", help="Scan collected examples against the data policy "
        "(default)")
    _tr_scan.add_argument("agent")
    _tr_scan.add_argument("--runs", default="",
                          help="JSON file of real run outcomes "
                               "([{requirement, output, status, task_id}])")
    _tr_export = training_subparsers.add_parser(
        "export", help="Export the dataset with the upload-policy verdict")
    _tr_export.add_argument("agent")
    _tr_export.add_argument("--runs", default="")
    _tr_start = training_subparsers.add_parser(
        "start", help="Upload data and start a fine-tuning job "
                      "(policy-gated, key required)")
    _tr_start.add_argument("agent")
    _tr_start.add_argument("--runs", required=True)
    _tr_start.add_argument("--model", default="gpt-4o-mini")
    _tr_start.add_argument("--authorize", action="store_true",
                           help="Explicit operator authorization for the "
                                "upload (required by the data policy)")
    _tr_job = training_subparsers.add_parser(
        "job", help="Check a fine-tuning job's status")
    _tr_job.add_argument("job_id")
    for _tr in (_tr_scan, _tr_export, _tr_start, _tr_job):
        _tr.add_argument("--json", action="store_true",
                         default=argparse.SUPPRESS,
                         help="Emit machine-readable JSON")
    training_parser.add_argument("--json", action="store_true",
                                 default=argparse.SUPPRESS,
                                 help="Emit machine-readable JSON")

    engineer_parser = subparsers.add_parser(
        "engineer",
        help="A83 engineering platform: plan, approve, execute, probe, boot",
        description="Drive the A83 engineering pipeline. A requirement "
        "becomes a reviewable plan; nothing executes until that plan is "
        "approved, and an edit invalidates the approval.",
    )
    engineer_subs = engineer_parser.add_subparsers(dest="engineer_command")

    _eng_plan = engineer_subs.add_parser(
        "plan", help="Turn a requirement into an editable plan")
    _eng_plan.add_argument("requirement", help="What to build")
    _eng_plan.add_argument("--profile", default="",
                           help="Project profile name (e.g. zeroos)")
    _eng_plan.add_argument("--json", action="store_true",
                           help="Emit the plan as JSON")

    _eng_show = engineer_subs.add_parser("show", help="Show a stored plan")
    _eng_show.add_argument("plan_id")
    _eng_show.add_argument("--json", action="store_true")

    engineer_subs.add_parser("list", help="List stored plans")

    _eng_approve = engineer_subs.add_parser(
        "approve", help="Approve a plan for execution")
    _eng_approve.add_argument("plan_id")
    _eng_approve.add_argument("--actor", default="cli")

    _eng_edit = engineer_subs.add_parser(
        "edit", help="Edit a plan field (drops any approval)")
    _eng_edit.add_argument("plan_id")
    _eng_edit.add_argument("--field", required=True,
                           help="One of: specifications, components, "
                                "decisions, dependencies, tasks, test_plan, "
                                "benchmark_plan, release_plan, open_questions")
    _eng_edit.add_argument("--value", required=True,
                           help="JSON value for the field")
    _eng_edit.add_argument("--summary", default="")
    _eng_edit.add_argument("--actor", default="cli")

    _eng_execute = engineer_subs.add_parser(
        "execute", help="Check out an approved plan and show its frontier")
    _eng_execute.add_argument("plan_id")

    _eng_pipeline = engineer_subs.add_parser(
        "pipeline", help="Run the multi-agent engineering pipeline")
    _eng_pipeline.add_argument("--roles", default="",
                               help="Comma-separated roles to run")
    _eng_pipeline.add_argument("--profile", default="")

    _eng_hw = engineer_subs.add_parser(
        "hardware", help="Probe this machine's hardware support")
    _eng_hw.add_argument("--json", action="store_true")
    _eng_hw.add_argument("--profile", default="")

    _eng_boot = engineer_subs.add_parser(
        "boot", help="Build and boot the artifact under QEMU")
    _eng_boot.add_argument("--profile", default="")
    _eng_boot.add_argument("--no-build", action="store_true",
                           help="Skip the build and boot what exists")
    _eng_boot.add_argument("--scenario", default="",
                           help="Run only this boot scenario")

    _eng_mem = engineer_subs.add_parser(
        "memory", help="Long-term project memory")
    _eng_mem.add_argument("--search", default="")
    _eng_mem.add_argument("--kind", default="")
    _eng_mem.add_argument("--path", default="")
    _eng_mem.add_argument("--add", default="", help="Title for a new entry")
    _eng_mem.add_argument("--kind-value", dest="add_kind", default="decision")
    _eng_mem.add_argument("--detail", default="")
    _eng_mem.add_argument("--source", default="cli")

    engineer_subs.add_parser(
        "tasks", help="Show the task-kind -> model-class routing table")

    _eng_stats = engineer_subs.add_parser(
        "stats", help="Measured model routing statistics")
    _eng_stats.add_argument("--limit", type=int, default=20)

    engineer_subs.add_parser("agents", help="Show the agent roster")

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
        if (getattr(args, "models_subcommand", "") or "") \
                in INFERENCE_MODEL_SUBCOMMANDS:
            raise SystemExit(_run_models_inference(args))
        _run_models(args)

    elif args.command == "infer":
        raise SystemExit(_run_infer(args))

    elif args.command == "runtime":
        raise SystemExit(_run_runtime(args))

    elif args.command == "agents":
        raise SystemExit(_run_agents(args))
    elif args.command == "memory":
        raise SystemExit(_run_memory(args))

    elif args.command == "assistant":
        raise SystemExit(_run_assistant(args))

    elif args.command == "tasks":
        raise SystemExit(_run_tasks(args))

    elif args.command == "training":
        raise SystemExit(_run_training(args))

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

    elif args.command == "engineer":
        raise SystemExit(_run_engineer(args))

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
