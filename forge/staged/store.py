"""SQLite persistence for staged builds (A82).

Tables live in the same control-plane database so build projects survive
restarts alongside runs, events, and approvals. The store is deliberately
dumb: all gating and verification lives in :mod:`forge.staged.service`.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional
from uuid import uuid4

from forge.control.db import Database
from forge.staged.models import BuildProject, BuildStage, StageStatus


def _parse_json(text: Any, default: Any) -> Any:
    try:
        value = json.loads(text or "")
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


class StagedStore:
    """CRUD over ``staged_projects`` / ``build_stages``."""

    def __init__(self, db: Database) -> None:
        self._db = db
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS staged_projects (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                roadmap TEXT NOT NULL DEFAULT '',
                blueprint TEXT NOT NULL DEFAULT '',
                created_by TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS build_stages (
                id TEXT PRIMARY KEY,
                build_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                position INTEGER NOT NULL,
                title TEXT NOT NULL,
                prompt TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                run_id TEXT NOT NULL DEFAULT '',
                attempts INTEGER NOT NULL DEFAULT 0,
                runs_json TEXT NOT NULL DEFAULT '[]',
                evidence_json TEXT NOT NULL DEFAULT '{}',
                docs_snapshot_json TEXT NOT NULL DEFAULT '{}',
                error TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(build_id, position)
            )
            """
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_build_stages_build "
            "ON build_stages(build_id)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_staged_projects_project "
            "ON staged_projects(project_id)"
        )

    # -- builds -----------------------------------------------------------

    def create_build(self, project_id: str, name: str, *,
                     description: str = "", roadmap: str = "",
                     blueprint: str = "",
                     created_by: str = "") -> BuildProject:
        now = time.time()
        build = BuildProject(
            id="b-" + uuid4().hex[:16], project_id=project_id,
            name=name, description=description, roadmap=roadmap,
            blueprint=blueprint, created_by=created_by,
            created_at=now, updated_at=now)
        self._db.execute(
            "INSERT INTO staged_projects (id, project_id, name, description, "
            "roadmap, blueprint, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (build.id, build.project_id, build.name, build.description,
             build.roadmap, build.blueprint, build.created_by,
             build.created_at, build.updated_at))
        return build

    def get_build(self, build_id: str) -> Optional[BuildProject]:
        row = self._db.query_one(
            "SELECT * FROM staged_projects WHERE id = ?", (build_id,))
        return self._row_to_build(row) if row is not None else None

    def list_builds(self, project_id: str) -> List[BuildProject]:
        rows = self._db.query(
            "SELECT * FROM staged_projects WHERE project_id = ? "
            "ORDER BY created_at ASC", (project_id,))
        return [self._row_to_build(row) for row in rows]

    def count_builds(self, project_id: str) -> int:
        row = self._db.query_one(
            "SELECT COUNT(*) AS n FROM staged_projects WHERE project_id = ?",
            (project_id,))
        return int(row["n"]) if row is not None else 0

    def update_build(self, build_id: str, **fields: Any) -> Optional[BuildProject]:
        allowed = {"name", "description", "roadmap", "blueprint"}
        updates = {key: value for key, value in fields.items()
                   if key in allowed}
        if not updates:
            return self.get_build(build_id)
        updates["updated_at"] = time.time()
        assignments = ", ".join("%s = ?" % key for key in sorted(updates))
        params = [updates[key] for key in sorted(updates)] + [build_id]
        cursor = self._db.execute(
            "UPDATE staged_projects SET %s WHERE id = ?" % assignments,
            tuple(params))
        if cursor.rowcount == 0:
            return None
        return self.get_build(build_id)

    def delete_build(self, build_id: str) -> bool:
        self._db.execute(
            "DELETE FROM build_stages WHERE build_id = ?", (build_id,))
        cursor = self._db.execute(
            "DELETE FROM staged_projects WHERE id = ?", (build_id,))
        return cursor.rowcount > 0

    # -- stages -----------------------------------------------------------

    def add_stage(self, build: BuildProject, title: str,
                  prompt: str) -> BuildStage:
        row = self._db.query_one(
            "SELECT COALESCE(MAX(position), 0) AS m FROM build_stages "
            "WHERE build_id = ?", (build.id,))
        position = int(row["m"] or 0) + 1 if row is not None else 1
        now = time.time()
        stage = BuildStage(
            id="s-" + uuid4().hex[:16], build_id=build.id,
            project_id=build.project_id, position=position,
            title=title, prompt=prompt, created_at=now, updated_at=now)
        self._db.execute(
            "INSERT INTO build_stages (id, build_id, project_id, position, "
            "title, prompt, status, run_id, attempts, runs_json, "
            "evidence_json, docs_snapshot_json, error, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (stage.id, stage.build_id, stage.project_id, stage.position,
             stage.title, stage.prompt, stage.status.value, stage.run_id,
             stage.attempts, "[]", "{}", "{}", stage.error,
             stage.created_at, stage.updated_at))
        return stage

    def list_stages(self, build_id: str) -> List[BuildStage]:
        rows = self._db.query(
            "SELECT * FROM build_stages WHERE build_id = ? "
            "ORDER BY position ASC", (build_id,))
        return [self._row_to_stage(row) for row in rows]

    def get_stage(self, build_id: str, position: int) -> Optional[BuildStage]:
        row = self._db.query_one(
            "SELECT * FROM build_stages WHERE build_id = ? AND position = ?",
            (build_id, int(position)))
        return self._row_to_stage(row) if row is not None else None

    def save_stage(self, stage: BuildStage) -> BuildStage:
        stage.updated_at = time.time()
        self._db.execute(
            "UPDATE build_stages SET title = ?, prompt = ?, status = ?, "
            "run_id = ?, attempts = ?, runs_json = ?, evidence_json = ?, "
            "docs_snapshot_json = ?, error = ?, updated_at = ? "
            "WHERE id = ?",
            (stage.title, stage.prompt, stage.status.value, stage.run_id,
             stage.attempts, json.dumps(stage.runs, default=str),
             json.dumps(stage.evidence, default=str),
             json.dumps(stage.docs_snapshot, default=str), stage.error,
             stage.updated_at, stage.id))
        return stage

    def delete_stage(self, stage: BuildStage) -> None:
        """Delete one stage and close the position gap (renumber)."""
        self._db.execute(
            "DELETE FROM build_stages WHERE id = ?", (stage.id,))
        self._db.execute(
            "UPDATE build_stages SET position = position - 1 "
            "WHERE build_id = ? AND position > ?",
            (stage.build_id, stage.position))

    def touch_build(self, build_id: str) -> None:
        self._db.execute(
            "UPDATE staged_projects SET updated_at = ? WHERE id = ?",
            (time.time(), build_id))

    # -- mapping ----------------------------------------------------------

    @staticmethod
    def _row_to_build(row: Any) -> BuildProject:
        return BuildProject(
            id=row["id"], project_id=row["project_id"], name=row["name"],
            description=row["description"] or "",
            roadmap=row["roadmap"] or "",
            blueprint=row["blueprint"] or "",
            created_by=row["created_by"] or "",
            created_at=float(row["created_at"] or 0.0),
            updated_at=float(row["updated_at"] or 0.0))

    @staticmethod
    def _row_to_stage(row: Any) -> BuildStage:
        try:
            status = StageStatus(str(row["status"] or "pending"))
        except ValueError:
            status = StageStatus.PENDING
        return BuildStage(
            id=row["id"], build_id=row["build_id"],
            project_id=row["project_id"], position=int(row["position"]),
            title=row["title"] or "", prompt=row["prompt"] or "",
            status=status, run_id=row["run_id"] or "",
            attempts=int(row["attempts"] or 0),
            runs=_parse_json(row["runs_json"], []),
            evidence=_parse_json(row["evidence_json"], {}),
            docs_snapshot=_parse_json(row["docs_snapshot_json"], {}),
            error=row["error"] or "",
            created_at=float(row["created_at"] or 0.0),
            updated_at=float(row["updated_at"] or 0.0))
