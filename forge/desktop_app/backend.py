"""Headless backend for the Forge desktop app.

:class:`DesktopBackend` embeds a :class:`ControlPlane` in-process and exposes
a small, GUI-friendly, exception-normalized API: every method returns plain
``dict``/``list`` data (safe to pass across threads) and raises
:class:`BackendError` with a human-readable message on failure. It imports
no GUI toolkit, so it runs headless and is fully unit-tested.

Threading contract: methods are safe to call from a background polling
thread; the Tk UI must only touch widgets from the main thread.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class BackendError(RuntimeError):
    """User-facing backend failure with an actionable message."""


@dataclass
class ProjectSession:
    project_id: str
    session_id: str = ""
    profile: str = "assisted"
    actor: str = "desktop"


@dataclass
class DesktopBackend:
    """In-process ControlPlane host for the desktop GUI."""

    actor: str = "desktop"
    db_path: str = ""
    fabric: Any = None
    policy: Any = None
    #: Seconds one approval may wait before it fails closed. Interactive
    #: users get a generous window; tests pass a small value.
    approval_timeout: float = 600.0
    _plane: Any = field(default=None, init=False, repr=False)
    _sessions: dict[str, ProjectSession] = field(default_factory=dict,
                                                 init=False, repr=False)
    _started: bool = field(default=False, init=False, repr=False)

    # -- lifecycle ------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._started and self._plane is not None

    def _require_plane(self):
        if not self.running:
            raise BackendError("Backend is not started.")
        assert self._plane is not None
        return self._plane

    def start(self, projects: dict[str, str]) -> list[dict[str, str]]:
        """Start the embedded control plane over ``{id: root}`` projects."""
        from forge.control import ControlConfig, ControlPlane

        if self.running:
            raise BackendError("Backend is already started.")
        if not projects:
            raise BackendError("Add at least one project folder first.")
        resolved: dict[str, str] = {}
        for project_id, root in projects.items():
            path = Path(root).expanduser()
            if not path.is_dir():
                raise BackendError(
                    f"Project folder is not a directory: {root}")
            resolved[project_id] = str(path.resolve())
        db = self.db_path or str(Path(resolved[next(iter(resolved))])
                                   / ".forge" / "desktop.db")
        try:
            plane = ControlPlane(ControlConfig(
                db_path=db, projects=resolved, fabric=self.fabric,
                policy=self.policy,
                approval_timeout=self.approval_timeout))
            plane.start()
        except Exception as exc:
            raise BackendError(f"Could not start Forge: {exc}") from exc
        self._plane = plane
        self._started = True
        self._sessions = {}
        return self.projects()

    def stop(self, *, shutdown_timeout: float = 20.0) -> None:
        """Cancel in-flight tasks, then stop the plane (never hangs long).

        Workers blocked on approval waits or stage boundaries wake on
        cancel; this waits (bounded) for them to land before joining the
        pool, so closing the window cannot stall for a full approval
        timeout.
        """
        plane = self._plane
        if plane is not None:
            self._cancel_all_active()
            deadline = time.time() + max(1.0, shutdown_timeout)
            while time.time() < deadline:
                if not self._active_tasks():
                    break
                time.sleep(0.1)
        self._plane = None
        self._started = False
        self._sessions = {}
        if plane is not None:
            try:
                plane.stop()
            except Exception:
                pass

    def _active_tasks(self) -> list[tuple[str, str]]:
        """``(project_id, task_id)`` pairs not yet in a terminal state."""
        terminal = {"SUCCEEDED", "FAILED", "CANCELLED", "ROLLED_BACK"}
        active: list[tuple[str, str]] = []
        if not self.running:
            return active
        for project in self.projects():
            try:
                tasks = self.list_tasks(project["id"], limit=100)
            except BackendError:
                continue
            for task in tasks:
                if task.get("status") not in terminal:
                    active.append((project["id"], task["id"]))
        return active

    def _cancel_all_active(self) -> None:
        for project_id, task_id in self._active_tasks():
            try:
                self.cancel_task(project_id, task_id)
            except BackendError:
                pass

    # -- projects & sessions --------------------------------------------

    def projects(self) -> list[dict[str, str]]:
        plane = self._require_plane()
        return [{"id": project.id, "name": project.name, "root": project.root}
                for project in plane.projects.values()]

    def add_project(self, project_id: str, root: str) -> dict[str, str]:
        plane = self._require_plane()
        try:
            project = plane.register_project(project_id, root)
        except Exception as exc:
            raise BackendError(str(exc)) from exc
        self._sessions.pop(project_id, None)
        return {"id": project.id, "name": project.name, "root": project.root}

    def _session_for(self, project_id: str, profile: str = ""):
        plane = self._require_plane()
        if project_id not in plane.projects:
            raise BackendError(f"Unknown project: {project_id!r}")
        cached = self._sessions.get(project_id)
        if cached is not None and (not profile or profile == cached.profile):
            try:
                session = plane.sessions.get(cached.session_id)
            except Exception:
                session = None
            if session is not None and session.active:
                return session
        try:
            session, _token = plane.create_session(
                self.actor, project_id, profile=profile or "assisted")
        except Exception as exc:
            raise BackendError(f"Could not open session: {exc}") from exc
        self._sessions[project_id] = ProjectSession(
            project_id=project_id, session_id=session.id,
            profile=session.profile, actor=self.actor)
        return session

    # -- tasks -----------------------------------------------------------

    @staticmethod
    def _run_to_dict(run) -> dict[str, Any]:
        try:
            files = run.files()
        except Exception:
            files = []
        return {
            "id": run.id,
            "project_id": run.project_id,
            "requirement": run.requirement,
            "status": run.status.value,
            "stage": run.stage,
            "mode": run.mode,
            "actor": run.actor,
            "model": run.model,
            "provider": run.provider,
            "error": run.error,
            "rollback": run.rollback,
            "files": list(files),
            "version": run.version,
            "created_at": run.created_at,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
        }

    def submit_task(self, project_id: str, requirement: str,
                    mode: str = "") -> dict[str, Any]:
        plane = self._require_plane()
        session = self._session_for(project_id)
        if not requirement.strip():
            raise BackendError("Describe the task first.")
        try:
            run = plane.submit_task(session, requirement, mode=mode or "")
        except Exception as exc:
            raise BackendError(str(exc)) from exc
        return self._run_to_dict(run)

    def list_tasks(self, project_id: str, status: str = "",
                   limit: int = 50) -> list[dict[str, Any]]:
        plane = self._require_plane()
        session = self._session_for(project_id)
        try:
            runs, _total = plane.list_tasks(session, status=status,
                                            limit=limit)
        except Exception as exc:
            raise BackendError(str(exc)) from exc
        return [self._run_to_dict(run) for run in runs]

    def get_task(self, project_id: str, task_id: str) -> dict[str, Any]:
        plane = self._require_plane()
        session = self._session_for(project_id)
        try:
            run = plane.get_task(session, task_id)
        except Exception as exc:
            raise BackendError(str(exc)) from exc
        return self._run_to_dict(run)

    def get_task_report(self, project_id: str,
                        task_id: str) -> dict[str, Any]:
        plane = self._require_plane()
        session = self._session_for(project_id)
        try:
            return dict(plane.get_task_report(session, task_id))
        except Exception as exc:
            raise BackendError(str(exc)) from exc

    def get_task_events(self, project_id: str, task_id: str, *,
                        after: int = 0, limit: int = 200) -> dict[str, Any]:
        plane = self._require_plane()
        session = self._session_for(project_id)
        try:
            events, latest = plane.get_task_events(session, task_id,
                                                   after=after, limit=limit)
        except Exception as exc:
            raise BackendError(str(exc)) from exc
        return {"events": [dict(event) for event in events], "latest": latest}

    def pause_task(self, project_id: str, task_id: str) -> dict[str, Any]:
        return self._mutate_task("pause_task", project_id, task_id)

    def resume_task(self, project_id: str, task_id: str) -> dict[str, Any]:
        return self._mutate_task("resume_task", project_id, task_id)

    def cancel_task(self, project_id: str, task_id: str) -> dict[str, Any]:
        return self._mutate_task("cancel_task", project_id, task_id)

    def retry_task(self, project_id: str, task_id: str) -> dict[str, Any]:
        return self._mutate_task("retry_task", project_id, task_id)

    def _mutate_task(self, operation: str, project_id: str,
                     task_id: str) -> dict[str, Any]:
        plane = self._require_plane()
        session = self._session_for(project_id)
        try:
            run = getattr(plane, operation)(session, task_id)
        except Exception as exc:
            raise BackendError(str(exc)) from exc
        return self._run_to_dict(run)

    # -- approvals --------------------------------------------------------

    def list_approvals(self, project_id: str) -> list[dict[str, Any]]:
        plane = self._require_plane()
        session = self._session_for(project_id)
        try:
            return [dict(item) for item in plane.list_approvals(session)]
        except Exception as exc:
            raise BackendError(str(exc)) from exc

    def decide_approval(self, project_id: str, approval_id: str,
                        approved: bool) -> dict[str, Any]:
        plane = self._require_plane()
        session = self._session_for(project_id)
        try:
            if approved:
                return dict(plane.approve_request(session, approval_id))
            return dict(plane.deny_request(session, approval_id))
        except Exception as exc:
            raise BackendError(str(exc)) from exc

    # -- models ------------------------------------------------------------

    def model_readiness(self) -> dict[str, Any]:
        plane = self._require_plane()
        try:
            return dict(plane.model_readiness(probe_network=True, timeout=3.0))
        except Exception as exc:
            raise BackendError(str(exc)) from exc

    def model_state(self) -> dict[str, Any]:
        plane = self._require_plane()
        try:
            state = plane.get_model_state()
        except Exception as exc:
            raise BackendError(str(exc)) from exc
        return {
            "models": [dict(m) if isinstance(m, dict) else m
                       for m in state.get("models", [])],
            "providers": [dict(p) if isinstance(p, dict) else p
                          for p in state.get("providers", [])],
            "health": dict(state.get("health", {})),
            "provider_health": dict(state.get("provider_health", {})),
            "routing_policy": state.get("routing_policy", {}),
        }

    # -- files --------------------------------------------------------------

    def read_project_file(self, project_id: str, rel_path: str,
                          max_chars: int = 200_000) -> dict[str, Any]:
        """Read a repo-relative file for the viewer (bounded, no secrets)."""
        from forge.core.report import redact_text

        plane = self._require_plane()
        try:
            project = plane.get_project(project_id)
        except Exception as exc:
            raise BackendError(str(exc)) from exc
        candidate = (Path(project.root) / rel_path).resolve()
        root = Path(project.root).resolve()
        if candidate != root and root not in candidate.parents:
            raise BackendError("Refusing to read outside the project.")
        if not candidate.is_file():
            raise BackendError(f"Not a file: {rel_path}")
        try:
            text = candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise BackendError(f"Cannot read {rel_path}: {exc}") from exc
        truncated = len(text) > max_chars
        try:
            text = redact_text(text[:max_chars])
        except Exception:
            text = text[:max_chars]
        return {"path": rel_path, "content": text, "truncated": truncated,
                "size": candidate.stat().st_size}

    # -- agent manager (creation engine) ----------------------------------------

    def _engine_for(self, project_id: str):
        from forge.agents.creation import AgentCreationEngine

        plane = self._require_plane()
        try:
            project = plane.get_project(project_id)
        except Exception as exc:
            raise BackendError(str(exc)) from exc
        if not hasattr(self, "_agent_engines"):
            self._agent_engines: dict[str, Any] = {}
        engine = self._agent_engines.get(project_id)
        if engine is None:
            store = str(Path(project.root) / ".forge" / "agent-engine.json")
            try:
                engine = AgentCreationEngine(store_path=store)
            except ValueError as exc:
                raise BackendError(f"Agent store error: {exc}") from exc
            self._agent_engines[project_id] = engine
        return engine

    def agents_templates(self) -> list[dict[str, Any]]:
        from forge.agents.creation import AgentCreationEngine

        return AgentCreationEngine().templates()

    def agents_list(self, project_id: str) -> list[dict[str, Any]]:
        return [package.manifest()
                for package in self._engine_for(project_id).list()]

    def agents_create(self, project_id: str, template: str,
                      name: str) -> dict[str, Any]:
        engine = self._engine_for(project_id)
        try:
            package = engine.create_from_template(
                template, name, created_by=self.actor)
        except ValueError as exc:
            raise BackendError(str(exc)) from exc
        return package.to_dict()

    def agents_show(self, project_id: str, name: str) -> dict[str, Any]:
        try:
            return self._engine_for(project_id).get(name).to_dict()
        except ValueError as exc:
            raise BackendError(str(exc)) from exc

    def _agents_lifecycle(self, operation: str, project_id: str,
                          name: str) -> dict[str, Any]:
        engine = self._engine_for(project_id)
        try:
            if operation == "validate":
                return engine.validate(name, actor=self.actor)
            if operation == "test":
                return engine.benchmark(name, actor=self.actor)
            return getattr(engine, operation)(
                name, actor=self.actor).to_dict()
        except ValueError as exc:
            raise BackendError(str(exc)) from exc

    def agents_validate(self, project_id: str,
                        name: str) -> dict[str, Any]:
        return self._agents_lifecycle("validate", project_id, name)

    def agents_test(self, project_id: str, name: str) -> dict[str, Any]:
        return self._agents_lifecycle("test", project_id, name)

    def agents_enable(self, project_id: str, name: str) -> dict[str, Any]:
        return self._agents_lifecycle("enable", project_id, name)

    def agents_pause(self, project_id: str, name: str) -> dict[str, Any]:
        return self._agents_lifecycle("pause", project_id, name)

    def agents_resume(self, project_id: str, name: str) -> dict[str, Any]:
        return self._agents_lifecycle("resume", project_id, name)

    def agents_disable(self, project_id: str,
                       name: str) -> dict[str, Any]:
        return self._agents_lifecycle("disable", project_id, name)

    def agents_retire(self, project_id: str, name: str) -> dict[str, Any]:
        return self._agents_lifecycle("retire", project_id, name)

    def agents_grant(self, project_id: str, name: str,
                     index: int) -> dict[str, Any]:
        try:
            grant = self._engine_for(project_id).grant_permission(
                name, index, approver=self.actor)
        except ValueError as exc:
            raise BackendError(str(exc)) from exc
        return {"agent": name, "grant": grant}

    # -- polling snapshot ----------------------------------------------------

    def poll_snapshot(self, project_id: str, selected_task_id: str = "",
                      event_cursor: int = 0) -> dict[str, Any]:
        """One cheap refresh of everything the UI shows, in a single call.

        Never raises: per-section failures are captured under ``errors`` so
        one broken panel cannot take down the whole window.
        """
        snapshot: dict[str, Any] = {
            "at": time.time(), "project_id": project_id,
            "tasks": [], "approvals": [],
            "selected": None, "events": [], "event_cursor": event_cursor,
            "errors": {},
        }
        try:
            snapshot["tasks"] = self.list_tasks(project_id, limit=50)
        except BackendError as exc:
            snapshot["errors"]["tasks"] = str(exc)
        try:
            snapshot["approvals"] = self.list_approvals(project_id)
        except BackendError as exc:
            snapshot["errors"]["approvals"] = str(exc)
        if selected_task_id:
            try:
                snapshot["selected"] = self.get_task(project_id,
                                                     selected_task_id)
            except BackendError as exc:
                snapshot["errors"]["selected"] = str(exc)
            try:
                fetched = self.get_task_events(
                    project_id, selected_task_id, after=event_cursor)
                snapshot["events"] = fetched["events"]
                snapshot["event_cursor"] = fetched["latest"]
            except BackendError as exc:
                snapshot["errors"]["events"] = str(exc)
        return snapshot
