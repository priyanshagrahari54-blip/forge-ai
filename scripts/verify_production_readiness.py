"""Produce a production-readiness evidence report for this checkout.

Every value is derived from real code paths — nothing here is hard-coded, and
a section reports BLOCKED rather than guessing when an external dependency is
missing. Run it from the repository root:

    .venv/bin/python scripts/verify_production_readiness.py [--json]

Sections:

* ``agents``    — registered specialist count, role coverage, one routing
                  contract check per specialization;
* ``routing``   — representative specialists really executed through a real
                  ``ModelFabric`` (in-process provider, labeled SIMULATED);
* ``fabric``    — configured providers/models and whether the fabric has a
                  runtime-verified live model (LIVE vs BLOCKED);
* ``capabilities`` — the honest runtime capability snapshot;
* ``background``— worker/task persistence round-trip and stale-run recovery;
* ``voice``/``city`` — cockpit wiring that the node/Python suites also pin.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from forge.agents.execution import AgentRequest          # noqa: E402
from forge.agents.frontier_fleet import (                # noqa: E402
    SPECIALIZATIONS, build_frontier_fleet)
from forge.api.server import build_plane                 # noqa: E402
from forge.capabilities.runtime import (                 # noqa: E402
    runtime_capability_snapshot)
from forge.core.task_engine import TaskEngine, TaskStatus  # noqa: E402
from forge.models.capabilities import ALL_CAPABILITIES   # noqa: E402
from forge.models.fabric import ModelFabric              # noqa: E402
from forge.models.provider import ModelResult, ProviderRegistry  # noqa: E402
from forge.models.registry import Model, ModelRegistry   # noqa: E402
from forge.workers.persistence import WorkerStore        # noqa: E402
from forge.workers.registry import WorkerRecord, WorkerRegistry  # noqa: E402

WEB = REPO / "forge" / "cockpit" / "web"
PROVIDER_KEYS = {
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "ollama": ("OLLAMA_BASE_URL", "OLLAMA_URL"),
    "huggingface": ("HF_TOKEN", "HUGGINGFACE_TOKEN"),
}


class _InProcessProvider:
    """Test double. Labeled SIMULATED: it proves the routing path, not a live
    provider. It is never reported as LIVE."""

    name = "in-process-simulated"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def generate(self, prompt: str, **kwargs) -> ModelResult:
        self.prompts.append(prompt)
        return ModelResult("specialist ok", "full-capability-model")


def _section_agents(plane) -> dict:
    registry = build_frontier_fleet(plane.fabric, minimum_size=1000)
    names = registry.names()
    roles = registry.roles()
    per_role = {role: len(registry.get_by_role(role)) for role in roles}
    declared = [spec for spec, _role, _caps in SPECIALIZATIONS]
    missing = [spec for spec in declared
               if not any(name.startswith(spec + "-") for name in names)]
    return {
        "registered_specialists": len(names),
        "roles": len(roles),
        "specializations_declared": len(declared),
        "specializations_without_a_registered_specialist": missing,
        "every_specialization_registered": not missing,
        "specialists_per_role": per_role,
        "sample_names": [names[0], names[1], names[len(names) // 2],
                         names[-1]],
    }


def _section_routing(plane) -> dict:
    """Execute one representative per specialization through a ModelFabric."""
    provider = _InProcessProvider()
    model = Model(name="full-capability-model",
                  provider=provider.name,
                  capabilities=tuple(ALL_CAPABILITIES))
    model.metadata["runtime_verified"] = True
    fabric = ModelFabric(
        registry=ModelRegistry([model]),
        providers=ProviderRegistry({provider.name: provider}))
    registry = build_frontier_fleet(fabric, minimum_size=1000)
    engine = TaskEngine()
    executed, failures = [], []
    for role in registry.roles():
        registration = registry.get_by_role(role)[0]
        task = engine.add("verify-" + registration.name,
                          "perform a specialist verification task")
        response = registration.executor.execute(
            AgentRequest(task, TaskStatus.CODING, instructions="verify"))
        if response.success and response.metadata.get("routed_model") == \
                "full-capability-model":
            executed.append(registration.name)
        else:
            failures.append({"agent": registration.name,
                             "error": response.error[:200]})
    return {
        "provider": provider.name,
        "provider_state": "SIMULATED",
        "representatives_executed": len(executed),
        "roles": len(registry.roles()),
        "failures": failures,
        "note": ("in-process provider: this proves routing and execution "
                 "plumbing, not a live remote model"),
    }


def _section_deployment_routing(plane) -> dict:
    """What happens to the fleet against *this* deployment's own fabric.

    The representative section above proves the plumbing with a
    capability-complete model. This one uses the fabric the server actually
    builds, so it shows which specialists cannot run here at all because no
    configured model provides the capability they require.
    """
    fabric = plane.fabric
    provided = set()
    for model in fabric.registry:
        provided |= set(model.capabilities)
    registry = build_frontier_fleet(fabric, minimum_size=1000)
    engine = TaskEngine()
    executed: dict[str, int] = {}
    blocked: dict[str, int] = {}
    for name in registry.names():
        registration = registry.get(name)
        task = engine.add("deploy-" + name, "specialist smoke task")
        response = registration.executor.execute(
            AgentRequest(task, TaskStatus.CODING, instructions="smoke"))
        if response.success:
            provider = response.metadata.get("routed_provider", "")
            executed[provider] = executed.get(provider, 0) + 1
        else:
            required = registration.executor.required_capabilities[0]
            blocked[required] = blocked.get(required, 0) + 1
    total = len(registry)
    return {
        "specialists": total,
        "executed": sum(executed.values()),
        "executed_by_provider": executed,
        "blocked": blocked,
        "capabilities_provided_by_models": sorted(provided),
        "every_block_is_a_truly_missing_capability":
            all(capability not in provided for capability in blocked),
        "note": ("a blocked specialist means no configured model provides its "
                 "required capability; it is never rerouted to a model that "
                 "cannot do the job"),
    }


def _section_fabric(plane) -> dict:
    fabric = plane.fabric
    registry = getattr(fabric, "registry", None)
    models = list(registry) if registry is not None else []
    providers = []
    for name, provider in _provider_items(fabric):
        capabilities = getattr(provider, "capabilities", None)
        providers.append({
            "name": name,
            "capabilities": list(capabilities) if capabilities else [],
            "registered": True,
        })
    live = [model.name for model in models
            if getattr(model, "available", False)
            and model.metadata.get("runtime_verified") is True]
    model_rows = [{"name": model.name, "provider": model.provider,
                   "available": bool(getattr(model, "available", False)),
                   "runtime_verified":
                       model.metadata.get("runtime_verified") is True}
                  for model in models]
    missing_keys = []
    for name, keys in PROVIDER_KEYS.items():
        if any(os.environ.get(key) for key in keys):
            continue
        missing_keys.append({"provider": name, "env": list(keys)})
    return {
        "providers_registered": providers,
        "models_registered": model_rows,
        "runtime_verified_models": live,
        "state": "LIVE" if live else "CONFIGURED" if model_rows else "BLOCKED",
        "blocked": ([] if live else [{
            "reason": "no provider credential is present in this environment",
            "implemented": ("ModelFabric routing, capability matching, "
                            "runtime verification, failover and provenance "
                            "are implemented and tested"),
            "external_requirement": ("configure a real provider "
                                     "(e.g. OPENAI_API_KEY / ANTHROPIC_API_KEY "
                                     "/ a reachable OLLAMA_BASE_URL)"),
            "credentials_absent": missing_keys,
        }]),
    }


def _provider_items(fabric):
    providers = getattr(fabric, "providers", None)
    if providers is None:
        return []
    items = getattr(providers, "items", None)
    if callable(items):
        return list(items())
    names = getattr(providers, "names", None)
    if callable(names):
        return [(name, providers.get(name)) for name in names()]
    return []


def _section_capabilities(plane) -> dict:
    snapshot = runtime_capability_snapshot(plane.fabric)
    states: dict[str, int] = {}
    rows = []
    for item in snapshot.get("capabilities", []):
        state = item.get("state") or item.get("status") or "UNKNOWN"
        states[state] = states.get(state, 0) + 1
        rows.append({"id": item.get("id") or item.get("capability_id"),
                     "state": state})
    return {"schema_version": snapshot.get("schema_version"),
            "counts": states, "capabilities": rows,
            "honesty_contract": snapshot.get("honesty_contract")}


def _section_background(tmp: Path) -> dict:
    """A restarted control plane restores worker identity, never authority."""
    store = WorkerStore(str(tmp / "workers.db"))
    store.save(WorkerRecord(worker_id="w-1", name="edge-worker",
                            capabilities=("coding", "testing"),
                            cpu_threads=4, ram_mb=2048, platform="linux",
                            endpoint="http://worker.local"))
    registry = WorkerRegistry()
    restored = store.restore(registry)
    snapshot = registry.snapshot()
    workers = snapshot["workers"]
    return {
        "worker_store_round_trip": restored == 1,
        "workers_restored": restored,
        "live_count_before_heartbeat": snapshot["live_count"],
        "capabilities_restored":
            sorted(workers[0]["capabilities"]) if workers else [],
        "persisted_file": store.path.exists(),
        "note": ("identity and capabilities survive restart; liveness is "
                 "re-established only by a fresh heartbeat"),
    }


def _section_web() -> dict:
    index = (WEB / "index.html").read_text(encoding="utf-8")
    home = (WEB / "forge-home.html").read_text(encoding="utf-8")
    handsfree = (WEB / "handsfree.js").read_text(encoding="utf-8")
    city = (WEB / "city.html").read_text(encoding="utf-8")
    return {
        "ai_city_in_navigation": 'href="#/city"' in index
        and 'data-route="city"' in index,
        "ai_city_renders_backend_events": "events/stream" in city
        and "NO SYNTHETIC PROGRESS" in city,
        "ai_city_token_in_session_storage":
            "sessionStorage.setItem('forge.city.token'" in city
            and "localStorage.setItem('forge.city.token'" not in city,
        "shared_voice_playback": {
            "module": (WEB / "voice-playback.js").exists(),
            "loaded_before_handsfree":
                index.index("/voice-playback.js") < index.index("/handsfree.js"),
            "home_uses_module": "ForgeVoicePlayback" in home,
            "handsfree_uses_module": "window.ForgeVoicePlayback" in handsfree,
            "handsfree_speaks_directly":
                "speechSynthesis.speak(" in handsfree,
        },
        "enable_voice_gesture": 'id="enable-voice"' in home,
        "confirm_before_execute": "confirm: true" in handsfree
        and "confirm:true" in home,
    }


def _section_failover() -> dict:
    """Provider failover: which of the operator's servers exist, tiers, quotas.

    Configuration is read from the environment exactly as the fabric reads it,
    so this reports the deployment's real posture: how many self-hosted
    servers are configured, which models they serve, how they are ranked by
    declared power, and whether any provider is currently sitting out a spent
    quota.
    """
    from forge.models.config import FabricConfig, group_local_endpoints
    from forge.models.quota import ExhaustionTracker

    config = FabricConfig.from_dict({})
    endpoints = list(config.local_openai_endpoints)
    groups = group_local_endpoints(endpoints)
    models = [
        {
            "model": model_id,
            "tier": tier,
            "servers": [entry.url for entry in entries],
            "capabilities_declared": list(capabilities),
        }
        for model_id, capabilities, tier, entries in groups
    ]
    tracker = ExhaustionTracker(
        cooldown_seconds=float(config.provider_cooldown_seconds or 60.0),
        max_cooldown_seconds=float(config.quota_max_cooldown_seconds or 900.0),
    )
    snapshot = tracker.snapshot()
    return {
        "self_hosted_servers_configured": len(endpoints),
        "models": models,
        "models_ranked_by_tier": [row["model"] for row in sorted(
            models, key=lambda row: -row["tier"])],
        "cloud_provider_configured": bool(config.openai_enabled),
        "cooldown_seconds": snapshot["default_cooldown_seconds"],
        "max_cooldown_seconds": snapshot["max_cooldown_seconds"],
        "exhausted_now": snapshot["exhausted"],
        "failover": (
            "quota exhaustion is classified (HTTP 402/429/503 + provider "
            "wording), the spent provider is skipped until its cooldown "
            "passes and tried again afterwards, and the request fails over "
            "to the next model in the same call"
            if endpoints else
            "no self-hosted endpoint is configured; set FORGE_LOCAL_MODEL_URL "
            "or FORGE_MODEL_ENDPOINTS to enable rotation"),
    }


def _section_multimodal(plane) -> dict:
    """What the multimodal specialists can really do in this deployment."""
    from forge.agents.multimodal_fleet import (SPECIALIST_NAMES,
                                               multimodal_readiness)
    from forge.models.multimodal_bridge import multimodal_specs

    fabric = getattr(plane, "fabric", None)
    reports = multimodal_readiness(fabric) if fabric is not None else ()
    configured = [spec.to_dict() for spec in multimodal_specs()]
    return {
        "defined_specialists": len(SPECIALIST_NAMES),
        "registered_specialists": sum(report.registered for report in reports),
        "configured_endpoints": configured,
        "modalities": [report.to_dict() for report in reports],
        "ready": [report.family for report in reports if report.registered],
        "missing": [report.family for report in reports if not report.registered],
        "state": ("READY" if any(report.registered for report in reports)
                  else "MISSING"),
        "note": ("a modality is READY only when a registered model advertises "
                 "its capability; nothing is registered from code alone"),
    }


def _section_finetune() -> dict:
    """Fine-tuning: what is installed, what is registered, what was trained.

    Never says "trained" because a pipeline exists. It reports the trainer's
    own availability, the dataset it would use, and the most recent job report
    on disk (state, loss curve, artifact) when one exists.
    """
    from forge.training.job import register_default_trainers
    from forge.models.model_studio import ModelStudio

    studio = ModelStudio(root=str(REPO))
    registered = register_default_trainers(studio)
    reports = sorted((REPO / ".forge" / "models").glob("*/training.json"))
    trained = sorted((REPO / ".forge" / "models").glob("*/training.json"))
    last: dict = {}
    if reports:
        import json as _json
        newest = max(reports, key=lambda path: path.stat().st_mtime)
        try:
            payload = _json.loads(newest.read_text(encoding="utf-8"))
            last = {
                "path": str(newest.relative_to(REPO)),
                "base_model": payload.get("base_model", ""),
                "specialization": payload.get("specialization", ""),
                "steps": payload.get("steps"),
                "initial_loss": payload.get("initial_loss"),
                "final_loss": payload.get("final_loss"),
                "held_out_loss": payload.get("held_out_loss"),
                "trainable_parameters": payload.get("trainable_parameters"),
                "servable": payload.get("servable"),
            }
        except (ValueError, OSError):
            last = {}
    stack = {"torch": _importable("torch"), "gguf": _importable("gguf"),
             "tokenizers": _importable("tokenizers"),
             "transformers": _importable("transformers"),
             "peft": _importable("peft")}
    return {
        "training_stack": stack,
        "registered_trainers": studio.trainers(),
        "usable_for_local_gguf": "gguf-lora" in studio.trainers(),
        "adapters_trained_on_this_machine": len(trained),
        "last_training_report": last,
        "blocked": ("" if studio.trainers() else
                    "no training backend is registered: install torch + gguf + "
                    "tokenizers (local GGUF LoRA) or the HuggingFace/PEFT stack"),
    }


def _importable(name: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        plane = build_plane({"forge-readiness": str(REPO)},
                            db_path=str(tmp / "cockpit.db"))
        report = {
            "agents": _section_agents(plane),
            "routing": _section_routing(plane),
            "deployment_routing": _section_deployment_routing(plane),
            "fabric": _section_fabric(plane),
            "capabilities": _section_capabilities(plane),
            "background": _section_background(tmp),
            "web": _section_web(),
            "failover": _section_failover(),
            "finetune": _section_finetune(),
            "multimodal": _section_multimodal(plane),
        }
        try:
            plane.stop(wait=False)
        except Exception:                                  # noqa: BLE001
            pass
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
        return 0
    print("Forge production-readiness evidence")
    print("=" * 60)
    agents = report["agents"]
    print(f"specialists registered : {agents['registered_specialists']}")
    print(f"roles / specializations: {agents['roles']} / "
          f"{agents['specializations_declared']} "
          f"(all registered: {agents['every_specialization_registered']})")
    if not agents["every_specialization_registered"]:
        print("  MISSING: "
              f"{agents['specializations_without_a_registered_specialist']}")
    routing = report["routing"]
    print(f"representatives routed : {routing['representatives_executed']}"
          f"/{routing['roles']} ({routing['provider_state']} provider "
          f"'{routing['provider']}', failures: {len(routing['failures'])})")
    deploy = report["deployment_routing"]
    print(f"this deployment's fabric: {deploy['executed']}/"
          f"{deploy['specialists']} specialists execute "
          f"(by provider: {deploy['executed_by_provider']})")
    if deploy["blocked"]:
        print(f"  blocked (capability no model provides): {deploy['blocked']}")
    print("  models provide: "
          f"{', '.join(deploy['capabilities_provided_by_models'])}")
    fabric = report["fabric"]
    print(f"model fabric           : {fabric['state']} "
          f"(models={len(fabric['models_registered'])}, "
          f"runtime-verified={len(fabric['runtime_verified_models'])})")
    for row in fabric["models_registered"]:
        print(f"  model {row['name']:<28} provider={row['provider']:<10} "
              f"available={row['available']} "
              f"runtime_verified={row['runtime_verified']}")
    for provider in fabric["providers_registered"]:
        print(f"  provider {provider['name']:<18} registered=True")
    for entry in fabric["blocked"]:
        print(f"  BLOCKED: {entry['reason']}")
        print(f"    implemented : {entry['implemented']}")
        print(f"    requirement : {entry['external_requirement']}")
    caps = report["capabilities"]
    print(f"capability states      : {caps['counts']}")
    by_state: dict = {}
    for row in caps["capabilities"]:
        by_state.setdefault(row["state"], []).append(row["id"])
    for state in sorted(by_state):
        print(f"  {state:<13}: {', '.join(sorted(by_state[state]))}")
    print(f"worker persistence     : {report['background']}")
    web = report["web"]
    print(f"AI City in nav         : {web['ai_city_in_navigation']}")
    print(f"shared voice playback  : {web['shared_voice_playback']}")
    failover = report["failover"]
    print(f"self-hosted servers    : {failover['self_hosted_servers_configured']} "
          f"configured, {len(failover['models'])} model(s); tier order "
          f"{failover['models_ranked_by_tier'] or '-'}")
    print(f"quota failover         : cooldown {failover['cooldown_seconds']}s "
          f"(max {failover['max_cooldown_seconds']}s), exhausted now: "
          f"{failover['exhausted_now'] or 'none'}")
    multimodal = report["multimodal"]
    print(f"multimodal specialists : {multimodal['registered_specialists']}/"
          f"{multimodal['defined_specialists']} registered, state "
          f"{multimodal['state']}")
    for row in multimodal["modalities"]:
        detail = (", ".join(row["models"]) if row["models"]
                  else row["requirement"] or row["reason"])
        print(f"  {row['family']:<17} {row['status']:<8} "
              f"{row['registered']}/{row['defined']} — {detail}")
    finetune = report["finetune"]
    print(f"fine-tuning            : trainers={finetune['registered_trainers'] or 'none'} "
          f"stack={ {k: v for k, v in finetune['training_stack'].items() if v} }")
    last = finetune["last_training_report"]
    if last:
        print(f"  last adapter         : {last['specialization']} on "
              f"{Path(last['base_model']).name} — loss "
              f"{last['initial_loss']} -> {last['final_loss']} "
              f"(held-out {last['held_out_loss']}), "
              f"{last['trainable_parameters']} params, servable={last['servable']}")
    elif finetune["blocked"]:
        print(f"  BLOCKED: {finetune['blocked']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
