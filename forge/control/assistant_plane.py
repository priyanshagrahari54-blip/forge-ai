"""Assistant plane — the A84 personal-assistant surface of the control plane.

This module wires the A84 capability stack *onto the existing plane*, sharing
one database, one policy path, one audit stream and one Model Fabric:

* ``SessionLedger``      — durable assistant sessions over plane SQLite
* ``PersonalMemoryService`` — short-term RAM + LongTermMemory-backed retention
* ``PromptIntelligence`` / strategy ledger / version store
* ``ToolIntelligence``   — registry/planner/verifier over the same descriptors
* ``DeepResearchEngine`` — over the A81 secure research engine
* ``PatternGraph`` / ``OperationalLedger`` / ``RoutingPriors`` /
  ``PreferenceObserver`` / ``ImprovementEngine`` — learning layers
* ``CritiqueLoop`` / ``ModelTeam`` — verification and cross-model work
* ``AssistantCore`` — the pipeline itself

The plane owns construction (lazily, once per control plane) and exposes
dict-returning methods for the API/cockpit; every execution step still goes
through the existing supervisor, orchestrator or fabric — there is no second
execution path here, and no capability the substrate lacks is pretended at.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, List, Optional

from forge.assistant.behavior import AssistantBehavior
from forge.assistant.context import ContextEngine
from forge.assistant.continuity import ContinuityResolver
from forge.assistant.core import AssistantCore
from forge.assistant.memory import PersonalMemoryService
from forge.assistant.personalization import PreferenceProfile
from forge.assistant.sessions import SessionLedger
from forge.improvement.proposals import ImprovementEngine
from forge.learning.operational import OperationalLedger
from forge.learning.preferences import PreferenceObserver
from forge.learning.routing import RoutingPriors
from forge.memory.engine import LongTermMemory
from forge.models.teams import ModelTeam
from forge.patterns.graph import PatternGraph
from forge.prompt_intelligence.pipeline import PromptIntelligence
from forge.prompt_intelligence.strategies import PromptStrategyLedger
from forge.prompt_intelligence.versions import PromptLedger
from forge.research.deep import DeepResearchEngine
from forge.tools.intelligence.planner import ToolPlanner
from forge.tools.intelligence.registry import builtin_registry
from forge.tools.intelligence.verification import ToolVerifier
from forge.verification.critic import Critic
from forge.verification.loop import CritiqueLoop
from forge.verification.verifier import EvidenceVerifier

__all__ = ["AssistantPlane"]

PROJECT = "personal-assistant"

#: A37-shaped memory scope: the assistant owns one memory namespace,
#: gated like every other session memory surface.
ASSISTANT_MEMORY_SCOPE = "session:assistant"


class _PlaneAudit:
    """Duck-adapter: A84 services call ``record_decision``; the plane audits
    through ``ControlPlane._audit`` so everything lands in one audit stream."""

    def __init__(self, plane: Any, actor: str) -> None:
        self._plane = plane
        self._actor = actor

    _ALLOWED_RESOURCES = ("memory", "agent", "model", "network")

    def record_decision(self, *, agent: str = "", resource: str = "",
                        operation: str = "", decision: str = "",
                        **fields: Any) -> None:
        # The plane audit API takes (actor, resource, operation, allowed) and
        # the policy resource vocabulary; extra detail rides in the reason so
        # nothing is silently dropped by signature mismatch.
        safe = resource if resource in self._ALLOWED_RESOURCES else "agent"
        detail = ", ".join(f"{k}={str(v)[:80]}" for k, v in fields.items()
                           if k not in ("reason",))
        self._plane._audit(self._actor, safe, (operation or "call")[:40],
                           str(decision) in ("allow", "success", "stored"),
                           reason=("assistant-plane via " + (agent or "core")
                                   + (" " + detail if detail else ""))[:300])

    def record(self, event: Any) -> None:  # PermissionAuditEvent passthrough
        self._plane.audit.record(event)


class AssistantPlane:
    """One assistant, one memory, one registry — attached to the real plane."""

    def __init__(self, plane: Any) -> None:
        self._plane = plane
        self._lock = threading.Lock()
        db = plane._db
        self.ledger = SessionLedger(db)
        self.pattern_graph = PatternGraph(db)
        self.strategy_ledger = PromptStrategyLedger(db)
        self.versions = PromptLedger(db)
        self.operational = OperationalLedger(db)
        self.priors = RoutingPriors(db)
        self.observer = PreferenceObserver()
        self.engine = LongTermMemory(db=db, project=PROJECT)
        self.memory = PersonalMemoryService(
            self.engine, pattern_graph=self.pattern_graph,
            preference_observer=self.observer)
        self.context = ContextEngine(memory_service=self.memory,
                                     ledger=self.ledger,
                                     project_state_provider=self._project_state)
        self.registry = builtin_registry()
        self.tool_planner = ToolPlanner(self.registry)
        self.tool_verifier = ToolVerifier(self.registry)
        self.deep_research = DeepResearchEngine(root=self._research_root())
        self.critique = CritiqueLoop(Critic(fabric=plane.fabric),
                                      EvidenceVerifier())
        self.teams = ModelTeam(plane.fabric)
        self.improvement = ImprovementEngine(
            operational=self.operational, strategy_ledger=self.strategy_ledger,
            routing_priors=self.priors, fabric=plane.fabric)
        # Fabric routing may *consult* learned priors — attached explicitly,
        # capped, and only after every hard filter (see attach_learning).
        try:
            plane.fabric.attach_learning(self.priors)
        except Exception:
            pass
        self._cores: Dict[str, AssistantCore] = {}

    def _research_root(self) -> str:
        projects = getattr(self._plane, "projects", None) or {}
        try:
            for project in projects.values():
                root = str(getattr(project, "root", "") or "")
                if root:
                    return root
        except Exception:
            pass
        return "."

    # -- core factory ------------------------------------------------------------

    def _core(self, session: Any) -> AssistantCore:
        """Per-caller-session core: shared stores, session-bound delegates."""
        plane = self._plane
        actor = getattr(session, "actor", "local-user")

        def submit_task(requirement: str) -> Dict[str, Any]:
            run = plane.submit_task(session, requirement)
            return {"task_id": getattr(run, "id", ""),
                    "state": str(getattr(run, "status", "") or
                            getattr(run, "state", ""))}

        def orchestrate(requirement: str) -> Dict[str, Any]:
            record = plane.submit_orchestration(session, requirement)
            return {"orchestration_id": getattr(record, "id", "")}

        def live_answer(text: str, extra: Dict[str, Any]) -> Any:
            return plane._model_conversation_reply(session, text)

        return AssistantCore(
            ledger=self.ledger, memory_service=self.memory,
            context_engine=self.context,
            prompt_intelligence=PromptIntelligence(
                context_provider=None, strategy_ledger=self.strategy_ledger),
            prompt_ledger=self.versions,
            behavior=AssistantBehavior(),
            continuity_resolver=ContinuityResolver(
                ledger=self.ledger, memory=self.memory,
                task_lookup=self._task_lookup(session)),
            tool_registry=self.registry, tool_planner=self.tool_planner,
            tool_verifier=self.tool_verifier,
            deep_research=self.deep_research, critique_loop=self.critique,
            model_team=self.teams, fabric=plane.fabric,
            improvement_engine=self.improvement,
            submit_task=submit_task,
            orchestrate=orchestrate if hasattr(plane, "submit_orchestration")
            else None,
            live_answer=live_answer,
            profile_provider=lambda: PreferenceProfile.from_memory(
                self.engine, project=PROJECT),
            audit=_PlaneAudit(plane, actor).record_decision,
            default_project=PROJECT)

    def _task_lookup(self, session: Any) -> Callable[[str], Dict[str, Any]]:
        plane = self._plane

        def lookup(task_id: str) -> Dict[str, Any]:
            try:
                run = plane.get_task(session, task_id)
            except Exception:
                return {}
            return {"id": getattr(run, "id", task_id),
                    "state": str(getattr(run, "status", "") or
                            getattr(run, "state", "")),
                    "requirement": str(getattr(run, "requirement", ""))[:400]}
        return lookup

    def _project_state(self) -> List[str]:
        """Registered projects + session counts, read from real plane state."""
        plane = self._plane
        out: List[str] = []
        try:
            for project_id, project in list(
                    (getattr(plane, "projects", None) or {}).items())[:4]:
                out.append(f"project {project_id} at {getattr(project, 'root', '?')}")
        except Exception:
            pass
        return out[:4]

    # -- conversation ---------------------------------------------------------------

    def respond(self, session: Any, message: str, *,
                assistant_session_id: str = "", allow_web: bool = False,
                confirmed: bool = False) -> Dict[str, Any]:
        core = self._core(session)
        sid = assistant_session_id or f"plane-{getattr(session, 'id', 'local')}"
        started = time.time()
        response = core.respond(sid, message, project=PROJECT,
                               allow_web=allow_web, confirmed=confirmed)
        payload = response.to_dict()
        payload["duration_ms"] = round((time.time() - started) * 1000.0, 1)
        return payload

    # -- sessions ---------------------------------------------------------------------

    def session_state(self, session: Any, assistant_session_id: str) -> Dict[str, Any]:
        snapshot = self.ledger.get(assistant_session_id)
        if snapshot is None:
            return {"found": False, "session_id": assistant_session_id}
        return {"found": True, **snapshot.to_dict(include_history=True)}

    def sessions(self, *, limit: int = 25) -> Dict[str, Any]:
        rows = self.ledger.list(limit=max(1, min(int(limit), 100)))
        return {"sessions": [r.to_dict(include_history=False) for r in rows],
                "count": len(rows)}

    def close_assistant_session(self, assistant_session_id: str) -> Dict[str, Any]:
        self.ledger.close(assistant_session_id)
        return {"closed": assistant_session_id}

    def set_retention(self, assistant_session_id: str, mode: str, *,
                      session: Any = None) -> Dict[str, Any]:
        ok = self.ledger.set_retention_mode(assistant_session_id, mode)
        return {"updated": bool(ok), "mode": str(mode)[:16],
                "note": "retention modes: normal | disabled (short-term only)"}

    def clear_assistant_session(self, assistant_session_id: str) -> Dict[str, Any]:
        return {"cleared": self.ledger.clear_session(assistant_session_id)}

    def continuity(self, assistant_session_id: str, text: str) -> Dict[str, Any]:
        resolver = ContinuityResolver(ledger=self.ledger, memory=self.memory)
        detected = resolver.detect(text)
        resolved = resolver.resolve(assistant_session_id, text)
        return {"detected": detected, "bundle": resolved.to_dict()}

    # -- memory controls (user-controlled: inspect/correct/delete/forget/clear) ------

    def _gated(self, session: Any, operation: str, *,
               approval_id: str = "") -> Optional[Dict[str, Any]]:
        """Route A84 memory mutations through the plane's A33 memory gate.

        Returns None when the operation is authorized (proceed) or the
        permission dict when approval/ denial is required — the same shape the
        A37 memory routes already speak. Reads stay ungated-but-scoped;
        mutations do not run on the assistant's word alone.
        """
        if session is None or operation not in ("write", "delete"):
            return None
        permission = self._plane._memory_permission(
            session, operation, ASSISTANT_MEMORY_SCOPE,
            approval_id=approval_id)
        if permission["allowed"]:
            return None
        return permission

    def memory_search(self, query: str, *, k: int = 8) -> Dict[str, Any]:
        hits = self.memory.recall(query, k=k, project=PROJECT)
        return {"hits": [self._memory_row(h) for h in hits],
                "count": len(hits),
                "note": "redacted at rest; provenance on every record"}

    def memory_inspect(self, *, memory_type: str = "", limit: int = 50) -> Dict[str, Any]:
        payload = dict(self.memory.inspect(
            project=PROJECT, memory_type=memory_type or None,
            limit=limit))
        payload["count"] = len(payload.get("entries") or ())
        payload["note"] = ("content is stored redacted; every entry carries "
                           "source/timestamp/confidence provenance")
        return payload

    def memory_provenance(self, memory_id: str) -> Dict[str, Any]:
        return {"memory_id": memory_id,
                "events": self.memory.provenance(memory_id)}

    def memory_correct(self, memory_id: str, new_content: str, *,
                       session: Any = None,
                       approval_id: str = "") -> Dict[str, Any]:
        denied = self._gated(session, "write", approval_id=approval_id)
        if denied is not None:
            return denied
        record = self.memory.correct(memory_id, new_content, project=PROJECT)
        return {"corrected": record is not None,
                "memory_id": getattr(record, "id", memory_id),
                "note": "corrections are versioned; the prior record remains "
                        "in the provenance trail"}

    def memory_delete(self, memory_id: str, *, session: Any = None,
                      approval_id: str = "") -> Dict[str, Any]:
        denied = self._gated(session, "delete", approval_id=approval_id)
        if denied is not None:
            return denied
        return {"deleted": bool(self.memory.delete(memory_id, project=PROJECT))}

    def memory_forget(self, memory_id: str, *, session: Any = None,
                      approval_id: str = "") -> Dict[str, Any]:
        denied = self._gated(session, "delete", approval_id=approval_id)
        if denied is not None:
            return denied
        result = self.memory.forget(memory_id, project=PROJECT)
        # the service answers with a purge report; "forgotten" is only ever
        # what actually got purged — never "anything non-empty is success"
        if isinstance(result, dict):
            forgotten = bool(result.get("purged"))
        else:
            forgotten = bool(result)
        return {"forgotten": forgotten, "result": result,
                "note": ("forget purges content + embeddings + pattern refs; "
                         "irreversible by design" if forgotten else
                         "nothing with that id was stored, so nothing was "
                         "forgotten")}

    def memory_short_term(self, assistant_session_id: str) -> Dict[str, Any]:
        window = self.memory.short_term(assistant_session_id) or []
        return {"messages": [dict(m) if isinstance(m, dict) else
                             getattr(m, "__dict__", {}) for m in window],
                "count": len(window),
                "note": "short-term lives in RAM only — never persisted"}

    def memory_clear(self, confirm: str, *, session: Any = None,
                     approval_id: str = "") -> Dict[str, Any]:
        denied = self._gated(session, "delete", approval_id=approval_id)
        if denied is not None:
            return denied
        try:
            return self.memory.clear_long_term(confirm=confirm,
                                               project=PROJECT)
        except ValueError as exc:
            # refusal is a first-class outcome, not a server error: the
            # exact confirmation phrase is required (C3).
            return {"removed": 0, "refused": str(exc)[:300],
                    "reversible": False}

    def _memory_row(self, hit: Any) -> Dict[str, Any]:
        record = getattr(hit, "record", hit)
        return {"id": str(getattr(record, "id", "")),
                "memory_type": str(getattr(record, "memory_type", "")),
                "content": str(getattr(record, "content", ""))[:400],
                "importance": round(float(getattr(record, "importance", 0.0)), 3),
                "created_at": getattr(record, "created_at", None),
                "score": getattr(hit, "score", None)}

    # -- profile --------------------------------------------------------------------------

    def profile(self) -> Dict[str, Any]:
        profile = PreferenceProfile.from_memory(self.engine, project=PROJECT)
        return profile.to_dict() if hasattr(profile, "to_dict") else {
            "values": dict(getattr(profile, "values", {}))}

    def profile_set(self, field_name: str, value: str, *, session: Any = None,
                    approval_id: str = "") -> Dict[str, Any]:
        profile = PreferenceProfile.from_memory(self.engine, project=PROJECT)
        try:
            # vocabulary/safety validation first: a refused field never needs
            # (and never gets) an authorization round-trip
            profile.set(field_name, value)
        except ValueError as exc:
            return {"saved": False, "error": str(exc)[:300]}
        denied = self._gated(session, "write", approval_id=approval_id)
        if denied is not None:
            return denied
        saved = profile.save_to(self.engine, project=PROJECT)
        return {"saved": bool(saved.get("saved", True)) if isinstance(saved, dict)
                else bool(saved), "field": field_name,
                "note": "profile fields persist as correctable/forgettable "
                        "memories; soft hints only — never override safety"}

    def profile_proposals(self) -> Dict[str, Any]:
        proposals = self.observer.pending()
        return {"proposals": [p.to_dict() for p in proposals],
                "count": len(proposals),
                "rejections": list(self.observer.reject_reasons()),
                "note": "preference proposals require explicit confirmation "
                        "before they become profile fields"}

    # -- prompts -------------------------------------------------------------------------

    def prompt_versions(self, assistant_session_id: str,
                        *, limit: int = 20) -> Dict[str, Any]:
        rows = self.versions.for_session(assistant_session_id, limit=limit) \
            if assistant_session_id else self.versions.recent(limit=limit)
        return {"versions": [r.to_dict() for r in rows], "count": len(rows)}

    def prompt_strategy_stats(self) -> Dict[str, Any]:
        return self.strategy_ledger.stats()

    # -- tools --------------------------------------------------------------------------------

    def tool_catalog(self) -> Dict[str, Any]:
        tools = []
        availability: Dict[str, Any] = {}
        for descriptor in sorted(self.registry.all(), key=lambda d: d.name):
            tools.append(descriptor.to_dict())
            try:
                availability[descriptor.name] = self.registry.availability(
                    descriptor.name)
            except Exception as exc:
                availability[descriptor.name] = {
                    "state": "error", "detail": type(exc).__name__}
        return {"tools": tools, "count": len(tools),
                "availability": availability,
                "honesty": "registry descriptions are NOT permissions; "
                           "execution goes through A33-gated paths only"}

    def tool_plan(self, text: str) -> Dict[str, Any]:
        from forge.prompt_intelligence.pipeline import PromptIntelligence as _PI
        enhanced = _PI().enhance(text)
        plan = self.tool_planner.plan_for_request(enhanced)
        return plan.to_dict()

    # -- research --------------------------------------------------------------------------------

    def research_deep(self, question: str, *, allow_web: bool = False) -> Dict[str, Any]:
        report = self.deep_research.run(question, allow_web=allow_web)
        payload = report.to_dict() if hasattr(report, "to_dict") else dict(report)
        for record in tuple(payload.get("evidence") or ())[:40]:
            self.memory.pattern_graph.ingest_text(
                str(record.get("snippet", ""))[:600],
                source=str(record.get("cite", "research")))
        return payload

    def networks_status(self) -> Dict[str, Any]:
        from forge.research.networks import SafeResearchNetworkGateway
        status = SafeResearchNetworkGateway().status()
        payload = status.to_dict() if hasattr(status, "to_dict") else dict(status)
        return {"gateway": payload,
                "note": "privacy-network research stays inert until an "
                        "operator supplies an isolated transport; legal/"
                        "authorized research only"}

    def network_classify(self, url: str) -> Dict[str, Any]:
        from forge.research.networks import TorUrlValidator, classify_url
        validation = TorUrlValidator().validate(url) \
            if hasattr(TorUrlValidator(), "validate") else {}
        return {"network_class": classify_url(url),
                "url_validation": validation if isinstance(validation, dict)
                else validation.to_dict()}

    def network_screen(self, goal: str) -> Dict[str, Any]:
        from forge.research.networks import screen_goal
        screen = screen_goal(goal)
        return screen.to_dict() if hasattr(screen, "to_dict") else dict(screen)

    # -- patterns / learning / improvement ---------------------------------------------------------

    def pattern_stats(self) -> Dict[str, Any]:
        return {"summary": self.pattern_graph.summary(),
                "open_conflicts": self.pattern_graph.open_conflicts(limit=20)}

    def pattern_adjudicate(self, conflict_id: str, resolution: str, *,
                           note: str = "") -> Dict[str, Any]:
        return self.pattern_graph.adjudicate(conflict_id, resolution, note=note)

    def learning_stats(self) -> Dict[str, Any]:
        return {"operational": {"events": self.operational.count(),
                                "stats": self.operational.stats()},
                "routing_priors": self.priors.report(limit=30),
                "governance": {"weights_trained": False,
                               "conversations_stored_by_default": False,
                               "priors_can_relax_filters": False,
                               "priors_max_abs_adjustment":
                                   self.priors.max_adjustment}}

    def improvement_scan(self) -> Dict[str, Any]:
        proposals = self.improvement.scan()
        return {"proposals": [p.to_dict() for p in proposals],
                "count": len(proposals),
                "governance": self.improvement.governance()}

    # -- verification / teams --------------------------------------------------------------------

    def critique_review(self, text: str, *, requirements: Optional[List[str]] = None,
                        citations: Optional[List[str]] = None) -> Dict[str, Any]:
        from types import SimpleNamespace
        enhanced = SimpleNamespace(goals=tuple(requirements or ()),
                                   verification=())
        outcome = self.critique.critic.review(
            text, enhanced=enhanced,
            evidence=[{"cite": c} for c in tuple(citations or ())])
        to_dict = getattr(outcome, "to_dict", None)
        return to_dict() if callable(to_dict) else dict(outcome) \
            if isinstance(outcome, dict) else {"summary": str(outcome)[:2000]}

    def team_run(self, task: str, *, shape: str = "") -> Dict[str, Any]:
        result = self.teams.run(task, shape=shape)
        return result.to_dict() if hasattr(result, "to_dict") else dict(result)

    def model_scale_catalog(self) -> Dict[str, Any]:
        """Declared model scale view — architecture facts, never hosting claims."""
        from forge.models.model_class import ALL_MODEL_CLASSES
        models = []
        try:
            for model in self._plane.fabric.models():
                scale = model.scale
                models.append({
                    "name": model.name, "provider": model.provider,
                    "model_class": model.model_class or "undeclared",
                    "scale": scale.to_dict(),
                    "scale_band": scale.band,
                    "available": bool(model.available),
                    "declared_by": scale.declared_by or "not disclosed",
                })
        except Exception as exc:
            return {"error": type(exc).__name__, "models": []}
        return {"models": models, "count": len(models),
                "vocabulary": list(ALL_MODEL_CLASSES),
                "legend": "band: declared parameter-scale band; unknown means "
                          "NOT DISCLOSED — Forge never invents counts and "
                          "never claims to host weights it routes to",
                "bands": ["small <2B", "medium <16B", "large <70B",
                          "xlarge <200B", "huge <1T", "massive >=1T",
                          "unknown = not disclosed (never guessed; never means small)"]}
