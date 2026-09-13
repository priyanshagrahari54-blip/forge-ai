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
    """In-process ControlPlane host for the desktop GUI.

    When a link is configured (:meth:`link_configure`), task submission
    and the *Server* tab route through :class:`forge.client.ForgeClient`
    — LOCAL runs the bounded light operations on this machine, SERVER
    and HYBRID delegate the heavy engineering to the Forge Server.
    """

    actor: str = "desktop"
    db_path: str = ""
    fabric: Any = None
    policy: Any = None
    #: Seconds one approval may wait before it fails closed. Interactive
    #: users get a generous window; tests pass a small value.
    approval_timeout: float = 600.0
    #: A81 Forge link client (``forge.client.ForgeClient``) or ``None``.
    link: Any = None
    #: Directory for the client settings + secret (injectable in tests).
    link_config_dir: str = ""
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
        # Close the Forge link: server tasks are unaffected (requirement
        # 8) — they continue server-side and are re-restored on reopen.
        if self.link is not None:
            try:
                self.link.disconnect()
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
        """Submit a task. Routes through the Forge link when configured
        (LOCAL = bounded light operations here, SERVER/HYBRID = decided
        by :mod:`forge.client.router`); otherwise runs the embedded
        local plane as before."""
        if requirement.strip() and self.link is not None:
            return self._submit_via_link(project_id, requirement)
        plane = self._require_plane()
        session = self._session_for(project_id)
        if not requirement.strip():
            raise BackendError("Describe the task first.")
        try:
            run = plane.submit_task(session, requirement, mode=mode or "")
        except Exception as exc:
            raise BackendError(str(exc)) from exc
        result = self._run_to_dict(run)
        result["execution"] = "LOCAL-PLANE"
        return result

    # -- Forge Server link (A81) -------------------------------------------

    def _submit_via_link(self, project_id: str, requirement: str) \
            -> dict[str, Any]:
        if self.link is None:
            raise BackendError("Forge link is not configured.")
        root = ""
        try:
            plane = self._require_plane()
            root = plane.get_project(project_id).root
        except Exception:
            root = ""
        try:
            return dict(self.link.submit(requirement, root=root))
        except Exception as exc:
            raise BackendError(f"Forge link: {exc}") from exc

    def link_configure(self, settings: dict[str, Any]) -> dict[str, Any]:
        """Create/replace the link client from a settings dict.

        Accepted keys: server_url, client_id, project_id, mode,
        request_timeout, heartbeat_seconds, reconnect{initial_delay,
        max_delay, multiplier, max_attempts}, local{allow_local,
        allow_server, max_task_chars, min_free_ram_mb,
        max_files_walked}, secret (stored 0600, never kept in memory
        beyond this call), config_dir (override, mainly tests), and
        ``_sender`` (test hook: replaces the HTTP transport callable).
        """
        from forge.client.config import ClientConfig, ConfigError

        if not isinstance(settings, dict):
            raise BackendError("Link settings must be a mapping.")
        config_dir = str(settings.get("config_dir")
                         or self.link_config_dir or "")
        try:
            config = ClientConfig.from_dict(settings, config_dir=config_dir)
            secret = str(settings.get("secret", "") or "")
            if secret:
                config.store_secret(secret)
            saved = config.save()
        except ConfigError as exc:
            raise BackendError(f"Invalid link settings: {exc}") from exc
        self._link_sender = settings.get("_sender")
        client = self._build_link_client(config)
        self.link = client
        status = client.status()
        status["configured"] = True
        status["settings_path"] = str(saved)
        status["secret_path"] = str(config.secret_path())
        return status

    def _build_link_client(self, config) -> Any:
        from forge.client.client import ForgeClient
        from forge.client.transport import LinkTransport

        sender = getattr(self, "_link_sender", None)
        transport = (LinkTransport(config, sender=sender)
                     if sender is not None else LinkTransport(config))
        return ForgeClient(config, transport=transport)


    def link_load(self) -> dict[str, Any]:
        """Load a previously saved link configuration (if any)."""
        from forge.client.config import ClientConfig, ConfigError
        try:
            config = ClientConfig.load(self.link_config_dir)
        except ConfigError:
            return {"configured": False}
        self.link = self._build_link_client(config)
        status = self.link.status()
        status["configured"] = True
        return status

    def _require_link(self):
        if self.link is None:
            raise BackendError(
                "Forge link is not configured. Open the Server tab and "
                "enter the server URL, client id, and secret.")
        return self.link

    def link_status(self) -> dict[str, Any]:
        if self.link is None:
            return {"configured": False,
                    "state": "DISCONNECTED", "mode": "hybrid"}
        status = dict(self.link.status())
        status["configured"] = True
        return status

    def link_connect(self, *, restore: bool = True) -> dict[str, Any]:
        """Connect (and by default restore server task state)."""
        link = self._require_link()
        snapshot = link.restore() if restore else {}
        status = dict(link.status())
        status["configured"] = True
        status["connected"] = link.connection.state_name == "CONNECTED"
        snapshot["connection"] = status
        return snapshot

    def link_disconnect(self) -> dict[str, Any]:
        link = self._require_link()
        link.disconnect()
        return self.link_status()

    def link_set_mode(self, mode: str) -> dict[str, Any]:
        """Switch execution mode (LOCAL/SERVER/HYBRID) and persist it."""
        from forge.client.config import EXECUTION_MODES
        link = self._require_link()
        normalized = (mode or "").strip().lower()
        if normalized not in EXECUTION_MODES:
            raise BackendError(
                f"mode must be one of {list(EXECUTION_MODES)}")
        link.config.mode = normalized
        try:
            link.config.save()
        except Exception as exc:  # settings must persist, but stay usable
            raise BackendError(f"could not save settings: {exc}") from exc
        return self.link_status()

    def link_snapshot(self, selected_task: str = "", *,
                      full: bool = False) -> dict[str, Any]:
        """Everything the Server tab renders; never raises."""
        if self.link is None:
            return {"configured": False}
        try:
            snapshot = dict(self.link.snapshot(selected_task=selected_task,
                                               full=full))
        except Exception as exc:
            snapshot = {"configured": True,
                        "connection": self.link_status(),
                        "error": str(exc)}
        snapshot["configured"] = True
        snapshot["decision_preview"] = self.link_decision_preview(
            fallback=True) or {}
        return snapshot

    def link_decision_preview(self, requirement: str = "",
                              *, fallback: bool = False) \
            -> dict[str, Any] | None:
        """What the router would do for ``requirement`` (or a neutral
        placeholder when ``fallback``). For the Server tab hint line."""
        if self.link is None:
            return None
        if not requirement:
            if fallback:
                return {"execution": "—",
                        "reason": "type a task to see the routing decision"}
            return None
        try:
            decision = self.link.decide(requirement)
        except Exception:
            return None
        return {"execution": decision.execution, "reason": decision.reason}

    def link_decide_approval(self, approval_id: str, approved: bool) \
            -> dict[str, Any]:
        link = self._require_link()
        try:
            return dict(link.decide_approval(approval_id, approved))
        except Exception as exc:
            raise BackendError(f"approval failed: {exc}") from exc

    def link_mutate_task(self, task_id: str, operation: str) \
            -> dict[str, Any]:
        link = self._require_link()
        try:
            return dict(link.mutate_task(task_id, operation))
        except Exception as exc:
            raise BackendError(f"{operation} failed: {exc}") from exc

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
