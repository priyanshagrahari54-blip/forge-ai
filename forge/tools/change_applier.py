"""Controlled code-change application layer (A32.1 / A32.4).

The single place where model-produced changes become repository writes. It
validates and authorizes the ENTIRE change set before modifying anything,
enforces permissions through the ``ToolRuntime``, records every changed path,
and (when a ``CheckpointManager`` is provided) checkpoints after full
validation and authorization but before the first modification, so a change
set can be rolled back exactly.

Model output never bypasses this layer: the coder and debugger write through
it, and the supervisor rolls back through the same recorded path set.

Hardening (A32 rebuild):

- ``CodeChange`` may carry ``expected_old_hash`` / ``expected_old_content`` so
  a change applies only against the exact file state it was generated for.
- :meth:`ChangeApplier.dry_run` validates a proposal without writing anything.
- :meth:`ChangeApplier.fingerprint` identifies a proposal deterministically.
- ``delete`` is structurally supported but denied by default: it requires
  ``allow_delete=True`` *and* an explicit approval, and the runtime must
  provide a permissioned ``delete_file`` tool. Model-proposed deletes stay
  rejected upstream (the coder schema refuses them); deletes are an explicit
  operator path, never an implicit model side effect.
- Failures are recorded as structured :class:`ChangeError` values (``code``,
  ``path``, ``message``) in addition to the legacy string list.
- Every change passes the explicit :class:`PolicyGate
  <forge.security.policy_gate.PolicyGate>` (ALLOW / DENY / REQUIRE_APPROVAL)
  *before* modification. The gate sees the operation, path, tool, risk, and
  requested capability; denials are recorded and never bypassed.
"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable
from uuid import uuid4

from forge.core.run_control import TaskCancelled
from forge.runtime.runtime import ToolResult, ToolRuntime
from forge.security.policy_gate import PolicyDecision, PolicyGate

#: Hard bound on a single generated file, so a runaway model cannot produce an
#: unbounded write.
MAX_FILE_BYTES = 2 * 1024 * 1024

_SECRET_PATTERNS = (
    re.compile(r"(?:api[_-]?key|secret|password|token)\s*[:=]\s*['\"][^'\"]{8,}", re.I),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)

_CREDENTIAL_FILENAME_TOKENS = ("credential", "secret", "private_key", "token")


@dataclass(frozen=True)
class CodeChange:
    """One validated, model-proposed repository change."""

    path: str
    content: str
    action: str = "modify"  # create | modify | delete (delete is gated)
    #: Optional guard: sha256 hex of the file bytes the change was built for.
    expected_old_hash: str | None = None
    #: Optional guard: exact text the file must currently hold.
    expected_old_content: str | None = None
    #: Caller-assessed risk (NONE/LOW/MEDIUM/HIGH/CRITICAL) for policy.
    risk: str = "NONE"


@dataclass(frozen=True)
class ChangeError:
    """Structured change failure: machine-readable code plus context."""

    code: str
    path: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "message": self.message}


class _ValidationError(ValueError):
    """Internal: a ``ValueError`` carrying a structured error code and path."""

    def __init__(self, code: str, path: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.path = path


@dataclass(frozen=True)
class ApprovalItem:
    """One change awaiting an explicit operator decision."""

    operation: str
    path: str
    tool: str
    risk: str
    reason: str


@dataclass(frozen=True)
class ApprovalQuery:
    """Everything an operator needs to decide on a blocked change set.

    The query is informational: answering it mints no authority by itself.
    Authority still comes only from a redeemed A33 approval token (or the
    caller's explicit ``approved`` flag), re-checked by the gate for every
    change before any write happens.
    """

    items: tuple[ApprovalItem, ...]
    agent: str
    task_id: str
    capability: str
    fingerprint: str
    label: str


#: Interactive approval hook: given a query, block until an operator
#: decides and return an approval-token id, or ``""`` when denied,
#: expired, or unavailable. May raise :class:`TaskCancelled` to abort
#: the enclosing run; any other exception fails closed like a denial.
ApprovalCallback = Callable[[ApprovalQuery], str]


@dataclass
class ApplyResult:
    """Outcome of applying a change set through the controlled layer."""

    changed_paths: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    checkpoint_id: str | None = None
    checkpoint: Any = None  # internal: the live Checkpoint for rollback
    error_details: list[ChangeError] = field(default_factory=list)
    fingerprint: str | None = None
    #: Policy decision per evaluated change (see ``PolicyOutcome.to_dict``).
    decisions: list[dict[str, Any]] = field(default_factory=list)
    #: Live task grant authorizing this apply, if task-scoped (A33).
    task_grant: dict[str, Any] | None = None
    #: Every declared path, recorded before validation (transaction audit).
    proposed_paths: list[str] = field(default_factory=list)
    #: True when this apply rolled its own checkpoint back after a failure.
    rolled_back: bool = False
    #: Wall-clock time spent inside :meth:`ChangeApplier.apply`, in ms.
    duration_ms: float | None = None

    @property
    def success(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "changed_paths": list(self.changed_paths),
            "errors": list(self.errors),
            "checkpoint_id": self.checkpoint_id,
            "fingerprint": self.fingerprint,
            "error_details": [detail.to_dict() for detail in self.error_details],
            "decisions": [dict(decision) for decision in self.decisions],
            "task_grant": dict(self.task_grant) if self.task_grant else None,
            "proposed_paths": list(self.proposed_paths),
            "rolled_back": self.rolled_back,
            "duration_ms": self.duration_ms,
        }


@dataclass
class DryRunResult:
    """Outcome of validating a proposal without writing anything."""

    would_change: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    error_details: list[ChangeError] = field(default_factory=list)
    fingerprint: str | None = None
    #: Policy preview per structurally valid change (non-filtering).
    decisions: list[dict[str, Any]] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "would_change": list(self.would_change),
            "errors": list(self.errors),
            "fingerprint": self.fingerprint,
            "error_details": [detail.to_dict() for detail in self.error_details],
            "decisions": [dict(decision) for decision in self.decisions],
        }


def _normalize(changes: Iterable[CodeChange | dict[str, Any]]) -> list[CodeChange]:
    normalized: list[CodeChange] = []
    for change in changes:
        if isinstance(change, CodeChange):
            normalized.append(change)
            continue
        action = change.get("action", "modify")
        if action == "delete":
            content = change.get("content", "")
        else:
            content = change["content"]
        normalized.append(
            CodeChange(
                path=change["path"],
                content=content,
                action=action,
                expected_old_hash=change.get("expected_old_hash"),
                expected_old_content=change.get("expected_old_content"),
                risk=change.get("risk", "NONE"),
            )
        )
    return normalized


class ChangeApplier:
    """Validate, checkpoint, and apply model changes through the runtime."""

    def __init__(self, runtime: ToolRuntime, checkpoint_manager=None,
                 root: str | Path | None = None,
                 policy_gate: PolicyGate | None = None,
                 approval_store=None,
                 approval_callback: ApprovalCallback | None = None) -> None:
        self.runtime = runtime
        self.checkpoint_manager = checkpoint_manager
        self.root = Path(root).resolve() if root is not None else None
        self.policy_gate = policy_gate
        #: Optional A33 approval store: mints task-scoped grants and redeems
        #: approval tokens. ``None`` preserves exact A32 behavior.
        self.approval_store = approval_store
        #: Optional interactive approval hook (A34). Consulted only when the
        #: policy pre-flight yields REQUIRE_APPROVAL and nothing worse;
        #: DENY is never escalated to an operator. ``None`` preserves exact
        #: A32 behavior (approval-required change sets fail immediately).
        self.approval_callback = approval_callback

    def _gate(self) -> PolicyGate:
        """Policy gate sharing the runtime's permission posture by default."""
        if self.policy_gate is not None:
            return self.policy_gate
        return PolicyGate(getattr(self.runtime, "permission_manager", None))

    @staticmethod
    def _operation_for(change: CodeChange) -> tuple[str, str]:
        if change.action == "delete":
            return "delete_file", "delete_file"
        return "write_file", "write_file"

    # -- validation -------------------------------------------------------

    def validate(self, change: CodeChange, *, allow_delete: bool = False) -> None:
        """Raise ``ValueError`` for any unsafe or malformed change.

        Independent of the coder's own structural validation so the write layer
        is safe even if a future caller forgets an upstream check.
        """
        self._validate_path(change.path)
        if change.action == "delete":
            if not allow_delete:
                raise _ValidationError(
                    "DELETE_REQUIRES_APPROVAL", change.path,
                    f"Delete of {change.path!r} requires explicit operator approval",
                )
            if change.content != "":
                raise _ValidationError(
                    "DELETE_CONTENT_NOT_EMPTY", change.path,
                    f"Delete of {change.path!r} must not carry new content",
                )
        elif change.action not in ("create", "modify"):
            raise _ValidationError(
                "UNSUPPORTED_ACTION", change.path,
                f"Unsupported change action for {change.path!r}: {change.action!r}",
            )
        else:
            self._validate_content(change.path, change.content)
        self._validate_old_state(change)

    @staticmethod
    def _validate_path(path: str) -> None:
        candidate = PurePosixPath(path)
        if not path or candidate.is_absolute() or re.match(r"^[A-Za-z]:[\\/]", path):
            raise _ValidationError(
                "UNSAFE_PATH", path, f"Change path must be relative: {path!r}")
        if ".." in candidate.parts or ".git" in candidate.parts or ".forge" in candidate.parts:
            raise _ValidationError(
                "UNSAFE_PATH", path,
                f"Change path is outside the permitted source tree: {path!r}")
        if "\\" in path:
            raise _ValidationError(
                "UNSAFE_PATH", path,
                f"Change path must use repository-relative POSIX separators: {path!r}")

    @classmethod
    def _validate_content(cls, path: str, content: str) -> None:
        name = PurePosixPath(path).name.lower()
        if name == ".env" or name.endswith(".env"):
            raise _ValidationError(
                "ENV_FILE", path,
                f"Environment files cannot be written autonomously: {path!r}")
        if any(token in name for token in _CREDENTIAL_FILENAME_TOKENS):
            raise _ValidationError(
                "CREDENTIAL_FILE", path,
                f"Credential-like files cannot be written autonomously: {path!r}")
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_FILE_BYTES:
            raise _ValidationError(
                "OVERSIZED", path, f"Change exceeds {MAX_FILE_BYTES} bytes: {path}")
        if any(pattern.search(content) for pattern in _SECRET_PATTERNS):
            raise _ValidationError(
                "SECRET_CONTENT", path, f"Change appears to contain a secret: {path}")
        if path.endswith(".py"):
            try:
                compile(content, path, "exec")
            except SyntaxError as exc:
                raise _ValidationError(
                    "INVALID_PYTHON", path,
                    f"Change produced invalid Python for {path}: {exc}") from exc

    def _validate_old_state(self, change: CodeChange) -> None:
        """Enforce ``expected_old_*`` guards against current file bytes."""
        if change.expected_old_hash is None and change.expected_old_content is None:
            return
        if self.root is None:
            raise _ValidationError(
                "OLD_STATE_UNVERIFIABLE", change.path,
                f"Cannot verify expected previous state for {change.path!r} "
                "without a repository root")
        target = (self.root / change.path).resolve()
        try:
            target.relative_to(self.root)
        except ValueError:
            raise _ValidationError(
                "UNSAFE_PATH", change.path,
                f"Change path is outside the permitted source tree: {change.path!r}")
        if not target.is_file():
            raise _ValidationError(
                "OLD_STATE_MISMATCH", change.path,
                f"Expected previous content for {change.path!r} "
                "but the file does not exist")
        current = target.read_bytes()
        if change.expected_old_hash is not None:
            actual = hashlib.sha256(current).hexdigest()
            if actual.lower() != change.expected_old_hash.lower():
                raise _ValidationError(
                    "OLD_STATE_MISMATCH", change.path,
                    f"Previous content of {change.path!r} does not match "
                    "the expected hash")
        if change.expected_old_content is not None:
            try:
                text = current.decode("utf-8")
            except UnicodeDecodeError:
                raise _ValidationError(
                    "OLD_STATE_MISMATCH", change.path,
                    f"Previous content of {change.path!r} does not match "
                    "the expected text")
            if text != change.expected_old_content:
                raise _ValidationError(
                    "OLD_STATE_MISMATCH", change.path,
                    f"Previous content of {change.path!r} does not match "
                    "the expected text")

    # -- fingerprint + dry-run --------------------------------------------

    @staticmethod
    def fingerprint(changes: Iterable[CodeChange | dict[str, Any]]) -> str:
        """Return a deterministic identity for a change proposal.

        Entries are normalized to ``action:path:content-sha`` (plus any old
        state guard) and hashed in sorted order, so the fingerprint is stable
        regardless of proposal order but sensitive to every path, action,
        payload, and guard byte.
        """
        entries: list[str] = []
        for change in _normalize(changes):
            content_sha = hashlib.sha256(change.content.encode("utf-8")).hexdigest()
            guard = ""
            if change.expected_old_hash is not None:
                guard = f":old-hash:{change.expected_old_hash.lower()}"
            elif change.expected_old_content is not None:
                old_sha = hashlib.sha256(
                    change.expected_old_content.encode("utf-8")).hexdigest()
                guard = f":old-content:{old_sha}"
            entries.append(f"{change.action}:{change.path}:{content_sha}{guard}")
        return hashlib.sha256("\n".join(sorted(entries)).encode("utf-8")).hexdigest()

    def dry_run(self, changes: Iterable[CodeChange | dict[str, Any]],
                *, allow_delete: bool = False, approved: bool = False,
                capability: str = "", actor: str = "", task_id: str = "",
                approval_token_id: str = "") -> DryRunResult:
        """Validate a proposal without writing, checkpointing, or staging.

        Returns the paths that *would* change plus structured errors for every
        rejected entry. Only declared entries are ever considered: there is no
        path by which an undeclared file can be touched. ``decisions`` previews
        the policy verdict for each structurally valid change without
        filtering ``would_change`` (approval is evaluated for real in
        :meth:`apply`). A dry run never mints authority: no task grant is
        created here.
        """
        normalized = _normalize(changes)
        result = DryRunResult(fingerprint=self.fingerprint(normalized))
        gate = self._gate()
        for change in normalized:
            try:
                self.validate(change, allow_delete=allow_delete)
            except ValueError as exc:
                self._record(result.errors, result.error_details, exc)
                continue
            operation, tool = self._operation_for(change)
            result.decisions.append(gate.evaluate(
                operation=operation, path=change.path, tool=tool,
                risk=change.risk, capability=capability,
                approved=approved, agent=actor, task_id=task_id,
                approval_token_id=approval_token_id,
                fingerprint=result.fingerprint or "",
                preview=True).to_dict())
            if change.path not in result.would_change:
                result.would_change.append(change.path)
        return result

    @staticmethod
    def _record(errors: list[str], details: list[ChangeError], exc: ValueError) -> None:
        errors.append(str(exc))
        if isinstance(exc, _ValidationError):
            details.append(ChangeError(exc.code, exc.path, str(exc)))
        else:
            details.append(ChangeError("VALIDATION_FAILED", "", str(exc)))

    # -- application ------------------------------------------------------

    def _request_interactive_approval(self, plan: list[CodeChange],
                                      previews: list[Any], *,
                                      approved: bool, capability: str,
                                      actor: str, task_id: str,
                                      fingerprint: str, label: str) -> str:
        """Ask the operator for one token covering the blocked changes.

        Returns the granted token id, or ``""`` when denied, expired, or
        unavailable. Only REQUIRE_APPROVAL outcomes are queried — callers
        guarantee no DENY is present. :class:`TaskCancelled` propagates so
        a cancelled run aborts instead of failing closed as a denial.
        """
        assert self.approval_callback is not None
        items: list[ApprovalItem] = []
        for change, outcome in zip(plan, previews):
            if outcome.allowed:
                continue
            operation, tool = self._operation_for(change)
            items.append(ApprovalItem(
                operation=operation, path=change.path, tool=tool,
                risk=change.risk, reason=outcome.reason))
        if not items:
            return ""
        query = ApprovalQuery(
            items=tuple(items), agent=actor, task_id=task_id,
            capability=capability, fingerprint=fingerprint, label=label)
        try:
            token_id = self.approval_callback(query)
        except TaskCancelled:
            raise
        except Exception:
            return ""
        return token_id if isinstance(token_id, str) else ""

    @staticmethod
    def _detect_conflicts(normalized: list[CodeChange],
                          result: ApplyResult) -> list[CodeChange]:
        """Reject ambiguous repeats of one path; dedupe exact repeats.

        Two entries for the same path that differ in any way (action,
        content, or guards) make the change set ambiguous, so the whole
        transaction is rejected. Byte-identical repeats are harmless and are
        collapsed to a single planned change.
        """
        plan: list[CodeChange] = []
        seen: dict[str, CodeChange] = {}
        for change in normalized:
            previous = seen.get(change.path)
            if previous is None:
                seen[change.path] = change
                plan.append(change)
                continue
            if previous == change:
                continue
            message = (f"Conflicting changes for {change.path!r}: the same "
                       f"path appears with different actions or content")
            result.errors.append(message)
            result.error_details.append(
                ChangeError("CONFLICTING_CHANGES", change.path, message))
        return plan

    def apply(self, changes: Iterable[CodeChange | dict[str, Any]], approved: bool,
              label: str = "change", *, allow_delete: bool = False,
              capability: str = "", actor: str = "", task_id: str = "",
              approval_token_id: str = "",
              commit_guard: Callable[[], str] | None = None) -> ApplyResult:
        """Validate, authorize, checkpoint, and apply a change set atomically.

        The transaction boundary is strict: the ENTIRE change set is
        normalized, structurally validated, and policy-authorized BEFORE the
        checkpoint is created and before any candidate file is modified. A
        checkpoint is created only for a fully validated and fully authorized
        plan, when a ``CheckpointManager`` is configured, so the pre-change
        state can be restored exactly. Paths are recorded in application
        order; unrelated files are never touched. Only declared entries are
        written: anything not in ``changes`` cannot be modified through this
        call.

        If any write (or its execution-time re-check) fails mid-application,
        the transaction stops immediately and the checkpoint is rolled back,
        restoring exactly the files this candidate touched.

        When ``task_id`` and an approval store are configured, a temporary
        task grant is minted over exactly the validated paths (bound to the
        proposal fingerprint) and each write is checked against it; the grant
        is revoked if the apply fails.
        """
        started = time.perf_counter()
        result = ApplyResult()
        try:
            normalized = _normalize(changes)
        except (KeyError, TypeError, AttributeError) as exc:
            message = f"Malformed change set entry: {exc}"
            result.errors.append(message)
            result.error_details.append(
                ChangeError("MALFORMED_CHANGESET", "", message))
            result.duration_ms = (time.perf_counter() - started) * 1000
            return result
        result.fingerprint = self.fingerprint(normalized)
        for change in normalized:
            if change.path not in result.proposed_paths:
                result.proposed_paths.append(change.path)
        gate = self._gate()

        # Phase 1 — structural pre-flight: validate EVERY change. Any
        # failure rejects the whole transaction before any policy state,
        # checkpoint, grant, or write exists.
        for change in normalized:
            try:
                self.validate(change, allow_delete=allow_delete)
            except ValueError as exc:
                self._record(result.errors, result.error_details, exc)
        plan = self._detect_conflicts(normalized, result)
        if result.errors:
            result.duration_ms = (time.perf_counter() - started) * 1000
            return result

        # Phase 2 — policy pre-flight: authorize EVERY change through the
        # existing gate in preview mode (no token consumption, no writes).
        # One enforcement chain per change: the preview, the real
        # evaluation, and the runtime layer share this id.
        enforcement_ids = [uuid4().hex for _ in plan]
        previews: list[Any] = []
        for change, enforcement_id in zip(plan, enforcement_ids):
            operation, tool = self._operation_for(change)
            previews.append(gate.evaluate(
                operation=operation, path=change.path, tool=tool,
                risk=change.risk, capability=capability, approved=approved,
                agent=actor, task_id=task_id,
                approval_token_id=approval_token_id,
                fingerprint=result.fingerprint or "",
                request_id=enforcement_id, preview=True))
        # Phase 2b — interactive approval (A34, opt-in): when every block
        # is REQUIRE_APPROVAL (never DENY) and a callback is configured,
        # ask the operator once for the whole change set. A granted token
        # is re-checked by the gate for every change below; a denial (or
        # callback failure) falls through to the normal failure path.
        active_token = approval_token_id
        if (self.approval_callback is not None
                and any(not outcome.allowed for outcome in previews)
                and not any(outcome.decision == PolicyDecision.DENY
                            for outcome in previews)):
            active_token = self._request_interactive_approval(
                plan, previews, approved=approved, capability=capability,
                actor=actor, task_id=task_id,
                fingerprint=result.fingerprint or "", label=label)
            if active_token:
                previews = []
                for change, enforcement_id in zip(plan, enforcement_ids):
                    operation, tool = self._operation_for(change)
                    previews.append(gate.evaluate(
                        operation=operation, path=change.path, tool=tool,
                        risk=change.risk, capability=capability,
                        approved=approved, agent=actor, task_id=task_id,
                        approval_token_id=active_token,
                        fingerprint=result.fingerprint or "",
                        request_id=enforcement_id, preview=True))
        if any(not outcome.allowed for outcome in previews):
            result.decisions.extend(
                outcome.to_dict() for outcome in previews)
            for change, outcome in zip(plan, previews):
                if outcome.allowed:
                    continue
                code = ("POLICY_DENIED"
                        if outcome.decision == PolicyDecision.DENY
                        else "APPROVAL_REQUIRED")
                message = (f"Change to {change.path} not permitted: "
                           f"{outcome.reason}")
                result.errors.append(message)
                result.error_details.append(
                    ChangeError(code, change.path, message))
            result.duration_ms = (time.perf_counter() - started) * 1000
            return result

        # Phase 2c — execution fence (Session 10): if the attempt that
        # owns this change set has been fenced (timed out, cancelled,
        # superseded by a retry, lost across a restart), refuse before
        # any checkpoint or write exists. The transaction reports the
        # fence instead of succeeding.
        if commit_guard is not None:
            try:
                fenced_reason = commit_guard()
            except Exception as exc:
                fenced_reason = f"commit guard errored: {exc}"
            if fenced_reason:
                message = f"Change set fenced by execution attempt: {fenced_reason}"
                result.errors.append(message)
                result.error_details.append(
                    ChangeError("FENCED_ATTEMPT", "", message))
                result.duration_ms = (time.perf_counter() - started) * 1000
                return result

        # Phase 3 — checkpoint the fully validated + authorized plan.
        if self.checkpoint_manager is not None:
            checkpoint = self.checkpoint_manager.create(label)
            result.checkpoint_id = checkpoint.id
            result.checkpoint = checkpoint

        # Phase 4 — mint the task grant over exactly the planned paths.
        task_scoped = bool(task_id) and self.approval_store is not None
        if task_scoped:
            grant = self.approval_store.grant_task(
                task_id, [change.path for change in plan],
                result.fingerprint or "")
            result.task_grant = grant.to_dict()

        # Phase 5 — apply the plan, stopping at the first failure.
        for change, enforcement_id in zip(plan, enforcement_ids):
            if task_scoped:
                granted, grant_reason = self.approval_store.check_task_grant(
                    task_id, change.path, result.fingerprint or "")
                if not granted:
                    message = (f"Change to {change.path} outside task scope: "
                               f"{grant_reason}")
                    result.errors.append(message)
                    result.error_details.append(
                        ChangeError("TASK_SCOPE_DENIED", change.path, message))
                    break
            operation, tool = self._operation_for(change)
            outcome = gate.evaluate(
                operation=operation, path=change.path, tool=tool,
                risk=change.risk, capability=capability, approved=approved,
                agent=actor, task_id=task_id,
                approval_token_id=active_token,
                fingerprint=result.fingerprint or "",
                request_id=enforcement_id)
            result.decisions.append(outcome.to_dict())
            if not outcome.allowed:
                # Pre-flight authorized this plan; a flip here fails closed.
                code = ("POLICY_DENIED"
                        if outcome.decision == PolicyDecision.DENY
                        else "APPROVAL_REQUIRED")
                message = (f"Change to {change.path} not permitted: "
                           f"{outcome.reason}")
                result.errors.append(message)
                result.error_details.append(
                    ChangeError(code, change.path, message))
                break
            if commit_guard is not None:
                # Re-check the fence before every write: an attempt can be
                # fenced mid-apply (timeout/cancel landed between writes).
                # The failure path below rolls back exactly what was
                # already written.
                try:
                    fenced_reason = commit_guard()
                except Exception as exc:
                    fenced_reason = f"commit guard errored: {exc}"
                if fenced_reason:
                    message = (f"Change to {change.path} refused: "
                               f"fenced by execution attempt: "
                               f"{fenced_reason}")
                    result.errors.append(message)
                    result.error_details.append(
                        ChangeError("FENCED_ATTEMPT", change.path,
                                    message))
                    break
            try:
                if change.action == "delete":
                    write = self.runtime.execute(
                        "delete_file", approved=approved, path=change.path,
                        actor=actor, task_id=task_id,
                        approval_token_id=active_token, risk=change.risk,
                        fingerprint=result.fingerprint or "",
                        request_id=enforcement_id,
                    )
                else:
                    write = self.runtime.execute(
                        "write_file", approved=approved, path=change.path, content=change.content,
                        actor=actor, task_id=task_id,
                        approval_token_id=active_token, risk=change.risk,
                        fingerprint=result.fingerprint or "",
                        request_id=enforcement_id,
                    )
            except Exception as exc:
                # An unexpected execution failure fails closed: the
                # transaction stops and rolls back like any write failure.
                write = ToolResult.fail(
                    "delete_file" if change.action == "delete" else "write_file",
                    f"{type(exc).__name__}: {exc}")
            if not write.success:
                message = f"Failed to write {change.path}: {write.error}"
                result.errors.append(message)
                result.error_details.append(
                    ChangeError("WRITE_FAILED", change.path, message))
                break
            if change.path not in result.changed_paths:
                result.changed_paths.append(change.path)

        if result.checkpoint is not None and not result.success:
            # Restore exactly the files touched so a failed apply never leaves
            # a half-applied change set behind.
            self.checkpoint_manager.rollback(result.checkpoint, result.changed_paths)
            self.checkpoint_manager.cleanup(result.checkpoint)
            result.checkpoint_id = None
            result.checkpoint = None
            result.rolled_back = True
        if task_scoped and not result.success:
            # A failed apply keeps no authority: revoke the task grant.
            self.approval_store.revoke_task(task_id)
            result.task_grant = None
        result.duration_ms = (time.perf_counter() - started) * 1000
        return result

    def rollback(self, result: ApplyResult) -> None:
        """Restore the checkpoint captured for a successful ``ApplyResult``."""
        if self.checkpoint_manager is None or result.checkpoint is None:
            return
        self.checkpoint_manager.rollback(result.checkpoint, result.changed_paths)
        self.checkpoint_manager.cleanup(result.checkpoint)
        result.checkpoint = None
        result.checkpoint_id = None
        result.changed_paths = []
        if result.task_grant is not None and self.approval_store is not None:
            # Rolled-back work keeps no authority.
            grant_task = result.task_grant.get("task_id", "")
            if grant_task:
                self.approval_store.revoke_task(grant_task)
            result.task_grant = None
        result.task_grant = None
