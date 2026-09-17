from __future__ import annotations

from pathlib import Path


def replace_between(path: Path, start_marker: str, end_marker: str, replacement: str) -> None:
    text = path.read_text(encoding="utf-8")
    if start_marker not in text:
        raise SystemExit(f"missing start marker in {path}: {start_marker!r}")
    start = text.index(start_marker)
    end = text.index(end_marker, start)
    path.write_text(text[:start] + replacement + text[end:], encoding="utf-8")


def patch_dag() -> None:
    path = Path("forge/core/dag_scheduler.py")
    text = path.read_text(encoding="utf-8")
    marker = "Durable persisted-task adoption for restart-safe graph reconstruction"
    if marker in text:
        return
    new_add = '''    def add_task(self, task_id: str, description: str = "", *,
                 depends_on: Sequence[str] = (), timeout: Optional[float] = None,
                 max_attempts: int = 1, resources: Sequence[str] = (),
                 priority: int = 0, metadata: Optional[dict] = None) -> ScheduledTask:
        """Add a graph node, adopting an existing durable definition safely.

        Multi-agent orchestration rebuilds the deterministic graph after a
        process restart. A persisted task id therefore means "resume this
        exact node", not "overwrite its durable state with a fresh QUEUED row".
        Static task configuration must match; persisted lifecycle state is
        recovered with the same fail-closed rules used by ``_recover()``.
        """
        task_id = str(task_id or "").strip()
        if not task_id:
            raise DAGSchedulerError("task_id must be non-empty")
        if task_id in self._tasks:
            raise DAGSchedulerError("task already exists: %s" % task_id)
        deps = tuple(str(d) for d in (depends_on or ()))
        if any(d == task_id for d in deps):
            raise DAGSchedulerError(
                "task %s depends on itself" % task_id)
        if max_attempts < 1:
            raise DAGSchedulerError("max_attempts must be >= 1")
        if timeout is not None and timeout <= 0:
            raise DAGSchedulerError("timeout must be positive")
        resources_tuple = tuple(str(r) for r in (resources or ()))
        description_text = str(description or "")
        priority_value = int(priority)

        # Durable persisted-task adoption for restart-safe graph reconstruction.
        persisted = None
        if self._conn is not None:
            with self._db_lock:
                persisted = self._conn.execute(
                    "SELECT task_id, description, dependencies, timeout, "
                    "max_attempts, resources, priority, state, attempts, "
                    "error, output, created_at FROM tasks WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
        if persisted is not None:
            try:
                persisted_deps = tuple(
                    str(item) for item in json.loads(
                        persisted["dependencies"] or "[]"))
                persisted_resources = tuple(
                    str(item) for item in json.loads(
                        persisted["resources"] or "[]"))
                persisted_timeout = persisted["timeout"]
                persisted_timeout = (
                    float(persisted_timeout)
                    if persisted_timeout is not None else None)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise DAGSchedulerError(
                    "corrupt persisted task definition: %s" % exc) from None

            definition_matches = (
                str(persisted["description"] or "") == description_text
                and persisted_deps == deps
                and persisted_timeout == timeout
                and int(persisted["max_attempts"] or 1) == int(max_attempts)
                and persisted_resources == resources_tuple
                and int(persisted["priority"] or 0) == priority_value
            )
            if not definition_matches:
                raise DAGSchedulerError(
                    "persisted task definition mismatch: %s" % task_id)
            for dep in persisted_deps:
                if dep not in self._tasks:
                    raise DAGSchedulerError(
                        "task %s depends on unknown task %s" % (task_id, dep))

            persisted_state = str(persisted["state"] or "").strip()
            attempts = int(persisted["attempts"] or 0)
            persisted_max_attempts = int(persisted["max_attempts"] or 1)
            reason = str(persisted["error"] or "")
            if persisted_state in TERMINAL_STATES:
                recovered_state = persisted_state
            elif persisted_state == QUEUED:
                recovered_state = QUEUED
            elif persisted_state == CANCELLING:
                recovered_state = CANCELLED
                reason = "worker lost during cancellation (restart)"
            elif persisted_state == RUNNING:
                if attempts < persisted_max_attempts:
                    recovered_state = QUEUED
                    reason = "interrupted at generation %d; re-queued" % (
                        attempts + 1)
                else:
                    recovered_state = FAILED
                    reason = "interrupted by restart; attempts exhausted"
            else:
                recovered_state = FAILED
                reason = "unknown persisted task state: %s" % (
                    persisted_state or "<empty>")

            task = ScheduledTask(
                task_id=task_id,
                description=description_text,
                depends_on=persisted_deps,
                timeout=persisted_timeout,
                max_attempts=persisted_max_attempts,
                resources=persisted_resources,
                priority=int(persisted["priority"] or 0),
                metadata=dict(metadata or {}),
                state=recovered_state,
                attempts_used=attempts,
                error=reason[:MAX_TASK_ERROR],
                output=str(persisted["output"] or "")[:4000],
                created_at=float(persisted["created_at"] or time.time()),
            )
            self._tasks[task_id] = task
            if recovered_state != persisted_state or reason != str(persisted["error"] or ""):
                self._persist_task(task)
                self._record_event(
                    task_id, "-", attempts, recovered_state,
                    "recovered persisted task from %s" % (
                        persisted_state or "unknown"),
                )
            return task

        for dep in deps:
            if dep not in self._tasks:
                raise DAGSchedulerError(
                    "task %s depends on unknown task %s" % (task_id, dep))
        task = ScheduledTask(
            task_id=task_id, description=description_text,
            depends_on=deps, timeout=timeout,
            max_attempts=int(max_attempts),
            resources=resources_tuple,
            priority=priority_value,
            metadata=dict(metadata or {}))
        self._tasks[task_id] = task
        # Optimization: Full graph cycle detection is deferred to run() or manual check.
        # Since add_task requires all dependencies to already exist in self._tasks and new
        # tasks have no dependents yet, adding a node to an acyclic graph cannot introduce a cycle.
        # Removing _detect_cycle() here turns O(N^2) task insertion into O(1).
        self._persist_task(task)
        self._record_event(task_id, "-", 0, QUEUED, "task added")
        return task

'''
    replace_between(path, "    def add_task(", "    def _detect_cycle(", new_add)


