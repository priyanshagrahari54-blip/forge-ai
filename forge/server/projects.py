"""Persistent project registry (A81).

A task may only ever touch disk inside a *registered* project root.
Registration validates the id, resolves the root to an absolute
directory, and refuses anything that is not an existing directory —
so the API can never be pointed at arbitrary paths at task time. The
registry itself is durable (SQLite): projects survive restarts.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from forge.server.errors import Conflict, InvalidRequest, ProjectNotFound
from forge.server.models import ProjectInfo, validate_id
from forge.server.storage import Database

#: Bound the registry so one server instance stays reviewable.
MAX_PROJECTS = 64


class ProjectRegistry:
    """SQLite-backed registry of projects the server may work in."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def register(self, project_id: str, root: "str | Path", *,
                 name: str = "") -> ProjectInfo:
        try:
            project_id = validate_id(project_id, kind="project_id")
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from exc
        if not isinstance(root, str) or not root.strip():
            raise InvalidRequest("root must be a non-empty path string.")
        if "\x00" in root:
            raise InvalidRequest("root contains a null byte.")
        resolved = Path(root).expanduser()
        try:
            resolved = resolved.resolve()
        except (OSError, RuntimeError) as exc:
            raise InvalidRequest(
                "Cannot resolve project root: %s" % exc) from exc
        if not resolved.is_dir():
            raise InvalidRequest(
                "Project root is not an existing directory: %s"
                % resolved.name)
        existing = self.get(project_id)
        if existing is not None:
            if Path(existing.root) == resolved:
                return existing  # idempotent re-registration
            raise Conflict(
                "Project %r is already registered with a different root."
                % project_id, project_id=project_id)
        count_row = self._db.query_one(
            "SELECT COUNT(*) AS n FROM projects WHERE status = 'active'")
        if count_row is not None and int(count_row["n"]) >= MAX_PROJECTS:
            raise Conflict("Project registry is full (%d)." % MAX_PROJECTS)
        project = ProjectInfo(
            project_id=project_id,
            name=(name or project_id)[:128],
            root=str(resolved), created_at=time.time())
        self._db.execute(
            "INSERT INTO projects (project_id, name, root, status, "
            "created_at) VALUES (?, ?, ?, 'active', ?)",
            (project.project_id, project.name, project.root,
             project.created_at))
        return project

    def get(self, project_id: str) -> Optional[ProjectInfo]:
        row = self._db.query_one(
            "SELECT * FROM projects WHERE project_id = ? "
            "AND status = 'active'", (project_id,))
        return self._row_to_project(row) if row is not None else None

    def get_or_raise(self, project_id: str) -> ProjectInfo:
        project = self.get(project_id)
        if project is None:
            raise ProjectNotFound(
                "Unknown project: %r" % project_id, project_id=project_id)
        return project

    def list(self) -> List[ProjectInfo]:
        rows = self._db.query(
            "SELECT * FROM projects WHERE status = 'active' "
            "ORDER BY created_at ASC")
        return [self._row_to_project(row) for row in rows]

    def project_ids(self) -> List[str]:
        return [project.project_id for project in self.list()]

    def unregister(self, project_id: str) -> bool:
        """Deactivate a project (tasks keep their history)."""
        self.get_or_raise(project_id)
        cursor = self._db.execute(
            "UPDATE projects SET status = 'inactive' WHERE project_id = ?",
            (project_id,))
        return cursor.rowcount > 0

    @staticmethod
    def _row_to_project(row: Any) -> ProjectInfo:
        return ProjectInfo(
            project_id=row["project_id"], name=row["name"],
            root=row["root"], status=row["status"] or "active",
            created_at=float(row["created_at"]))
