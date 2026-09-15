"""First-class project-building API for Forge.

The project builder is intentionally thin: the existing Supervisor remains the
single guarded engineering transaction, while this module gives callers one
stable API for the product promise:

    requirement -> real-model preflight -> plan -> code -> test/debug ->
    review -> security -> acceptance -> git commit

No model is invented here. A caller must provide a real-capable ``ModelFabric``
(or the builder will construct the configured default fabric) and the Supervisor
will fail fast when the fabric is fallback-only. This makes ``build_project``
a product API rather than another inference implementation.

Python floor: 3.8.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

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
        payload: Dict[str, Any] = {
            "accepted": bool(self.accepted),
            "project_id": self.project_id,
            "root": self.root,
            "requirement": self.requirement,
            "files": list(self.files),
            "model": self.model,
            "provider": self.provider,
            "error": self.error,
        }
        # Keep the detailed supervisor report available to programmatic callers
        # while avoiding accidental duplication of large text fields.
        payload["result"] = self.result
        return payload


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
        """Check whether a configured fabric can actually produce project code.

        The check is intentionally conservative. Forge's deterministic fallback
        is useful for infrastructure tests, but it is not a coding model and is
        therefore never reported as project-builder readiness.
        """
        fabric = self._fabric_or_default()
        try:
            from forge.models.readiness import (
                describe_no_model_error,
                fabric_has_real_model,
            )

            ready = bool(fabric_has_real_model(fabric))
            return {
                "ready": ready,
                "reason": "real coding-capable model is available"
                if ready else describe_no_model_error(fabric=fabric),
                "fallback_only": not ready,
            }
        except Exception as exc:
            return {
                "ready": False,
                "fallback_only": True,
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
            )

        report = report if isinstance(report, dict) else {}
        files = tuple(
            str(path) for path in (report.get("files") or [])
            if isinstance(path, str)
        )[:500]
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


__all__ = ["ProjectBuildResult", "ProjectBuilder", "build_project"]
