"""One attempt authority: lease + fence + control, one answer.

Session 11.5 (§8). Forge already had three things that could each say "this
attempt may not continue":

* the durable queue **lease** — does this process still own the task?
* the **FenceRegistry** generation — is this attempt still the authoritative
  one, and is it still in a state that may commit?
* the **SupervisorControl** cancel flag — did an operator ask it to stop?

They were consulted separately, in different places, with different vocabulary,
and one of them (a helper named ``_fenced`` that only looked at the lease) gated
result publication. This module does not add a fourth rule set: it *composes*
the three existing authorities behind one object with one question —
:meth:`ExecutionAuthority.publish_authorized` — and one fail-closed answer.

Everything that can produce a state-changing side effect consults it: tool
writes, git commits, checkpoint release, result persistence, stream publication
and attempt-bound inference. Uncertainty is denial: a missing fence, a registry
that raises, or a lease that cannot be read all refuse, because "we could not
prove this attempt may act" is not "it may act".
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional

__all__ = [
    "ExecutionAuthority",
    "authority_for",
    "DENY_NO_FENCE_AUTHORITY",
    "DENY_FENCE_ERROR",
    "DENY_STALE_ATTEMPT",
    "DENY_SUPERSEDED_ATTEMPT",
    "DENY_CANCELLED_ATTEMPT",
    "DENY_LEASE_LOST",
    "FENCE_ABSENT",
]

#: Denial vocabulary. These are the same codes the server's inference surface
#: raises as typed errors (§9), so one vocabulary spans "may not publish" and
#: "may not generate" — an operator reading either sees the same words.
DENY_NO_FENCE_AUTHORITY = "NO_FENCE_AUTHORITY"
DENY_FENCE_ERROR = "FENCE_ERROR"
DENY_STALE_ATTEMPT = "STALE_ATTEMPT"
DENY_SUPERSEDED_ATTEMPT = "SUPERSEDED_ATTEMPT"
DENY_CANCELLED_ATTEMPT = "CANCELLED_ATTEMPT"
DENY_LEASE_LOST = "LEASE_LOST"

#: Reported as the fence state when no attempt fence could be established.
FENCE_ABSENT = "ABSENT"

#: Fence states that mean "cancellation is in effect".
_CANCELLING_STATES = ("CANCELLING", "CANCELLED")


class ExecutionAuthority:
    """Whether one attempt of one task may still mutate state or publish.

    The object holds no state of its own beyond identity: every answer is read
    live from the authorities it composes, so a decision taken a millisecond ago
    cannot be reused after a cancellation, a supersession or a lease loss.

    Parameters mirror what a worker already has. All of them are optional so a
    partial deployment (no registry, no queue) degrades to the authorities that
    exist — and says so in :meth:`snapshot` rather than pretending to have
    checked something it could not.
    """

    def __init__(self, *, task_id: str, project_id: str = "",
                 lease_owner: str = "", boot_id: str = "",
                 fence: Any = None, registry: Any = None, queue: Any = None,
                 control: Any = None, require_fence: bool = True) -> None:
        self.task_id = str(task_id or "")
        self.project_id = str(project_id or "")
        self.lease_owner = str(lease_owner or "")
        self.boot_id = str(boot_id or "")
        self._fence = fence
        self._registry = registry
        self._queue = queue
        self._control = control
        #: With a registry deployed, an attempt that has no fence cannot prove
        #: it is authoritative, so it may not publish (§9). Callers that run
        #: without the fence system at all (legacy harnesses) opt out
        #: explicitly, and the opt-out is recorded in the snapshot.
        self._require_fence = bool(require_fence)

    # -- identity (minted once upstream, never reconstructed here) -----------

    @property
    def attempt_id(self) -> str:
        fence_id = str(getattr(self._fence, "attempt_id", "") or "")
        if fence_id:
            return fence_id
        return "%s#g%s" % (self.task_id, self.generation) if self.task_id else ""

    @property
    def generation(self) -> int:
        generation = getattr(self._fence, "generation", None)
        if generation is None and self._registry is not None:
            try:
                generation = self._registry.generation_of(self.task_id)
            except Exception:                          # noqa: BLE001
                generation = None
        try:
            return int(generation or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def fence_state(self) -> str:
        """The registry's view of this attempt's fence (``ABSENT`` if none)."""
        state = self._live_fence_state()
        return state if state else FENCE_ABSENT

    @property
    def cancel_requested(self) -> bool:
        """True when an operator asked this attempt to stop."""
        control = self._control
        if control is None:
            return False
        try:
            return bool(control.cancel_requested)
        except Exception:                              # noqa: BLE001
            #: A control we cannot read is a control we cannot trust.
            return True

    @property
    def cancellation_epoch(self) -> int:
        """The attempt generation whose cancellation is in effect (0 = none).

        Derived, not stored: a cancellation belongs to the generation that was
        current when it was requested, and the registry already knows which
        generation that is. Keeping a separate counter would create a second
        source of truth that could disagree with the fence.
        """
        if self.fence_state in _CANCELLING_STATES or self.cancel_requested:
            return self.generation
        return 0

    # -- the one question ----------------------------------------------------

    def denial_reason(self) -> str:
        """``""`` while this attempt may act; otherwise the reason it may not.

        Checks run cheapest-and-most-decisive first, and the first denial wins:
        an attempt that lost its lease is reported as ``LEASE_LOST`` even if its
        fence was also superseded, because the lease is what makes this process
        the owner at all.
        """
        queue = self._queue
        if queue is not None:
            try:
                if not queue.lease_held_by(self.task_id, self.lease_owner):
                    return DENY_LEASE_LOST
            except Exception:                          # noqa: BLE001
                #: An unreadable lease is not a held lease (§9: uncertainty is
                #: denial, never permission).
                return DENY_LEASE_LOST

        registry = self._registry
        if registry is None:
            #: No fence authority is deployed here; the lease is the only
            #: authority that exists. The snapshot says so explicitly.
            return DENY_CANCELLED_ATTEMPT if self.cancel_requested else ""

        fence = self._fence
        if fence is None:
            if self._require_fence:
                return DENY_NO_FENCE_AUTHORITY
            return DENY_CANCELLED_ATTEMPT if self.cancel_requested else ""

        try:
            current = registry.current(self.task_id)
        except Exception:                              # noqa: BLE001
            return DENY_FENCE_ERROR
        if current is not None and int(getattr(current, "generation", 0)
                                       ) != int(getattr(fence, "generation", 0)):
            return DENY_SUPERSEDED_ATTEMPT

        state = self._live_fence_state()
        if state in _CANCELLING_STATES or self.cancel_requested:
            return DENY_CANCELLED_ATTEMPT
        if not state:
            return DENY_FENCE_ERROR
        try:
            authorized = bool(registry.is_authorized(fence))
        except Exception:                              # noqa: BLE001
            return DENY_FENCE_ERROR
        if not authorized:
            #: Terminal, fenced or timed out: all "stale" for publication
            #: purposes. The precise state is in :meth:`snapshot`.
            return DENY_STALE_ATTEMPT
        return ""

    def publish_authorized(self) -> bool:
        """May this attempt persist a result / publish its output? (§8/§16)"""
        return self.denial_reason() == ""

    def mutation_authorized(self) -> bool:
        """May this attempt write files, commit, or release a checkpoint?

        The same answer as :meth:`publish_authorized`, named for the write
        choke points so a reader does not have to guess whether "publish" was
        meant to exclude mutation. One authority, one rule: a stale attempt
        neither writes nor publishes.
        """
        return self.publish_authorized()

    def commit_guard(self) -> Callable[[], str]:
        """The guard handed to write choke points (ToolRuntime, ChangeApplier).

        Returns ``""`` while the attempt may act and a short reason once it may
        not — the contract the existing fencing helper already established,
        widened to include the lease and the cancellation flag so a tool cannot
        write through a gap between the three authorities.
        """
        base: Optional[Callable[[], str]] = None
        if self._fence is not None and self._registry is not None:
            try:
                from forge.core.fencing import commit_guard as _fence_guard

                base = _fence_guard(self._fence, self._registry)
            except Exception:                          # noqa: BLE001
                base = None

        def _guard() -> str:
            reason = self.denial_reason()
            if reason:
                return "attempt %s may not commit: %s" % (
                    self.attempt_id or "-", reason)
            if base is not None:
                try:
                    return str(base() or "")
                except Exception:                      # noqa: BLE001
                    return ("attempt %s may not commit: %s"
                            % (self.attempt_id or "-", DENY_FENCE_ERROR))
            return ""

        return _guard

    # -- unified cancellation (§11) ------------------------------------------

    def request_cancel(self, reason: str = "") -> bool:
        """Ask this attempt to stop, across every layer that can hear it.

        Signals the running control (so the Supervisor stops at its next stage
        boundary) and moves the fence to CANCELLING (so publish authority is
        revoked *now*, even if the worker has not noticed yet). The worker still
        owns the confirmation to CANCELLED; nothing here claims a stop that has
        not happened, and the return value says only whether a layer was
        reached.
        """
        reached = False
        control = self._control
        if control is not None:
            try:
                control.request_cancel()
                reached = True
            except Exception:                          # noqa: BLE001
                pass
        registry = self._registry
        if registry is not None:
            try:
                #: Idempotent by construction: an already-CANCELLING attempt is
                #: returned unchanged rather than raising a state conflict.
                registry.cancel(self.task_id)
                reached = True
            except Exception:                          # noqa: BLE001
                pass
        if reached and reason:
            #: Bounded and content-free: an operator's reason, not a payload.
            try:
                self._reason = str(reason)[:200]
            except Exception:                          # noqa: BLE001
                pass
        return reached

    # -- observability -------------------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        """A bounded, payload-free view of this authority (§33/observability).

        Identifiers, states and the denial reason only: never a prompt, a
        result, a file path or a credential. Reading it changes nothing.
        """
        reason = self.denial_reason()
        return {
            "task_id": self.task_id[:128],
            "attempt_id": self.attempt_id[:128],
            "generation": self.generation,
            "lease_owner": self.lease_owner[:128],
            "boot_id": self.boot_id[:64],
            "fence_state": self.fence_state,
            "cancel_requested": self.cancel_requested,
            "cancellation_epoch": self.cancellation_epoch,
            "lease_checked": self._queue is not None,
            "fence_authority": ("registry" if self._registry is not None
                                else "absent"),
            "require_fence": bool(self._require_fence),
            "publish_authorized": reason == "",
            "denial_reason": reason,
        }

    def __repr__(self) -> str:                          # pragma: no cover
        return "<ExecutionAuthority %s state=%s authorized=%s>" % (
            self.attempt_id or "-", self.fence_state,
            self.denial_reason() == "")

    # -- internals -----------------------------------------------------------

    def _live_fence_state(self) -> str:
        """The registry's current state for this attempt ("" when unknown).

        The registry wins over the object this attempt holds: fences are frozen
        records, and a superseded worker's copy still says ``RUNNING``.
        """
        registry = self._registry
        fence = self._fence
        if registry is None or fence is None:
            return str(getattr(fence, "state", "") or "")
        try:
            current = registry.get(self.task_id,
                                   int(getattr(fence, "generation", 0)))
        except Exception:                              # noqa: BLE001
            return ""
        if current is None:
            return ""
        return str(getattr(current, "state", "") or "")


def authority_for(server: Any, task_id: str, *, fence: Any = None,
                  lease_owner: str = "", control: Any = None,
                  project_id: str = "", require_fence: bool = True,
                  boot_id: str = "") -> ExecutionAuthority:
    """Build the authority for one attempt from a server-like object.

    Pulls the *existing* authorities off the server — ``queue`` (leases),
    ``fences`` (the registry), ``boot_id`` — and looks the control up in the
    server's control table when the caller does not pass one, so a caller cannot
    accidentally compose an authority that ignores a cancellation.
    """
    task_id = str(task_id or "")
    if not project_id:
        try:
            record = server.tasks.get(task_id)
            project_id = str(getattr(record, "project_id", "") or "")
        except Exception:                              # noqa: BLE001
            project_id = ""
    if control is None:
        try:
            control = server.control_for(task_id)
        except Exception:                              # noqa: BLE001
            control = None
    registry = getattr(server, "fences", None)
    if fence is None and registry is not None:
        try:
            fence = registry.current(task_id)
        except Exception:                              # noqa: BLE001
            fence = None
    return ExecutionAuthority(
        task_id=task_id, project_id=project_id,
        lease_owner=str(lease_owner or ""),
        boot_id=str(boot_id or getattr(server, "boot_id", "") or ""),
        fence=fence, registry=registry, queue=getattr(server, "queue", None),
        control=control,
        #: No registry deployed at all means nothing to require a fence from.
        require_fence=bool(require_fence and registry is not None))
