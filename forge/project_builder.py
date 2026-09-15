"""First-class project-building API for Forge.

The project builder is intentionally thin: the existing Supervisor remains the
single guarded engineering transaction, while this module gives callers one
stable API for the product promise:

    requirement -> real-model preflight -> plan -> code -> test/debug ->
    review -> security -> acceptance -> git commit

No model is invented here. A caller must provide a configured capable
``ModelFabric`` (or the builder will construct the configured default fabric).
Before work starts, the builder performs the same bounded provider-readiness
check used by Forge diagnostics. The deterministic/reference engine can prove
inference infrastructure but is never counted as project-builder readiness.

Python floor: 3.8.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Tuple

from forge.core.supervisor import Supervisor
from forge.security.permissions import OperationMode


@dataclass
class ProjectBuildResult:
    """Stable, bounded summary of one project-build transaction."""

    accepted: bool = False
    project_id: str = ""
    root: str = ""
    requirement: str = ""
    files: Tuple[str, ...] = ()
    model: str = ""
    provider: str = ""
    error: str = ""
    result: Dict[str, Any] = field(default_factory=dict)

    @property
    def success(self) -> bool:
        return self.accepted

    def to_dict(self) -> Dict[str, Any]:
        return {
            "accepted": bool(self.accepted),
            "project_id": self.project_id,
            "root": self.root,
            "requirement": self.requirement,
            "files": list(self.files),
            "model": self.model,
            "provider": self.provider,
            "error": self.error,
            "result": self.result,
        }


class ProjectBuilder:
    """Build real software projects through Forge's guarded loop.

    This class deliberately does not duplicate planning, model routing,
    checkpoints, permissions, testing or acceptance. Those responsibilities
    stay in :class:`forge.core.supervisor.Supervisor`.
    """

    def __init__(self, project_id: str, root: str | Path = ".",
                 *, fabric: Any = None,
                 mode: OperationMode = OperationMode.AUTONOMOUS,
                 max_debug_retries: int = 3) -> None:
        self.project_id = str(project_id or "project")
        self.root = str(Path(root).resolve())
        self.fabric = fabric
        self.mode = OperationMode(mode)
        self.max_debug_retries = max(0, int(max_debug_retries))

    def _fabric_or_default(self) -> Any:
        if self.fabric is not None:
            return self.fabric
        from forge.models.fabric import ModelFabric

        return ModelFabric.from_defaults()

    def preflight(self) -> Dict[str, Any]:
        """Check whether a configured fabric can actually serve a build.

        This is a bounded live readiness check, not merely a registry check.
        A model that is registered but unreachable is not considered ready.
        The returned payload is content-free and safe to expose to operators.
        """
        fabric = self._fabric_or_default()
        try:
            from forge.models.readiness import check_fabric_readiness

            report = check_fabric_readiness(
                fabric, probe_network=True, timeout=5.0)
            payload = report.to_dict()
            payload["reason"] = (
                "real coding-capable model is reachable and usable"
                if report.ready
                else "no working code model is reachable"
            )
            return payload
        except Exception as exc:
            return {
                "ready": False,
                "fallback_only": True,
                "usable_models": [],
                "checks": [],
                "reason": "project-builder preflight failed: %s"
                % str(exc)[:500],
            }

    def build(self, requirement: str, *, approved: bool = False,
              policy: Any = None,
              approval_store: Any = None,
              approval_token_id: str = "",
              approval_callback: Any = None,
              audit_log: Any = None,
              model_policy: Any = None,
              control: Any = None,
              on_event: Any = None) -> ProjectBuildResult:
        """Build one project and return a bounded product-level result."""
        text = str(requirement or "").strip()
        if not text:
            return ProjectBuildResult(
                accepted=False,
                project_id=self.project_id,
                root=self.root,
                requirement=text,
                error="project requirement must not be empty",
            )

        fabric = self._fabric_or_default()
        readiness = self.preflight()
        if not bool(readiness.get("ready")):
            return ProjectBuildResult(
                accepted=False,
                project_id=self.project_id,
                root=self.root,
                requirement=text,
                error=str(readiness.get("reason") or
                            "No working code model is available."),
                result={"preflight": readiness},
            )

        supervisor = Supervisor(self.project_id, root=self.root)
        try:
            report = supervisor.run(
                text,
                approved=bool(approved),
                fabric=fabric,
                max_debug_retries=self.max_debug_retries,
                mode=self.mode,
                policy=policy,
                approval_store=approval_store,
                approval_token_id=approval_token_id,
                approval_callback=approval_callback,
                audit_log=audit_log,
                model_policy=model_policy,
                control=control,
                on_event=on_event,
            )
        except Exception as exc:
            return ProjectBuildResult(
                accepted=False,
                project_id=self.project_id,
                root=self.root,
                requirement=text,
                error=str(exc)[:2000],
                result={"preflight": readiness},
            )

        report = report if isinstance(report, dict) else {}
        files = tuple(
            str(path) for path in (report.get("files") or [])
            if isinstance(path, str)
        )[:500]
        report.setdefault("preflight", readiness)
        return ProjectBuildResult(
            accepted=bool(report.get("accepted")),
            project_id=self.project_id,
            root=self.root,
            requirement=text,
            files=files,
            model=str(report.get("model") or report.get("selected_model") or ""),
            provider=str(report.get("provider") or report.get("selected_provider") or ""),
            error=str(report.get("error") or "")[:2000],
            result=report,
        )


def build_project(requirement: str, project_id: str,
                  root: str | Path = ".", *, fabric: Any = None,
                  mode: OperationMode = OperationMode.AUTONOMOUS,
                  max_debug_retries: int = 3,
                  approved: bool = False, **kwargs: Any) -> ProjectBuildResult:
    """Convenience function for applications embedding Forge."""
    builder = ProjectBuilder(
        project_id,
        root=root,
        fabric=fabric,
        mode=mode,
        max_debug_retries=max_debug_retries,
    )
    return builder.build(requirement, approved=approved, **kwargs)


def main(argv=None) -> int:
    """Small executable entry point: ``forge-build <requirement>``."""
    parser = argparse.ArgumentParser(
        prog="forge-build",
        description="Build a real software project through Forge's guarded loop.",
    )
    parser.add_argument("requirement", help="Natural-language project requirement")
    parser.add_argument("--project", default="project", help="Stable project id")
    parser.add_argument("--root", default=".", help="Project root/worktree")
    parser.add_argument(
        "--mode", choices=[item.value for item in OperationMode],
        default=OperationMode.AUTONOMOUS.value,
    )
    parser.add_argument("--max-debug-retries", type=int, default=3)
    parser.add_argument("--approve", action="store_true",
                        help="Pre-approve writes subject to the policy gate")
    parser.add_argument("--preflight", action="store_true",
                        help="Only report whether a real coding model is ready")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    builder = ProjectBuilder(
        args.project,
        root=args.root,
        mode=OperationMode(args.mode),
        max_debug_retries=args.max_debug_retries,
    )
    if args.preflight:
        payload = builder.preflight()
        if args.json:
            print(json.dumps(payload, indent=2, default=str))
        else:
            print("READY" if payload["ready"] else "NOT READY")
            print(payload["reason"])
        return 0 if payload["ready"] else 1

    outcome = builder.build(args.requirement, approved=args.approve)
    if args.json:
        print(json.dumps(outcome.to_dict(), indent=2, default=str))
    else:
        print("Project build: %s" % ("ACCEPTED" if outcome.accepted else "FAILED"))
        print("  project=%s" % outcome.project_id)
        if outcome.model:
            print("  model=%s provider=%s" % (outcome.model, outcome.provider or "-"))
        if outcome.files:
            print("  files=%s" % ", ".join(outcome.files))
        if outcome.error:
            print("  error=%s" % outcome.error)
    return 0 if outcome.accepted else 1


__all__ = ["ProjectBuildResult", "ProjectBuilder", "build_project", "main"]


if __name__ == "__main__":
    raise SystemExit(main())