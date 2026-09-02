import argparse

from forge.core.supervisor import Supervisor
from forge.core.state import ForgeStatus


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="forge",
        description="Forge AI software engineering system",
    )

    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("status")

    task_parser = subparsers.add_parser("plan")
    task_parser.add_argument("request")

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

        print("Forge Plan")
        print()

        for step in plan:
            print(f"{step.id}. {step.description}")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
