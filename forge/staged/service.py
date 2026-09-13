"""Staged-build orchestration: one stage at a time, verified for real (A82).

The service consumes stage prompts strictly in order. A stage is submitted
to the *existing* control-plane pipeline (the real Supervisor transaction),
and it becomes ``completed`` only when the linked run verifies:

- run status is ``SUCCEEDED`` (the worker sets this solely from the
  Supervisor's ``accepted`` flag), **and**
- the stored report's ``acceptance.accepted`` is ``True``, **and**
- ``acceptance.failed_gates`` is empty.

Anything else -- failed runs, missing acceptance evidence, cancelled runs
-- becomes ``failed`` with the reason recorded. There is deliberately no
method that completes a stage without a verified run.
"""
from __future__ import annotations

import json
from pathlib import PurePosixPath
from typing import Any, Dict, List, Optional, Tuple

from forge.control.control_plane import (
    Conflict,
    InvalidRequest,
    NotFound,
    RunStatus,
    TERMINAL_STATUSES,
    validate_id,
)
from forge.staged.preview import (
    ENTRY_EXTENSIONS,
    IMAGE_EXTENSIONS,
    MAX_RAW_BYTES,
    PreviewError,
    media_type_for,
    normalize_preview_path,
    read_text_preview,
    resolve_under_root,
    scan_candidates,
)
from forge.staged.models import (
    MAX_BUILDS_PER_PROJECT,
    MAX_DESCRIPTION_CHARS,
    MAX_DOC_CHARS,
    MAX_NAME_CHARS,
    MAX_STAGES_PER_BUILD,
    MAX_STAGE_PROMPT_CHARS,
    MAX_STAGE_TITLE_CHARS,
    BuildProject,
    BuildStage,
    StageStatus,
    assemble_stage_requirement,
)


def _require_text(value: Any, name: str, *,
                  min_len: int = 0, max_len: int = 0) -> str:
    if not isinstance(value, str):
        raise InvalidRequest("%s must be a string." % name)
    text = value.strip()
    if len(text) < min_len:
        raise InvalidRequest("%s must be a non-empty string." % name)
    if max_len and len(value) > max_len:
        raise InvalidRequest(
            "%s exceeds %d characters." % (name, max_len))
    return text


def _optional_text(value: Any, name: str, *, max_len: int) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidRequest("%s must be a string." % name)
    if len(value) > max_len:
        raise InvalidRequest(
            "%s exceeds %d characters." % (name, max_len))
    return value


def _trim_result(result: Any) -> Any:
    """Bound verification evidence (drop verbose outputs, keep verdicts)."""
    if isinstance(result, dict):
        trimmed: Dict[str, Any] = {}
        for key, val in result.items():
            name = str(key).lower()
            if name in ("stdout", "stderr", "output", "log", "logs",
                        "traceback", "details_text"):
                trimmed[str(key)] = {"chars": len(str(val))}
            elif isinstance(val, str) and len(val) > 2000:
                trimmed[str(key)] = val[:2000] + "...[truncated]"
            elif isinstance(val, list) and len(val) > 100:
                trimmed[str(key)] = val[:100] + ["...[truncated]"]
            elif isinstance(val, dict):
                trimmed[str(key)] = _trim_result(val)
            else:
                trimmed[str(key)] = val
        return trimmed
    if isinstance(result, list):
        return [_trim_result(item) for item in result[:100]]
    if isinstance(result, str) and len(result) > 2000:
        return result[:2000] + "...[truncated]"
    return result


def verify_run_accepted(run: Any) -> "tuple[bool, str]":
    """Strict completion check for a linked run.

    Returns ``(ok, reason)``. ``ok`` is True only for a genuinely
    accepted run; the reason names exactly what failed otherwise.
    """
    try:
        status = run.status
    except AttributeError:
        return False, "Linked run has no status."
    if status != RunStatus.SUCCEEDED:
        error = str(getattr(run, "error", "") or "")
        return False, error or "Run did not succeed (status %s)." % (
            getattr(status, "value", status),)
    try:
        report = run.report()
    except Exception:
        return False, "Run report is unreadable."
    if not isinstance(report, dict):
        return False, "Run report is missing."
    acceptance = report.get("acceptance", {})
    if not isinstance(acceptance, dict) or not acceptance:
        return False, "Acceptance evidence is missing from the run report."
    if acceptance.get("accepted") is not True:
        gates = acceptance.get("failed_gates", [])
        names = ", ".join(str(gate) for gate in gates) if gates else "?"
        return False, "Acceptance refused (failed gates: %s)." % names
    failed = acceptance.get("failed_gates", [])
    if failed:
        names = ", ".join(str(gate) for gate in failed)
        return False, "Acceptance failed gates: %s." % names
    return True, ""


