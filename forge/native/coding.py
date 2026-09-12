"""Native coding engine: propose → validate → authorize → apply → test.

This is A81 layer 6 — a real engineering workflow, not a demo printer. It
adds no new write path: every step is delegated to the existing, already
audited layers, so the native engine and the Supervisor share one security
model (requirement: reuse, never duplicate):

* proposals only ever come from a neural reasoning backend through the hub;
  the deterministic backend refuses with ``NEURAL_REQUIRED`` and this module
  propagates the refusal honestly (``ProposalResult.refused``) instead of
  inventing code;
* validation is the A32 ChangeSet engine's ``dry_run`` (path safety, secrets,
  invalid Python, size bounds — zero writes);
* application is ``ChangeApplier.apply`` — per-change policy-gate decisions,
  checkpoint-before-write, structured failures;
* test execution uses the constrained ``run_tests`` tool (project pytest
  only, no shell) with the same path sanitization as ``TestDebugLoop``.

A denial or a failure is returned as data (never raised away, never
flattened into success) so the engine can record it, roll back, and report
it.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Any, Dict, List, Optional, Sequence, Tuple

from forge.native.reasoning import ReasoningKind, ReasoningRequest, RefusalCode


@dataclass
class InspectResult:
    path: str
    ok: bool
    content: str = ""
    error: str = ""
    chars: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {"path": self.path, "ok": self.ok, "chars": self.chars,
                "error": self.error[:400]}


@dataclass
class ProposalResult:
    """A change proposal with full provenance (or its honest refusal)."""

    ok: bool
    changes: Dict[str, str] = field(default_factory=dict)
    explanation: str = ""
    refusal: str = ""
    message: str = ""
    model: str = ""
    provider: str = ""
    generated_by: str = ""
    latency_ms: float = 0.0
    #: Raw model text (for opt-in dataset capture only; never re-emitted as
    #: if it were verified output).
    raw: str = ""

    @property
    def requires_model(self) -> bool:
        return self.refusal == RefusalCode.NEURAL_REQUIRED.value

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "paths": sorted(self.changes),
            "explanation": self.explanation[:800],
            "refusal": self.refusal,
            "message": self.message[:400],
            "model": self.model,
            "provider": self.provider,
            "generated_by": self.generated_by,
            "latency_ms": self.latency_ms,
            "requires_model": self.requires_model,
        }


@dataclass
class ValidationIssue:
    path: str
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {"path": self.path, "reason": self.reason}


@dataclass
class ValidationReport:
    ok: bool
    would_change: List[str] = field(default_factory=list)
    issues: List[ValidationIssue] = field(default_factory=list)
    decisions: List[Dict[str, Any]] = field(default_factory=list)
    fingerprint: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "would_change": list(self.would_change),
            "issues": [i.to_dict() for i in self.issues],
            "decisions": [dict(d) for d in self.decisions],
            "fingerprint": self.fingerprint,
        }


@dataclass
class ApplyOutcome:
    ok: bool
    files: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    decisions: List[Dict[str, Any]] = field(default_factory=list)
    checkpoint_id: str = ""
    label: str = ""
    blocked_by_approval: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "files": list(self.files),
            "errors": [e[:400] for e in self.errors],
            "decision_count": len(self.decisions),
            "checkpoint_id": self.checkpoint_id,
            "label": self.label,
            "blocked_by_approval": self.blocked_by_approval,
        }


@dataclass
class TestRun:
    """One real test execution (result recorded exactly as measured)."""

    command: List[str] = field(default_factory=list)
    exit_code: Optional[int] = None
    passed: bool = False
    no_tests: bool = False
    output: str = ""
    duration_seconds: float = 0.0
    error: str = ""

    @property
    def executed(self) -> bool:
        return self.exit_code is not None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "command": list(self.command),
            "exit_code": self.exit_code,
            "passed": self.passed,
            "no_tests": self.no_tests,
            "executed": self.executed,
            "output_tail": self.output[-3000:],
            "output_chars": len(self.output),
            "duration_seconds": round(self.duration_seconds, 3),
            "error": self.error[:400],
        }


class NativeCodingEngine:
    """The gated path from reasoning output to repository writes."""

    #: Cap proposal payloads so a chatty model cannot exhaust the tiny host.
    MAX_FILE_BYTES = 512 * 1024
    MAX_FILES_PER_PROPOSAL = 40

    def __init__(self, root: str | Path, runtime: Any,
                 hub: Any = None, applier: Any = None,
                 checkpoint_manager: Any = None,
                 max_output_chars: int = 6000) -> None:
        self.root = Path(root).resolve()
        self.runtime = runtime
        self.hub = hub
        self.max_output_chars = int(max_output_chars)
        self.applier = applier
        if self.applier is None:
            from forge.tools.change_applier import ChangeApplier
            self.applier = ChangeApplier(runtime, root=str(self.root))
        #: Run-level rollback state belongs to the *engine* (same pattern as
        #: the A32 supervisor): the applier deliberately carries no
        #: checkpoint manager so a bounded debug loop with several applies
        #: does not snapshot the tree over and over. The manager is built
        #: lazily so analysis-only runs never pay for a snapshot.
        self._injected_checkpoint_manager = checkpoint_manager
        self._checkpoint_manager = None
        self._checkpoint = None

    @property
    def checkpoint_manager(self):
        if self._checkpoint_manager is None:
            if self._injected_checkpoint_manager is not None:
                self._checkpoint_manager = self._injected_checkpoint_manager
            else:
                from forge.tools.checkpoint import CheckpointManager
                self._checkpoint_manager = CheckpointManager(self.root)
        return self._checkpoint_manager

    # -- checkpoint lifecycle (one per engine run) --------------------------

    def open_checkpoint(self, label: str = "native-ai-run") -> str:
        """Snapshot current application files once per run (called lazily
        by the engine before the first authorized write)."""
        if self.checkpoint_manager is None:
            return ""
        if self._checkpoint is None:
            self._checkpoint = self.checkpoint_manager.create(label)
        return self._checkpoint.id

    @property
    def checkpoint_id(self) -> str:
        return self._checkpoint.id if self._checkpoint is not None else ""

    def rollback(self, files: Sequence[str]) -> bool:
        """Restore exactly this run's candidate files; no-op without a
        checkpoint. Cleanup afterwards so a rollback is not re-entered."""
        if self._checkpoint is None:
            return False
        self.checkpoint_manager.rollback(self._checkpoint, sorted(files))
        self.checkpoint_manager.cleanup(self._checkpoint)
        self._checkpoint = None
        return True

    def release_checkpoint(self) -> None:
        if self._checkpoint is not None:
            self.checkpoint_manager.cleanup(self._checkpoint)
            self._checkpoint = None

    # -- inspection ------------------------------------------------------------

    def inspect_file(self, path: str, max_chars: int = 20000) -> InspectResult:
        result = self.runtime.execute("read_file", path=path,
                                      actor="forge-native-ai")
        if result.success:
            content = (result.output or "")[:max_chars]
            return InspectResult(path=path, ok=True, content=content,
                                 chars=len(result.output or ""))
        return InspectResult(path=path, ok=False,
                             error=result.error or "read denied or failed")

    # -- proposal (neural-gated) --------------------------------------------------

    def propose_changes(self, task: str, context_text: str,
                        kind: ReasoningKind = ReasoningKind.GENERATE,
                        payload: Optional[Dict[str, Any]] = None
                        ) -> ProposalResult:
        if self.hub is None:
            return ProposalResult(
                False, refusal=RefusalCode.NOT_CONFIGURED.value,
                message="no reasoning hub attached to the coding engine")
        request = ReasoningRequest(
            kind=kind, task=task, context=context_text,
            payload=dict(payload or {}),
            max_output_chars=self.max_output_chars)
        result = self.hub.respond(request)
        if not result.ok:
            return ProposalResult(False,
                                  refusal=result.refusal,
                                  message=result.message,
                                  model=result.model,
                                  provider=result.provider,
                                  generated_by=result.generated_by,
                                  latency_ms=result.latency_ms)
        changes = result.data.get("changes") or {}
        safe: Dict[str, str] = {}
        for index, (path, content) in enumerate(sorted(changes.items())):
            if index >= self.MAX_FILES_PER_PROPOSAL:
                break
            if isinstance(path, str) and isinstance(content, str) \
                    and 0 < len(content.encode("utf-8")) <= self.MAX_FILE_BYTES:
                safe[path] = content
        return ProposalResult(
            ok=bool(safe),
            changes=safe,
            explanation=str(result.data.get("explanation") or ""),
            message="" if safe else "model proposed no usable changes",
            refusal="" if safe else RefusalCode.INVALID_MODEL_OUTPUT.value,
            model=result.model, provider=result.provider,
            generated_by=result.generated_by, latency_ms=result.latency_ms,
            raw=str(result.data.get("raw_text") or ""))

    # -- validation (zero writes) --------------------------------------------------

    def _to_code_changes(self, changes: Dict[str, str],
                         risk: str = "LOW"):
        from forge.tools.change_applier import CodeChange
        items = []
        for path, content in sorted(changes.items()):
            items.append(CodeChange(path=path, content=content,
                                    risk=risk))
        return items

    def validate_changes(self, changes: Dict[str, str], *,
                         approved: bool = False,
                         capability: str = "coding") -> ValidationReport:
        """Structural + policy preview through the ChangeSet engine."""
        if not changes:
            return ValidationReport(False, issues=[
                ValidationIssue("", "empty proposal: nothing to validate")])
        items = self._to_code_changes(changes)
        try:
            dry = self.applier.dry_run(
                items, allow_delete=False, approved=approved,
                capability=capability, actor="forge-native-ai")
        except Exception as exc:
            return ValidationReport(False, issues=[
                ValidationIssue("", "dry-run failed: %s" % exc)])
        issues = []
        for error in getattr(dry, "errors", []) or []:
            if isinstance(error, dict):
                issues.append(ValidationIssue(
                    str(error.get("path", "")),
                    str(error.get("message") or error.get("code") or error)))
            else:
                issues.append(ValidationIssue("", str(error)))
        decisions = [dict(d) if isinstance(d, dict)
                     else d.to_dict() for d in
                     getattr(dry, "decisions", []) or []]
        fingerprint = ""
        try:
            fingerprint = self.applier.fingerprint(items)
        except Exception:
            fingerprint = ""
        ok = bool(getattr(dry, "valid", not issues))
        return ValidationReport(
            ok=ok,
            would_change=list(getattr(dry, "would_change", []) or []),
            issues=issues, decisions=decisions, fingerprint=fingerprint)

    # -- application (authorized writes only) ----------------------------------------

    def apply_changes(self, changes: Dict[str, str], *,
                      approved: bool = False, task_id: str = "",
                      approval_token_id: str = "",
                      capability: str = "coding",
                      label: str = "native-ai") -> ApplyOutcome:
        if not changes:
            return ApplyOutcome(False, errors=["no changes to apply"],
                                label=label)
        items = self._to_code_changes(changes)
        result = self.applier.apply(
            items, approved=approved, label=label, allow_delete=False,
            capability=capability, actor="forge-native-ai",
            task_id=task_id, approval_token_id=approval_token_id)
        decisions = [dict(d) if isinstance(d, dict)
                     else d.to_dict() for d in
                     getattr(result, "decisions", []) or []]
        errors = [str(e) for e in getattr(result, "errors", []) or []]
        ok = bool(getattr(result, "success", True) and not errors)
        blocked = (not ok) and (
            any(str(d.get("decision", "")) in
                ("REQUIRE_APPROVAL", "DENY") for d in decisions)
            or any(("approval" in e.lower() or "not permitted" in e.lower())
                   for e in errors))
        files = list(getattr(result, "changed_paths", []) or [])
        return ApplyOutcome(
            ok=ok,
            files=files, errors=errors, decisions=decisions,
            checkpoint_id=self.checkpoint_id, label=label,
            blocked_by_approval=blocked)

    # -- tests ---------------------------------------------------------------------------

    def test_command(self, test_paths: Optional[Sequence[str]] = None
                     ) -> Tuple[List[str], List[str]]:
        """Build the constrained pytest command + the accepted path list."""
        from forge.agents.debugger import TestDebugLoop
        scoped = TestDebugLoop._sanitize_test_paths(
            list(test_paths or []) or None)
        command = [sys.executable, "-B", "-m", "pytest", "-q",
                   "-p", "no:cacheprovider"] + scoped
        return command, scoped

    def run_tests(self, test_paths: Optional[Sequence[str]] = None,
                  task_id: str = "") -> TestRun:
        command, scoped = self.test_command(test_paths)
        started = perf_counter()
        tool = "run_tests" if "run_tests" in getattr(self.runtime, "tools",
                                                     {}) else "terminal"
        result = self.runtime.execute(tool, approved=False, command=command,
                                       actor="forge-native-ai",
                                       task_id=task_id)
        combined = ((result.output or "")
                     + str(result.metadata.get("stdout", ""))
                     + str(result.metadata.get("stderr", "")))
        exit_code = result.metadata.get("returncode")
        no_tests = (exit_code == 5 and (
            "no tests ran" in combined.lower()
            or "collected 0 items" in combined.lower()))
        duration = perf_counter() - started
        if exit_code is None and not result.success:
            return TestRun(command=command, exit_code=None, passed=False,
                           output=combined,
                           duration_seconds=duration,
                           error=result.error or "test execution failed")
        passed = bool(exit_code == 0) or no_tests
        return TestRun(command=command, exit_code=exit_code,
                       passed=passed,
                       no_tests=no_tests, output=combined,
                       duration_seconds=duration,
                       error="" if result.success else (
                           result.error or ""))

    # -- repair plan + bounded debug cycles live in forge.native.debugging;
    # this engine supplies the primitives (propose/validate/apply/tests) they
    # reuse, so exactly one component ever writes to the repository.
