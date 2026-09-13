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


def _build_runtime(args):
    """Build the Native Model Runtime from config file / env / CLI overrides.

    The runtime is model *execution infrastructure*: it is separate from the
    Model Fabric (routing) and from the AI Engine (orchestration). Network
    access stays off unless explicitly requested, so these commands are safe
    to run anywhere.
    """
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
    return ModelRuntime.from_defaults(config)


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

    elif args.command == "runtime":
        raise SystemExit(_run_runtime(args))

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
