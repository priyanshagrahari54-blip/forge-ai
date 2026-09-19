"""Execute the full specialist fleet against a real model endpoint.

This is the evidence generator for "do all 1,000 agents produce real work?".
It makes no claims of its own: it asks the configured endpoint for real
completions, records every response verbatim (bounded), and reports the
specialists that could not run because no configured model provides the
capability they require.

Usage (after starting a local runtime, e.g. llama.cpp's ``llama-server``)::

    FORGE_LOCAL_MODEL_URL=http://127.0.0.1:8080 \
    FORGE_LOCAL_MODEL_NAME=gemma-3-270m-q4_k_m.gguf \
    .venv/bin/python scripts/run_fleet_real.py --out .forge/fleet-real-run.json

Requires a runtime-verified model: the script stops with an explicit BLOCKED
report instead of pretending the deterministic fallback produced model work.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from forge.agents.execution import AgentRequest                   # noqa: E402
from forge.agents.frontier_fleet import build_frontier_fleet      # noqa: E402
from forge.core.task_engine import TaskEngine, TaskStatus         # noqa: E402
from forge.models.config import FabricConfig                      # noqa: E402
from forge.models.configured_runtime_bridge import (              # noqa: E402
    sync_configured_runtimes)
from forge.models.fabric import ModelFabric                       # noqa: E402
from forge.models.runtime_monitor_service import (                # noqa: E402
    RuntimeMonitorService)

#: A short budget keeps a 1000-specialist sweep affordable on CPU while still
#: requiring the model to produce real tokens.
DEFAULT_MAX_TOKENS = 24


def build_fabric(url: str, model: str, capabilities: str = "",
                 context: int = 8192, timeout: float = 180.0) -> ModelFabric:
    env = {
        "FORGE_LOCAL_MODEL_URL": url,
        "FORGE_LOCAL_MODEL_NAME": model,
        "FORGE_LOCAL_MODEL_CONTEXT": str(context),
    }
    if capabilities:
        env["FORGE_LOCAL_MODEL_CAPABILITIES"] = capabilities
    config = FabricConfig.from_dict({}, env=env)
    return ModelFabric.from_defaults(config)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=os.environ.get("FORGE_LOCAL_MODEL_URL", ""))
    parser.add_argument("--model", default=os.environ.get("FORGE_LOCAL_MODEL_NAME", ""))
    parser.add_argument("--capabilities",
                        default=os.environ.get("FORGE_LOCAL_MODEL_CAPABILITIES", ""))
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--limit", type=int, default=0,
                        help="only run the first N specialists (0 = all)")
    parser.add_argument("--workers", type=int, default=1,
                        help="concurrent requests (the endpoint serves slots)")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    if not (args.url and args.model):
        print("BLOCKED: no self-hosted model endpoint is configured.")
        print("  reason     : FORGE_LOCAL_MODEL_URL and FORGE_LOCAL_MODEL_NAME "
              "are unset")
        print("  implemented: the local-openai provider, capability routing and "
              "runtime verification are in place")
        print("  requirement: start a local OpenAI-compatible runtime (e.g. "
              "llama.cpp llama-server --model <gguf> --port 8080) and set both "
              "variables")
        return 2

    fabric = build_fabric(args.url, args.model, args.capabilities,
                          timeout=args.timeout)
    sync_configured_runtimes(fabric)
    monitor = RuntimeMonitorService(fabric,
                                    state_path=Path(".forge/fleet-run-monitor.json"))
    report = monitor.tick(force=True)
    # A runtime that is already LIVE within its verification TTL is not
    # re-probed, so its state comes from the monitor registry rather than
    # from this tick's results.
    runtime = monitor.registry.maybe_get("local-openai", args.model)
    state = str(getattr(runtime, "state", "unknown"))
    model = fabric.registry.get(args.model)
    verified = bool(model.metadata.get("runtime_verified")) or state == "LIVE"
    probed = [entry for entry in report.get("results", [])
              if entry.get("provider") == "local-openai"]
    print(f"endpoint        : {args.url}")
    print(f"model           : {args.model}")
    print(f"runtime state   : {state}"
          f" (runtime_verified={model.metadata.get('runtime_verified')}, "
          f"probed_this_run={bool(probed)})")
    if not verified:
        reason = probed[0].get("error") if probed else \
            getattr(runtime, "last_reason", "")
        print(f"BLOCKED: the configured model is not runtime-verified "
              f"({reason or 'no probe evidence'}); refusing to report "
              f"model work.")
        return 3

    registry = build_frontier_fleet(
        fabric, minimum_size=1000, max_output_tokens=args.max_tokens,
        temperature=args.temperature)
    names = registry.names()
    if args.limit:
        names = names[: args.limit]
    engine = TaskEngine()
    providers = Counter()
    outcomes = Counter()
    blocked: dict[str, list[str]] = defaultdict(list)
    records: list[dict] = []
    total_tokens = 0

    # Tasks are created up front: the engine is not shared across threads.
    # Every specialist gets the same brief so the recorded outputs can be
    # compared across roles: the answer shows what that specialization does
    # with an identical problem, instead of a permissive "say something".
    scenario = (
        "A small web service is being prepared for production: it must handle "
        "ten times its current traffic, ship safely, and stay secure."
    )
    work = []
    for name in names:
        registration = registry.get(name)
        task = engine.add("real-" + name,
                          f"{scenario} Acting as the {registration.role} "
                          f"specialist, state the first concrete action you "
                          f"would take and why.")
        work.append((name, registration, task))

    def run_one(item):
        name, registration, task = item
        try:
            return name, registration, registration.executor.execute(
                AgentRequest(task, TaskStatus.CODING))
        except Exception as exc:                          # noqa: BLE001
            return name, registration, exc

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for index, (name, registration, response) in enumerate(
                pool.map(run_one, work), 1):
            if isinstance(response, Exception):
                outcomes["exception"] += 1
                blocked[type(response).__name__].append(name)
                records.append({
                    "agent": name, "role": registration.role,
                    "required_capability":
                        registration.executor.required_capabilities[0],
                    "success": False, "provider": "", "model": "",
                    "error": f"{type(response).__name__}: {response}"[:300],
                    "output": "",
                })
                continue
            if response.success:
                outcomes["real_output"] += 1
                providers[response.metadata.get("routed_provider", "?")] += 1
            else:
                required = registration.executor.required_capabilities[0]
                outcomes["capability_blocked"] += 1
                blocked[required].append(name)
            output = str(response.output or "")
            total_tokens += max(1, len(output) // 4)
            records.append({
                "agent": name,
                "role": registration.role,
                "required_capability":
                    registration.executor.required_capabilities[0],
                "success": bool(response.success),
                "provider": response.metadata.get("routed_provider", ""),
                "model": response.metadata.get("routed_model", ""),
                "error": str(response.error or "")[:300],
                "output": output[:1200],
            })
            if index % 100 == 0:
                print(f"  ... {index}/{len(names)} "
                      f"({outcomes['real_output']} real, "
                      f"{outcomes['capability_blocked']} blocked)", flush=True)

    elapsed = time.perf_counter() - started
    summary = {
        "endpoint": args.url,
        "runtime_state": state,
        "workers": max(1, args.workers),
        "model": args.model,
        "specialists_run": len(names),
        "real_output": outcomes["real_output"],
        "capability_blocked": outcomes["capability_blocked"],
        "exceptions": outcomes["exception"],
        "blocked_by_capability": {key: len(value)
                                  for key, value in sorted(blocked.items())},
        "routed_provider_counts": dict(providers),
        "wall_seconds": round(elapsed, 2),
        "approx_output_tokens": total_tokens,
        "verified_outputs": sum(1 for row in records
                                if row["success"] and row["output"].strip()),
        "empty_outputs": sum(1 for row in records
                             if row["success"] and not row["output"].strip()),
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"summary": summary, "records": records},
                                   indent=2), encoding="utf-8")
        print(f"wrote {path} ({len(records)} records)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
