import argparse

from forge.core.supervisor import Supervisor
from forge.intelligence.analyzer import ProjectAnalyzer
from forge.intelligence.report import generate_report


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="forge",
        description="Forge AI software engineering system",
    )

    subparsers = parser.add_subparsers(
        dest="command"
    )

    subparsers.add_parser("status")

    task_parser = subparsers.add_parser("plan")
    task_parser.add_argument("request")

    subparsers.add_parser("analyze")

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("requirement", help="Requirement description to run")

    args = parser.parse_args()

    if args.command == "status":

        supervisor = Supervisor("forge-ai")

        print("Forge AI")
        print("Version: 0.1.0")
        print(
            f"Project: "
            f"{supervisor.state.project_name}"
        )
        print(
            f"Status: "
            f"{supervisor.state.status.value}"
        )
        print(
            f"Iteration: "
            f"{supervisor.state.iteration}"
        )

    elif args.command == "plan":

        supervisor = Supervisor("forge-ai")

        plan = supervisor.create_plan(
            args.request
        )

        print("Forge Plan")
        print()

        for step in plan:
            print(
                f"{step.id}. "
                f"{step.description}"
            )

    elif args.command == "analyze":

        analyzer = ProjectAnalyzer(".")
        analysis = analyzer.analyze()

        print(
            generate_report(analysis)
        )

    elif args.command == "run":

        supervisor = Supervisor("forge-ai", root=".")
        result = supervisor.run_task(args.requirement)

        print("=== Forge AI Execution Summary ===")
        print(f"Task ID: {result.task_id}")
        print(f"Requirement: {result.requirement}")
        print(f"Final State: {result.final_state}")
        print(f"Model Selected: {result.selected_model}")
        if result.plan:
            print(f"Planned Agents: {', '.join(a.registration.name for a in result.plan.agents)}")
        print("--- Verification Results ---")
        for vr in result.verification_results:
            status_str = "PASS" if vr.success else "FAIL"
            print(f" [{status_str}] Stage: {vr.stage} | Findings: {vr.findings} | Errors: {vr.errors}")
        if result.errors:
            print(f"Errors: {', '.join(result.errors)}")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
