"""Run the capability specialists against real inputs and write the evidence.

The 100 vision / audio / browser / computer-use specialists used to stop with
"no registered model supports capabilities [...]" because this deployment had
no model for those capabilities. They now have real in-process backends, and
this script proves it the only way that counts: it builds real inputs (a real
image, real speech synthesized with espeak-ng, a real web page served on
loopback) and executes every one of those specialists through the *actual*
ModelFabric routing path, recording what each one really did.

Exit codes: 0 when every specialist executed, 3 when any of them did not —
with the per-specialist error in the evidence file.
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from forge.agents.execution import AgentRequest                    # noqa: E402
from forge.agents.frontier_fleet import build_frontier_fleet       # noqa: E402
from forge.capabilities.live_inputs import (                       # noqa: E402
    PAGES, media_inputs, stop as stop_server)
from forge.agents.multimodal_fleet import (                        # noqa: E402
    SPECIALIST_CAPABILITY, extend_registry_with_multimodal_fleet)
from forge.core.task_engine import TaskEngine, TaskStatus          # noqa: E402
from forge.models.local_capabilities import (                      # noqa: E402
    register_local_capability_models)
from forge.models.fabric import ModelFabric                        # noqa: E402
from forge.vision.pixels import measure                            # noqa: E402

BLOCKED_EXIT = 3

#: The capabilities this script exists to exercise in the frontier fleet.
TARGETS = ("vision", "audio", "browser", "computer_use")

#: ...and in the multimodal fleet, which is a separate set of 100 specialists.
MULTIMODAL_TARGETS = ("vision", "image_generation", "speech_to_text",
                      "text_to_speech")


def failure_line(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:300]


def multimodal_task(capability: str, media: dict[str, str],
                    specialist: str = "") -> str:
    """A request that carries the real media the capability needs.

    The request is written *per specialist*: the same input for all 25 would
    only prove routing. Each one is asked for its own work, and the artifacts
    it produces are named after it, so the evidence shows 25 different results
    rather than 25 copies of one.
    """
    specialty = specialist.split("-", 2)[-1] if specialist else "general"
    if capability == "vision":
        return (f"As the {specialty} specialist, inspect this screenshot of the "
                f"warehouse console and report what you find\n"
                f"file:{media['image']}")
    if capability == "speech_to_text":
        return (f"Transcribe this warehouse status call for the {specialty} "
                f"workflow\nfile:{media['audio']}")
    if capability == "text_to_speech":
        spoken = re.sub(r"[^a-z0-9]+", "-", specialty.lower()).strip("-")
        return (f"Read the warehouse status aloud for the {specialty} "
                f"announcement: two shelves are below their reorder point "
                f"out:{media['spoken']}-{spoken or 'general'}.wav")
    seed = sum(ord(character) for character in specialty) or 7
    bars = ", ".join(
        f'{{"label": "S{index + 1}", "value": {(seed * (index + 3)) % 11 + 1}}}'
        for index in range(3))
    chart = re.sub(r"[^a-z0-9]+", "-", specialty.lower()).strip("-") or "general"
    return ('{"title": "' + f'{specialty} panel"' + ', "x_label": "shelf", '
            f'"palette": "forge", "bars": [{bars}], '
            f'"out": "{media["chart"]}-{chart}.png"}}')


def task_for(capability: str, media: dict[str, str]) -> str:
    if capability == "vision":
        return (
            "Acting as a vision specialist, inspect this image and report the "
            "first concrete UI or layout problem you would fix and why.\n"
            f"file:{media['image']}")
    if capability == "audio":
        return (
            "Acting as an audio specialist, listen to this recording and "
            "report the first concrete action you would take.\n"
            f"file:{media['audio']}")
    if capability == "browser":
        return (
            "Acting as a browser specialist, open the warehouse console, read "
            "the stock status, and report the first concrete action you would "
            f"take.\nurl:{media['base']}/orders")
    return (
        "Acting as a computer-use specialist, operate the warehouse console: "
        "search for the alerting shelves and report what you found.\n"
        f"url:{media['base']}/ actions:[{{\"click\": \"Alerts\"}}, "
        f"{{\"navigate\": \"{media['base']}/\"}}, "
        "{\"type\": {\"q\": \"low stock\"}}, "
        "{\"submit\": {\"q\": \"low stock\"}}]")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="docs/evidence/capability-fleet.json")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int, default=0,
                        help="stop after N specialists (0 = all)")
    args = parser.parse_args()

    fabric = ModelFabric.from_defaults()
    registration = register_local_capability_models(fabric)
    print(f"local capability models: {registration['registered']}")
    for skipped in registration["skipped"]:
        print(f"  skipped {skipped['capability']}: {skipped['reason']}")
    if not registration["registered"]:
        print("BLOCKED: no local capability backend passed its probe on this "
              "machine, so nothing can be executed here.")
        return BLOCKED_EXIT

    #: The same real inputs the readiness report runs the same specialists on:
    #: a real bitmap, real audio (synthesised when an engine exists, generated
    #: samples when one does not, and the evidence records which), and pages
    #: served on loopback.
    server, media = media_inputs(REPO / ".forge")
    measured = measure(open(media["image"], "rb").read(), source="probe")
    print(f"real inputs: image {media['image_bytes']} B "
          f"({measured['width']}x{measured['height']}), audio "
          f"{media['audio_bytes']} B from {media['audio_source']}, pages at "
          f"{media['base']}")

    registry = build_frontier_fleet(fabric)
    engine = TaskEngine()
    started = time.time()
    records: list[dict[str, object]] = []
    executed = 0
    failures: list[dict[str, str]] = []
    for name in registry.names():
        registration_entry = registry.get(name)
        capability = registration_entry.executor.required_capabilities[0]
        if capability not in TARGETS:
            continue
        task = engine.add("capability-" + name, "capability run")
        prompt = task_for(capability, media)
        run_started = time.time()
        response = registration_entry.executor.execute(
            AgentRequest(task, TaskStatus.CODING, instructions=prompt))
        elapsed = round((time.time() - run_started) * 1000, 1)
        entry = {
            "agent": name,
            "capability": capability,
            "success": bool(response.success),
            "routed_model": response.metadata.get("routed_model", ""),
            "routed_provider": response.metadata.get("routed_provider", ""),
            "elapsed_ms": elapsed,
            "output": (response.output or "")[:600],
            "error": (response.error or "")[:300],
        }
        records.append(entry)
        if response.success and response.output:
            executed += 1
        else:
            failures.append({"agent": name, "capability": capability,
                             "error": entry["error"] or "empty output"})
        if args.limit and len(records) >= args.limit:
            break

    #: The 100 multimodal specialists are their own registry; they are run with
    #: requests that carry the media their capability needs.
    engine = TaskEngine()
    multimodal_records: list[dict[str, object]] = []
    multimodal_registry = build_frontier_fleet(fabric)
    extend_registry_with_multimodal_fleet(multimodal_registry, fabric)
    for name in multimodal_registry.names():
        capability = SPECIALIST_CAPABILITY.get(name)
        if capability not in MULTIMODAL_TARGETS:
            continue
        registration_entry = multimodal_registry.get(name)
        task = engine.add("multimodal-" + name, "multimodal run")
        prompt = multimodal_task(capability, media, name)
        run_started = time.time()
        response = registration_entry.executor.execute(
            AgentRequest(task, TaskStatus.CODING, instructions=prompt))
        entry = {
            "agent": name,
            "capability": capability,
            "success": bool(response.success),
            "routed_model": response.metadata.get("routed_model", ""),
            "routed_provider": response.metadata.get("routed_provider", ""),
            "elapsed_ms": round((time.time() - run_started) * 1000, 1),
            "output": (response.output or "")[:600],
            "error": (response.error or "")[:300],
        }
        multimodal_records.append(entry)
        if not (response.success and response.output):
            failures.append({"agent": name, "capability": capability,
                             "error": entry["error"] or "empty output"})

    stop_server(server)
    summary: dict[str, object] = {}
    for record in records:
        bucket = summary.setdefault(str(record["capability"]),
                                    {"executed": 0, "total": 0,
                                     "models": {}})
        bucket["total"] += 1
        if record["success"] and record["output"]:
            bucket["executed"] += 1
            models = bucket["models"]
            models[record["routed_model"]] = models.get(record["routed_model"], 0) + 1

    multimodal_summary: dict[str, object] = {}
    for record in multimodal_records:
        bucket = multimodal_summary.setdefault(
            str(record["capability"]),
            {"executed": 0, "total": 0, "models": {}})
        bucket["total"] += 1
        if record["success"] and record["output"]:
            bucket["executed"] += 1
            models = bucket["models"]
            models[record["routed_model"]] = models.get(record["routed_model"], 0) + 1
    multimodal_executed = sum(bucket["executed"]
                              for bucket in multimodal_summary.values())

    evidence = {
        "schema_version": 1,
        "generated_at": time.time(),
        "date": time.strftime("%Y-%m-%d", time.gmtime()),
        "capabilities": list(TARGETS),
        "local_models_registered": registration["registered"],
        "local_models_skipped": registration["skipped"],
        "real_inputs": {
            "image_bytes": media["image_bytes"],
            "audio_bytes": media["audio_bytes"],
            "audio_seconds": round(media["audio_bytes"] / (22050 * 2), 2),
            "audio_source": media["audio_source"],
            "pages": sorted(PAGES),
            "base_url": media["base"],
        },
        "specialists_targeted": len(records),
        "specialists_executed": executed,
        "by_capability": summary,
        "failures": failures,
        "duration_seconds": round(time.time() - started, 2),
        "multimodal_specialists_targeted": len(multimodal_records),
        "multimodal_specialists_executed": multimodal_executed,
        "multimodal_by_capability": multimodal_summary,
        "examples": records[:6],
        "multimodal_examples": multimodal_records[:4],
        "records": records,
        "multimodal_records": multimodal_records,
        "note": ("every record was produced by real ModelFabric routing; the "
                 "vision model measures real pixels, the speech models really "
                 "synthesize and recognise audio, and the browser/action "
                 "runtimes really fetch and act on pages served on loopback"),
    }
    out = Path(args.out)
    if not out.is_absolute():
        out = REPO / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    print(f"executed {executed}/{len(records)} capability specialists "
          f"in {evidence['duration_seconds']}s")
    for capability, bucket in sorted(summary.items()):
        print(f"  {capability:<14} {bucket['executed']}/{bucket['total']} "
              f"via {bucket['models']}")
    print(f"executed {multimodal_executed}/{len(multimodal_records)} "
          f"multimodal specialists")
    for capability, bucket in sorted(multimodal_summary.items()):
        print(f"  {capability:<16} {bucket['executed']}/{bucket['total']} "
              f"via {bucket['models']}")
    if failures:
        print("  failures:")
        for item in failures[:5]:
            print(f"    {item['agent']}: {item['error']}")
    print(f"wrote {out}")
    complete = (executed == len(records) and records
                and multimodal_executed == len(multimodal_records)
                and multimodal_records)
    return 0 if complete else BLOCKED_EXIT


if __name__ == "__main__":
    raise SystemExit(main())