def patch_control_plane() -> None:
    path = Path("forge/control/control_plane.py")
    text = path.read_text(encoding="utf-8")
    if "self._resume_orchestrations()" not in text:
        old = '''        self._dispatcher = threading.Thread(
            target=self._dispatch_loop, name="forge-dispatch", daemon=True)
        self._dispatcher.start()
'''
        new = '''        self._dispatcher = threading.Thread(
            target=self._dispatch_loop, name="forge-dispatch", daemon=True)
        self._dispatcher.start()
        self._resume_orchestrations()
'''
        if old not in text:
            raise SystemExit("could not patch ControlPlane.start")
        text = text.replace(old, new, 1)

    if "    def _resume_orchestrations(self) -> None:" not in text:
        anchor = "    def _recover_interrupted(self) -> None:\n"
        method = '''    def _resume_orchestrations(self) -> None:
        """Requeue durable A38 orchestrations after a backend restart.

        Only still-active sessions are resumed. QUEUED/RUNNING/
        WAITING_APPROVAL records are moved back to a single durable
        QUEUED boundary and dispatched once; the persisted DAG ledger
        prevents already-finished steps from being executed again.
        """
        try:
            rows = self._db.query(
                "SELECT * FROM orchestrations "
                "WHERE status IN ('QUEUED', 'RUNNING', 'WAITING_APPROVAL') "
                "ORDER BY created_at ASC")
        except Exception:
            return
        for row in rows:
            try:
                record = self.orchestrations._from_row(row)
                session = self.sessions.get(record.session_id)
                if session is None or not session.active:
                    self.orchestrations.compare_and_set(
                        record.id, record.version,
                        status=OrchestrationStatus.FAILED,
                        stage="failed",
                        error="Session inactive; orchestration not resumed after restart.",
                        finished_at=time.time())
                    continue
                stored_plan = record.plan()
                chain = bool(stored_plan.get("chain", False))
                updated = self.orchestrations.compare_and_set(
                    record.id, record.version,
                    status=OrchestrationStatus.QUEUED,
                    stage="recovered",
                    started_at=None,
                    finished_at=None,
                    error="")
                if updated is None:
                    continue
                self._submit_tracked(
                    self._execute_orchestration, record.id, chain)
            except Exception as exc:
                try:
                    self._audit(
                        "forge", "orchestration", "resume", False,
                        task_id=str(row[0]), reason=str(exc)[:300])
                except Exception:
                    pass

'''
        if anchor not in text:
            raise SystemExit("could not find recovery anchor in control_plane.py")
        text = text.replace(anchor, method + anchor, 1)

    old_submit = '''        record = self.orchestrations.create(
            session_id=session.id, project_id=project.id,
            requirement=requirement, actor=session.actor)
        self._emit(record.id, record.project_id, "orchestration.created",
'''
    new_submit = '''        record = self.orchestrations.create(
            session_id=session.id, project_id=project.id,
            requirement=requirement, actor=session.actor)
        # Persist the execution mode before any worker can start. A queued
        # orchestration can therefore recover its original chain semantics.
        record = self.orchestrations.mutate(
            record.id, plan_json=json.dumps({"chain": bool(chain)})) or record
        self._emit(record.id, record.project_id, "orchestration.created",
'''
    if "Persist the execution mode before any worker can start" not in text:
        if old_submit not in text:
            raise SystemExit("could not patch orchestration submit persistence")
        text = text.replace(old_submit, new_submit, 1)

    old_plan = '''            plan = orchestrator.build_plan(record.requirement, chain=chain)
            self.orchestrations.mutate(
                record.id, stage="running",
                plan_json=json.dumps(plan.to_dict(), default=str))
            report = orchestrator.execute(plan)
'''
    new_plan = '''            stored_plan = record.plan()
            stored_chain = bool(stored_plan.get("chain", chain))
            stored_steps = stored_plan.get("steps")
            if isinstance(stored_steps, list) and stored_steps:
                from forge.core.orchestrator import OrchestrationPlan, OrchestrationStep
                restored_steps = []
                for raw in stored_steps:
                    if not isinstance(raw, dict):
                        raise ValueError("malformed persisted orchestration step")
                    restored_steps.append(OrchestrationStep(
                        id=str(raw.get("id", "")),
                        agent=str(raw.get("agent", "")),
                        role=str(raw.get("role", "")),
                        capability=str(raw.get("capability", "")),
                        instructions=str(raw.get("instructions", "")),
                        depends_on=tuple(str(item) for item in (raw.get("depends_on") or ())),
                    ))
                plan = OrchestrationPlan(
                    requirement=str(stored_plan.get("requirement") or record.requirement),
                    steps=tuple(restored_steps),
                )
                plan.validate(registry)
            else:
                plan = orchestrator.build_plan(record.requirement, chain=stored_chain)
            plan_payload = plan.to_dict()
            plan_payload["chain"] = stored_chain
            self.orchestrations.mutate(
                record.id, stage="running",
                plan_json=json.dumps(plan_payload, default=str))
            report = orchestrator.execute(plan)
'''
    if "stored_steps = stored_plan.get(\"steps\")" not in text:
        if old_plan not in text:
            raise SystemExit("could not patch persisted orchestration plan")
        text = text.replace(old_plan, new_plan, 1)

    path.write_text(text, encoding="utf-8")


def main() -> None:
    patch_dag()
    patch_control_plane()
    print("durable orchestration recovery patch applied")


if __name__ == "__main__":
    main()
