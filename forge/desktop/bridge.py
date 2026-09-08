"""Authenticated Desktop Bridge (A35).

The bridge is the only place the Forge server touches the desktop
provider. It owns:

- bridge sessions (actor + project + scopes + expiry) — the browser
  never talks to the desktop provider directly and never receives
  arbitrary OS control;
- the single enforcement point: every submitted action runs the
  :class:`DesktopAgent` pipeline (identity -> scope -> A33 policy ->
  risk invariants -> profile -> approval -> execution -> audit).

Deployment: in A35 the bridge runs in-process behind the control plane.
The same surface is designed to move out of process (a small Desktop
Agent service on the user's machine) without changing callers — see
``docs/A35-DESKTOP-AGENT.md``. Provider state is never exposed with
secrets; snapshots carry simulation metadata.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from forge.desktop.actions import DesktopRequest
from forge.desktop.agent import DesktopAgent, DesktopActionResult


class DesktopBridgeError(Exception):
    """A bridge-level refusal (always fail closed)."""


@dataclass
class BridgeSession:
    bridge_id: str
    actor: str
    project_id: str
    scopes: frozenset[str] = frozenset()
    expires_at: float = 0.0

    def expired(self, now: float | None = None) -> bool:
        return (now if now is not None else time.time()) >= self.expires_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "bridge_id": self.bridge_id,
            "actor": self.actor,
            "project_id": self.project_id,
            "scopes": sorted(self.scopes),
            "expires_at": self.expires_at,
        }


class DesktopBridge:
    """Session-scoped, authenticated gateway to the desktop agent."""

    def __init__(self, agent: DesktopAgent, *,
                 ttl: float = 3600.0,
                 clock: Callable[[], float] | None = None) -> None:
        self.agent = agent
        self.ttl = ttl
        self._clock = clock or time.time
        self._sessions: dict[str, BridgeSession] = {}
        self._next = 0

    # -- sessions -----------------------------------------------------------------

    def open_session(self, actor: str, project_id: str, *,
                     scopes: tuple[str, ...] | list[str] = (),
                     ttl: float | None = None,
                     bridge_id: str = "") -> BridgeSession:
        if not actor or not project_id:
            raise DesktopBridgeError("actor and project_id are required")
        self._next += 1
        session = BridgeSession(
            bridge_id=bridge_id or f"bridge-{self._next:06d}",
            actor=actor, project_id=project_id, scopes=frozenset(scopes),
            expires_at=self._clock() + (self.ttl if ttl is None else ttl))
        self._sessions[session.bridge_id] = session
        return session

    def close_session(self, bridge_id: str) -> bool:
        return self._sessions.pop(bridge_id, None) is not None

    def session(self, bridge_id: str) -> BridgeSession:
        session = self._sessions.get(bridge_id)
        if session is None:
            raise DesktopBridgeError(f"unknown bridge session {bridge_id!r}")
        if session.expired(self._clock()):
            raise DesktopBridgeError(f"bridge session {bridge_id!r} expired")
        return session

    # -- submissions -----------------------------------------------------------------

    def submit(self, bridge_id: str, request: DesktopRequest, *,
               approval_token_id: str = "") -> DesktopActionResult:
        """Run one desktop action through the agent pipeline."""
        session = self.session(bridge_id)
        scoped = DesktopRequest(
            action=request.action, target=request.target,
            params=dict(request.params), agent=session.actor,
            task_id=request.task_id, reason=request.reason)
        return self.agent.act(scoped, approval_token_id=approval_token_id)

    def snapshot(self, bridge_id: str) -> dict[str, Any]:
        """Provider state snapshot (never secrets)."""
        session = self.session(bridge_id)
        provider = self.agent.provider
        healthy = False
        try:
            healthy = bool(provider.healthy())
        except Exception:
            healthy = False
        payload: dict[str, Any] = {"healthy": healthy,
                                   "session": session.to_dict()}
        snap = getattr(provider, "snapshot", None)
        if callable(snap):
            try:
                payload["provider"] = snap()
            except Exception as exc:
                payload["provider"] = {
                    "error": f"snapshot failed: {type(exc).__name__}"}
        return payload

    def provider_kind(self) -> str:
        provider = self.agent.provider
        module = type(provider).__module__
        name = type(provider).__name__
        return f"{module}.{name}"

    def simulate(self, bridge_id: str, *, width: int | None = None,
                 height: int | None = None) -> bool:
        """Explicitly reconfigure the fake provider (never a real one)."""
        self.session(bridge_id)  # must be authenticated
        provider = self.agent.provider
        if type(provider).__name__ != "FakeDesktopProvider":
            raise DesktopBridgeError("simulate() requires a fake provider")
        if width is not None:
            provider.screen_width = width
        if height is not None:
            provider.screen_height = height
        return True
