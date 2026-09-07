import argparse
import json

from forge.core.supervisor import Supervisor
from forge.intelligence.analyzer import ProjectAnalyzer
from forge.intelligence.report import generate_report
from forge.self_development import ForgeSelfAnalyzer, SelfDevelopmentLoop


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
        # real model is reachable.
        from forge.models import ModelRequest
        response = fabric.generate(ModelRequest(prompt="reply ok", capability="coding"))
        if args.json:
            print(json.dumps(response.to_dict(), indent=2))
        else:
            print("Model Fabric self-test")
            print(f"  success={response.success} model={response.model or '-'} provider={response.provider or '-'}")
            if response.error:
                print(f"  error={response.error}")
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


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="forge",
        description="Forge AI software engineering system",
    )

    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("status")

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
    models_subparsers.add_parser("list", help="List registered models (default)")
    models_subparsers.add_parser("health", help="Show model/provider health")
    models_subparsers.add_parser("providers", help="Show registered providers")
    models_subparsers.add_parser("capabilities", help="Show the capability vocabulary")
    models_subparsers.add_parser("test", help="Run a bounded local self-check")
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

    elif args.command == "analyze":
        analyzer = ProjectAnalyzer(".")
        analysis = analyzer.analyze()
        print(generate_report(analysis))

    elif args.command == "models":
        _run_models(args)

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

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
