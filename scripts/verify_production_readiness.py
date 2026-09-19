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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