class StagedBuilds:
    """Stage-by-stage build orchestration over a control plane."""

    def __init__(self, plane: Any) -> None:
        self._plane = plane

    @property
    def _store(self):  # lazy so test fakes can inject their own
        store = getattr(self._plane, "_staged_store", None)
        if store is None:
            from forge.staged.store import StagedStore

            store = StagedStore(self._plane._db)
            self._plane._staged_store = store
        return store

    # -- builds -----------------------------------------------------------

    def create_build(self, session: Any, name: str, *,
                     description: str = "", roadmap: str = "",
                     blueprint: str = "") -> BuildProject:
        name = _require_text(name, "name", min_len=1,
                             max_len=MAX_NAME_CHARS)
        description = _require_text(
            description, "description", max_len=MAX_DESCRIPTION_CHARS)
        roadmap = _require_text(roadmap, "roadmap", max_len=MAX_DOC_CHARS)
        blueprint = _require_text(
            blueprint, "blueprint", max_len=MAX_DOC_CHARS)
        if self._store.count_builds(session.project_id) >= MAX_BUILDS_PER_PROJECT:
            raise Conflict("Build project limit reached (%d)."
                           % MAX_BUILDS_PER_PROJECT)
        return self._store.create_build(
            session.project_id, name, description=description,
            roadmap=roadmap, blueprint=blueprint,
            created_by=getattr(session, "actor", "") or "")

    def list_builds(self, session: Any) -> List[Dict[str, Any]]:
        summaries: List[Dict[str, Any]] = []
        for build in self._store.list_builds(session.project_id):
            stages = self._store.list_stages(build.id)
            self._sync_stages(stages)
            counts = self._counts(stages)
            payload = build.to_dict()
            payload["progress"] = counts
            summaries.append(payload)
        return summaries

    def get_board(self, session: Any, build_id: str) -> Dict[str, Any]:
        build = self._get_build(session, build_id)
        stages = self._store.list_stages(build.id)
        self._sync_stages(stages)
        counts = self._counts(stages)
        current: Optional[int] = None
        for stage in stages:
            if stage.status != StageStatus.COMPLETED:
                current = stage.position
                break
        active_run = ""
        for stage in stages:
            if stage.status == StageStatus.RUNNING and stage.run_id:
                active_run = stage.run_id
                break
        return {
            "build": build.to_dict(include_docs=True),
            "stages": [stage.to_dict() for stage in stages],
            "progress": counts,
            "current_position": current,
            "all_complete": bool(stages) and current is None,
            "active_run_id": active_run,
        }

    def update_build(self, session: Any, build_id: str, *,
                     name: Optional[str] = None,
                     description: Optional[str] = None,
                     roadmap: Optional[str] = None,
                     blueprint: Optional[str] = None) -> BuildProject:
        build = self._get_build(session, build_id)
        fields: Dict[str, Any] = {}
        if name is not None:
            fields["name"] = _require_text(
                name, "name", min_len=1, max_len=MAX_NAME_CHARS)
        if description is not None:
            fields["description"] = _require_text(
                description, "description", max_len=MAX_DESCRIPTION_CHARS)
        if roadmap is not None:
            fields["roadmap"] = _optional_text(
                roadmap, "roadmap", max_len=MAX_DOC_CHARS)
        if blueprint is not None:
            fields["blueprint"] = _optional_text(
                blueprint, "blueprint", max_len=MAX_DOC_CHARS)
        fields = {key: val for key, val in fields.items()
                  if val is not None}
        if not fields:
            return build
        updated = self._store.update_build(build.id, **fields)
        if updated is None:  # pragma: no cover - defensive
            raise NotFound("Unknown build project.")
        return updated

    def delete_build(self, session: Any, build_id: str) -> Dict[str, Any]:
        build = self._get_build(session, build_id)
        stages = self._store.list_stages(build.id)
        self._sync_stages(stages)
        for stage in stages:
            if stage.status == StageStatus.RUNNING:
                raise Conflict(
                    "Stage %d is still running; wait for it to finish "
                    "before deleting." % stage.position)
        self._store.delete_build(build.id)
        return {"build_id": build.id, "deleted": True}

    # -- stages -----------------------------------------------------------

    def add_stages(self, session: Any, build_id: str,
                   items: List[Dict[str, Any]]) -> List[BuildStage]:
        build = self._get_build(session, build_id)
        if not items:
            raise InvalidRequest("Provide at least one stage.")
        existing = self._store.list_stages(build.id)
        if len(existing) + len(items) > MAX_STAGES_PER_BUILD:
            raise Conflict("Stage limit reached (%d per build)."
                           % MAX_STAGES_PER_BUILD)
        created: List[BuildStage] = []
        for item in items:
            if not isinstance(item, dict):
                raise InvalidRequest("Each stage must be an object.")
            title = _require_text(item.get("title", ""), "title",
                                  min_len=1, max_len=MAX_STAGE_TITLE_CHARS)
            prompt = _require_text(item.get("prompt", ""), "prompt",
                                   min_len=1,
                                   max_len=MAX_STAGE_PROMPT_CHARS)
            created.append(self._store.add_stage(build, title, prompt))
        self._store.touch_build(build.id)
        return created

    def update_stage(self, session: Any, build_id: str, position: int, *,
                     title: Optional[str] = None,
                     prompt: Optional[str] = None) -> BuildStage:
        build = self._get_build(session, build_id)
        stage = self._get_stage(build, position)
        self._sync_stages([stage])
        if stage.status not in (StageStatus.PENDING, StageStatus.FAILED):
            raise Conflict(
                "Stage %d is %s; only pending or failed stages can be "
                "edited." % (stage.position, stage.status.value))
        if title is not None:
            stage.title = _require_text(
                title, "title", min_len=1, max_len=MAX_STAGE_TITLE_CHARS)
        if prompt is not None:
            stage.prompt = _require_text(
                prompt, "prompt", min_len=1, max_len=MAX_STAGE_PROMPT_CHARS)
        self._store.save_stage(stage)
        self._store.touch_build(build.id)
        return stage

    def delete_stage(self, session: Any, build_id: str,
                     position: int) -> Dict[str, Any]:
        build = self._get_build(session, build_id)
        stages = self._store.list_stages(build.id)
        self._sync_stages(stages)
        for stage in stages:
            if stage.status == StageStatus.RUNNING:
                raise Conflict(
                    "Stage %d is still running; wait for it to finish "
                    "before changing stages." % stage.position)
        stage = self._get_stage(build, position)
        if stage.status != StageStatus.PENDING:
            raise Conflict(
                "Stage %d is %s; only pending stages can be deleted."
                % (stage.position, stage.status.value))
        self._store.delete_stage(stage)
        self._store.touch_build(build.id)
        return {"build_id": build.id, "position": position,
                "deleted": True}

    # -- execution: strictly one stage at a time --------------------------

    def run_next(self, session: Any, build_id: str, *,
                 mode: str = "") -> Dict[str, Any]:
        build = self._get_build(session, build_id)
        stages = self._store.list_stages(build.id)
        if not stages:
            raise Conflict("Add at least one stage before running.")
        self._sync_stages(stages)
        for stage in stages:
            if stage.status != StageStatus.COMPLETED:
                return self._start_stage(session, build, stages, stage,
                                         mode=mode)
        raise Conflict("All stages are already verified complete.")

    def run_stage(self, session: Any, build_id: str, position: int, *,
                  mode: str = "") -> Dict[str, Any]:
        build = self._get_build(session, build_id)
        stages = self._store.list_stages(build.id)
        if not stages:
            raise Conflict("Add at least one stage before running.")
        self._sync_stages(stages)
        stage = self._get_stage(build, position)
        # Re-read the synced row (sync may have replaced statuses).
        for synced in stages:
            if synced.position == stage.position:
                stage = synced
                break
        return self._start_stage(session, build, stages, stage, mode=mode)

    def stage_evidence(self, session: Any, build_id: str,
                       position: int) -> Dict[str, Any]:
        build = self._get_build(session, build_id)
        stage = self._get_stage(build, position)
        self._sync_stages([stage])
        payload: Dict[str, Any] = {
            "build_id": build.id,
            "stage": stage.to_dict(),
            "evidence": dict(stage.evidence),
            "run": None,
        }
        if stage.run_id:
            run = self._plane.runs.get(stage.run_id)
            if run is not None:
                payload["run"] = self._run_summary(run)
        return payload

    # -- live preview: show what Forge is making --------------------------

    def get_preview(self, session: Any, build_id: str) -> Dict[str, Any]:
        """Preview metadata: entry page, candidates, per-stage files."""
        build = self._get_build(session, build_id)
        stages = self._store.list_stages(build.id)
        self._sync_stages(stages)
        root = self._project_root(session)
        candidates = scan_candidates(root)
        entry_exists = False
        if build.preview_entry:
            try:
                entry_exists = resolve_under_root(
                    root, build.preview_entry).is_file()
            except (PreviewError, OSError):
                entry_exists = False
        files_made: List[Dict[str, Any]] = []
        for stage in stages:
            files: List[str] = []
            if stage.status == StageStatus.COMPLETED:
                changed = stage.evidence.get("files_changed", [])
                if isinstance(changed, list):
                    files = [str(path) for path in changed][:200]
            files_made.append({
                "position": stage.position,
                "title": stage.title,
                "status": stage.status.value,
                "run_id": stage.run_id,
                "files": files,
            })
        return {
            "build_id": build.id,
            "entry": build.preview_entry,
            "entry_exists": entry_exists,
            "candidates": candidates,
            "files_made": files_made,
        }

    def set_preview_entry(self, session: Any, build_id: str,
                          entry: Any) -> BuildProject:
        """Choose which HTML file the live preview renders ("" clears)."""
        build = self._get_build(session, build_id)
        if entry is None:
            entry = ""
        if not isinstance(entry, str):
            raise InvalidRequest("Preview entry must be a string.")
        entry = entry.strip()
        if entry:
            try:
                normalized = normalize_preview_path(entry)
            except PreviewError as exc:
                raise InvalidRequest(str(exc)) from None
            if (PurePosixPath(normalized).suffix.lower()
                    not in ENTRY_EXTENSIONS):
                raise InvalidRequest(
                    "Preview entry must be an HTML file.")
            try:
                target = resolve_under_root(
                    self._project_root(session), normalized)
            except PreviewError as exc:
                raise InvalidRequest(str(exc)) from None
            try:
                exists = target.is_file()
            except OSError:
                exists = False
            if not exists:
                raise InvalidRequest("Preview entry does not exist.")
            entry = normalized
        updated = self._store.update_build(build.id, preview_entry=entry)
        if updated is None:  # pragma: no cover - defensive
            raise NotFound("Unknown build project.")
        return updated

    def read_preview_file(self, session: Any, build_id: str,
                          path: Any) -> Dict[str, Any]:
        """Read one project file for the content viewer (bounded)."""
        build = self._get_build(session, build_id)
        try:
            normalized = normalize_preview_path(path)
            target = resolve_under_root(
                self._project_root(session), normalized)
        except PreviewError as exc:
            raise InvalidRequest(str(exc)) from None
        try:
            is_file = target.is_file()
            size = target.stat().st_size if is_file else 0
        except OSError:
            raise NotFound("Unknown preview file.") from None
        if not is_file:
            raise NotFound("Unknown preview file.")
        suffix = PurePosixPath(normalized).suffix.lower()
        if suffix in IMAGE_EXTENSIONS:
            return {"build_id": build.id, "path": normalized,
                    "kind": "image", "size": int(size),
                    "content": None, "truncated": False}
        try:
            text, truncated = read_text_preview(target)
        except PreviewError as exc:
            raise NotFound(str(exc)) from None
        if text is None:
            return {"build_id": build.id, "path": normalized,
                    "kind": "binary", "size": int(size),
                    "content": None, "truncated": False}
        return {"build_id": build.id, "path": normalized,
                "kind": "text", "size": int(size),
                "content": text, "truncated": truncated}

    def resolve_preview_raw(self, session: Any, build_id: str,
                            path: Any) -> Tuple[str, str]:
        """Resolve one allowlisted file to ``(abspath, media_type)``."""
        build = self._get_build(session, build_id)
        try:
            normalized = normalize_preview_path(path)
            target = resolve_under_root(
                self._project_root(session), normalized)
        except PreviewError as exc:
            raise InvalidRequest(str(exc)) from None
        media = media_type_for(normalized)
        if media is None:
            raise NotFound("Preview file is not servable.")
        try:
            is_file = target.is_file()
            size = target.stat().st_size if is_file else 0
        except OSError:
            raise NotFound("Unknown preview file.") from None
        if not is_file:
            raise NotFound("Unknown preview file.")
        if size > MAX_RAW_BYTES:
            raise InvalidRequest("Preview file is too large.")
        return str(target), media

    def _project_root(self, session: Any) -> str:
        project = self._plane.get_project(session.project_id)
        return str(project.root)

    # -- internals --------------------------------------------------------

    def _get_build(self, session: Any, build_id: str) -> BuildProject:
        validate_id(build_id, kind="build id")
        build = self._store.get_build(build_id)
        # Cross-project ids map to NOT_FOUND: existence must not leak.
        if build is None or build.project_id != session.project_id:
            raise NotFound("Unknown build project: %r" % (build_id,))
        return build

    def _get_stage(self, build: BuildProject, position: int) -> BuildStage:
        try:
            wanted = int(position)
        except (TypeError, ValueError):
            raise NotFound("Unknown stage: %r" % (position,)) from None
        stage = self._store.get_stage(build.id, wanted)
        if stage is None:
            raise NotFound("Unknown stage: %r" % (position,))
        return stage

    @staticmethod
    def _counts(stages: List[BuildStage]) -> Dict[str, int]:
        counts = {"total": len(stages), "completed": 0, "failed": 0,
                  "running": 0, "pending": 0}
        for stage in stages:
            if stage.status == StageStatus.COMPLETED:
                counts["completed"] += 1
            elif stage.status == StageStatus.FAILED:
                counts["failed"] += 1
            elif stage.status == StageStatus.RUNNING:
                counts["running"] += 1
            else:
                counts["pending"] += 1
        return counts

    def _sync_stages(self, stages: List[BuildStage]) -> None:
        """Refresh stage rows from their linked runs (source of truth).

        Only ``running`` stages move, and only based on the run record:
        verified acceptance -> ``completed``, any other terminal state ->
        ``failed``. Completed stages are never touched again.
        """
        for stage in stages:
            if stage.status != StageStatus.RUNNING:
                continue
            if not stage.run_id:
                stage.status = StageStatus.PENDING
                self._store.save_stage(stage)
                continue
            run = self._plane.runs.get(stage.run_id)
            if run is None:
                stage.status = StageStatus.FAILED
                stage.error = "Linked run record is missing."
                stage.evidence = {"verified": False,
                                  "run_id": stage.run_id,
                                  "reason": stage.error}
                self._store.save_stage(stage)
                continue
            if run.status not in TERMINAL_STATUSES:
                continue
            ok, reason = verify_run_accepted(run)
            if ok:
                stage.status = StageStatus.COMPLETED
                stage.error = ""
                stage.evidence = self._build_evidence(stage, run)
            else:
                stage.status = StageStatus.FAILED
                stage.error = reason[:2000]
                stage.evidence = {
                    "verified": False, "run_id": run.id,
                    "reason": reason[:2000],
                    "run_status": getattr(run.status, "value",
                                          str(run.status)),
                }
            self._store.save_stage(stage)

    def _start_stage(self, session: Any, build: BuildProject,
                     stages: List[BuildStage], stage: BuildStage, *,
                     mode: str = "") -> Dict[str, Any]:
        if stage.status == StageStatus.COMPLETED:
            raise Conflict(
                "Stage %d is already verified complete; verified stages "
                "are immutable." % stage.position)
        if stage.status == StageStatus.RUNNING:
            raise Conflict("Stage %d is already running."
                           % stage.position)
        if stage.status not in (StageStatus.PENDING, StageStatus.FAILED):
            raise Conflict("Stage %d cannot run from state %s."
                           % (stage.position, stage.status.value))
        for other in stages:
            if other.status == StageStatus.RUNNING:
                raise Conflict(
                    "Stage %d is still running; finish it before starting "
                    "stage %d." % (other.position, stage.position))
        for other in sorted(stages, key=lambda item: item.position):
            if other.position >= stage.position:
                break
            if other.status != StageStatus.COMPLETED:
                raise Conflict(
                    "Stage %d (%s) is not verified complete yet; stages "
                    "run strictly in order."
                    % (other.position, other.status.value))
        requirement, snapshot = assemble_stage_requirement(
            build, stages, stage.position)
        run = self._plane.submit_task(session, requirement, mode=mode or "")
        stage.run_id = run.id
        stage.attempts += 1
        if run.id not in stage.runs:
            stage.runs.append(run.id)
        stage.status = StageStatus.RUNNING
        stage.error = ""
        stage.evidence = {}
        stage.docs_snapshot = snapshot
        self._store.save_stage(stage)
        self._store.touch_build(build.id)
        return {
            "build_id": build.id,
            "position": stage.position,
            "stage": stage.to_dict(),
            "run": run.to_dict(),
        }

    def _build_evidence(self, stage: BuildStage,
                        run: Any) -> Dict[str, Any]:
        try:
            report = run.report()
        except Exception:
            report = {}
        if not isinstance(report, dict):
            report = {}
        try:
            files = [str(path) for path in run.files()][:200]
        except Exception:
            files = []
        changed = report.get("files_changed", [])
        if not isinstance(changed, list):
            changed = []
        acceptance = report.get("acceptance", {})
        if not isinstance(acceptance, dict):
            acceptance = {}
        finished = getattr(run, "finished_at", None)
        summary = ("Stage %d %r: %d file(s) changed, tests passed, "
                   "acceptance granted (run %s)."
                   % (stage.position, stage.title, len(changed),
                      str(getattr(run, "id", ""))[:14]))
        evidence = {
            "verified": True,
            "run_id": getattr(run, "id", ""),
            "accepted": True,
            "failed_gates": list(acceptance.get("failed_gates", [])),
            "tests": _trim_result(report.get("test_result", {})),
            "review": _trim_result(report.get("review_result", {})),
            "security": _trim_result(report.get("security_result", {})),
            "build": _trim_result(report.get("build_result", {})),
            "benchmark": _trim_result(report.get("benchmark", {})),
            "files_changed": [str(path) for path in changed][:200],
            "files": files,
            "checkpoint_id": str(getattr(run, "checkpoint_id", "") or ""),
            "model": str(getattr(run, "model", "") or ""),
            "provider": str(getattr(run, "provider", "") or ""),
            "retries": report.get("retries", 0),
            "duration_seconds": report.get("duration_seconds", 0.0),
            "finished_at": finished,
            "docs_snapshot": dict(stage.docs_snapshot),
            "summary": summary,
        }
        # Hard cap so one verbose report cannot bloat the database.
        blob = json.dumps(evidence, default=str)
        if len(blob) > 60000:
            for key in ("tests", "review", "security", "build",
                        "benchmark"):
                evidence[key] = {"omitted": "evidence too large"}
            evidence["summary"] = summary
        return evidence

    def _run_summary(self, run: Any) -> Dict[str, Any]:
        try:
            report = run.report()
        except Exception:
            report = {}
        acceptance = report.get("acceptance", {}) if isinstance(
            report, dict) else {}
        try:
            files = [str(path) for path in run.files()][:200]
        except Exception:
            files = []
        return {
            "task_id": getattr(run, "id", ""),
            "status": getattr(getattr(run, "status", ""), "value",
                              str(getattr(run, "status", ""))),
            "stage": str(getattr(run, "stage", "") or ""),
            "model": str(getattr(run, "model", "") or ""),
            "provider": str(getattr(run, "provider", "") or ""),
            "files": files,
            "error": str(getattr(run, "error", "") or ""),
            "acceptance": _trim_result(acceptance),
        }
