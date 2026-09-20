"""Personal Assistant Core — the A84 target pipeline.

```
User
  ↓ session + short-term memory          (SessionLedger)
  ↓ continuity resolution                (ContinuityResolver)
  ↓ prompt intelligence + quality gate   (forge.prompt_intelligence)
  ↓ behavior triage                      (AssistantBehavior)
  ↓ context assembly + quality           (ContextEngine)
  ↓ personalization                      (PreferenceProfile)
  ↓ specialist + model selection         (ModelTeam / ModelFabric routing)
  ↓ execution (delegated — never direct) (submit_task / deep research / fabric)
  ↓ cross verification                   (forge.verification CritiqueLoop)
  ↓ synthesis + honest provenance        (this module)
  ↓ controlled memory update             (PersonalMemoryService)
```

Every execution step is *delegated to the existing subsystem* through
injected callables so the core works standalone (tests, CLI) and inside the
control plane (cockpit/server) without a second implementation of anything:

* ``submit_task`` — the A32 supervisor pipeline (every write passes A33);
* ``orchestrate`` — the A38 multi-agent orchestrator;
* ``live_answer`` — the A43 model-conversation channel (runtime-verified
  models only — provenance included);
* ``deep_research`` — the A84 Stage F engine over the A81 secure engine.

If a delegate is absent, the core does not fake the capability: it answers
from what it truly has, and names what it could not do (Stage R).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from forge.assistant.behavior import Action, AssistantBehavior, TriageResult
from forge.assistant.continuity import ContinuityResolver
from forge.assistant.personalization import PreferenceProfile
from forge.prompt_intelligence.pipeline import PromptIntelligence
from forge.prompt_intelligence.quality import PromptQualityEvaluator

__all__ = ["AssistantCore", "AssistantResponse"]

MAX_REPLY_CHARS = 6000


@dataclass(frozen=True)
class AssistantResponse:
    """One completed assistant turn — answer plus the receipts behind it."""

    kind: str                      # triage action
    text: str
    session_id: str
    trace_id: str
    intent: Dict[str, Any] = field(default_factory=dict)
    quality: Dict[str, Any] = field(default_factory=dict)
    continuity: Dict[str, Any] = field(default_factory=dict)
    context_quality: Dict[str, Any] = field(default_factory=dict)
    tool_plan: Dict[str, Any] = field(default_factory=dict)
    verification: Dict[str, Any] = field(default_factory=dict)
    memory: Dict[str, Any] = field(default_factory=dict)
    task_id: str = ""
    report: Dict[str, Any] = field(default_factory=dict)
    citations: Tuple[str, ...] = ()
    provenance: str = "deterministic"   # deterministic | model:<name> | delegated:<path>
    needs_clarification: bool = False
    needs_confirmation: bool = False
    at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "text": self.text[:MAX_REPLY_CHARS],
                "session_id": self.session_id, "trace_id": self.trace_id,
                "intent": self.intent, "quality": self.quality,
                "continuity": self.continuity,
                "context_quality": self.context_quality,
                "tool_plan": self.tool_plan, "verification": self.verification,
                "memory": self.memory, "task_id": self.task_id,
                "report": self.report, "citations": list(self.citations),
                "provenance": self.provenance,
                "needs_clarification": self.needs_clarification,
                "needs_confirmation": self.needs_confirmation,
                "at": self.at}


class AssistantCore:
    """One continuing assistant, in front of everything Forge already is."""

    def __init__(self, *, ledger: Any, memory_service: Any,
                 context_engine: Any = None,
                 prompt_intelligence: Optional[PromptIntelligence] = None,
                 prompt_ledger: Any = None,
                 quality_evaluator: Optional[PromptQualityEvaluator] = None,
                 prompt_adapter: Any = None,
                 behavior: Optional[AssistantBehavior] = None,
                 continuity_resolver: Optional[ContinuityResolver] = None,
                 tool_registry: Any = None, tool_planner: Any = None,
                 tool_verifier: Any = None,
                 deep_research: Any = None, critique_loop: Any = None,
                 model_team: Any = None, fabric: Any = None,
                 improvement_engine: Any = None,
                 submit_task: Optional[Callable[[str], Dict[str, Any]]] = None,
                 orchestrate: Optional[Callable[[str], Dict[str, Any]]] = None,
                 live_answer: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
                 profile_provider: Optional[Callable[[], PreferenceProfile]] = None,
                 audit: Any = None,
                 default_project: str = "personal") -> None:
        self.ledger = ledger
        self.memory = memory_service
        self.context_engine = context_engine
        self.prompt_intelligence = prompt_intelligence or PromptIntelligence(
            context_provider=self._recall_for_prompt)
        self.prompt_ledger = prompt_ledger
        self.quality = quality_evaluator or PromptQualityEvaluator()
        self.prompt_adapter = prompt_adapter
        self.behavior = behavior or AssistantBehavior()
        self.continuity = continuity_resolver or ContinuityResolver(
            ledger=ledger, memory=memory_service)
        self.tool_registry = tool_registry
        self.tool_planner = tool_planner
        self.tool_verifier = tool_verifier
        self.deep_research = deep_research
        self.critique_loop = critique_loop
        self.model_team = model_team
        self.fabric = fabric
        self.improvement = improvement_engine
        self.submit_task = submit_task
        self.orchestrate = orchestrate
        self.live_answer = live_answer
        self.profile_provider = profile_provider
        self.audit = audit
        self.default_project = default_project

    # -- context provider hook used by prompt intelligence -------------------------

    def _recall_for_prompt(self, query: str) -> List[str]:
        if self.memory is None or not hasattr(self.memory, "recall"):
            return []
        try:
            hits = self.memory.recall(query, k=4)
        except Exception:
            return []
        out: List[str] = []
        for hit in hits:
            record = getattr(hit, "record", hit)
            text = str(getattr(record, "content", ""))[:400]
            if text:
                out.append(text)
        return out

    # -- the pipeline ------------------------------------------------------------------

    def respond(self, session_id: str, message: str, *,
                project: str = "", allow_web: bool = False,
                confirmed: bool = False) -> AssistantResponse:
        message = (message or "").strip()
        if not message:
            raise ValueError("message must be non-empty")
        project = project or self.default_project
        trace_id = "%d" % time.time_ns()
        session = self.ledger.get(session_id) if self.ledger else None
        if session is None and self.ledger is not None:
            session = self.ledger.open(project_id=project,
                                       plane_session_id=session_id,
                                       first_message=message,
                                       session_id=session_id)
        retention_mode = str(getattr(session, "retention_mode", "normal")
                             or "normal") if session is not None else "normal"

        if self.ledger is not None:
            self.ledger.record_turn(session_id, "user", message,
                                    kind="message")
        if self.memory is not None:
            try:
                self.memory.push_short_term(session_id, "user", message)
            except Exception:
                pass

        # 1. continuity
        continuity = self.continuity.resolve(session_id, message)

        # 2. prompt intelligence
        extra = [continuity.context_text] if continuity.context_text else []
        enhanced = self.prompt_intelligence.enhance(message,
                                                    extra_context=extra)
        quality = self.quality.evaluate(enhanced)

        # 3. behavior triage
        triage = self.behavior.triage(message, enhanced=enhanced,
                                      continuity=continuity, quality=quality,
                                      capabilities=enhanced.capabilities)

        version_id = self._record_version(session_id, project, trace_id,
                                          enhanced, quality, triage)

        # 4. early exits: ask / confirm — never silent guessing (D3/Q)
        if triage.action == Action.CLARIFY:
            return self._finish(session_id, project, trace_id, version_id,
                                kind=Action.CLARIFY,
                                text=triage.clarification or "Please clarify.",
                                enhanced=enhanced, quality=quality,
                                continuity=continuity, triage=triage,
                                needs_clarification=True,
                                memory_text="")
        if triage.action == Action.CONFIRM and not confirmed:
            return self._finish(session_id, project, trace_id, version_id,
                                kind=Action.CONFIRM,
                                text=triage.confirmation_prompt,
                                enhanced=enhanced, quality=quality,
                                continuity=continuity, triage=triage,
                                needs_confirmation=True,
                                memory_text="")

        # 5. context (after the routing question: budget needs the model)
        profile = self._profile()
        context_bundle = None
        model_window = 0
        if self.fabric is not None:
            try:
                chosen = self.fabric.select(capability=(
                    enhanced.capabilities[0] if enhanced.capabilities
                    else "reasoning"))
                model_window = int(getattr(chosen, "context_window", 0) or 0)
            except Exception:
                model_window = 0
        if self.context_engine is not None:
            context_bundle = self.context_engine.build(
                message, session_id=session_id,
                model_context_window=model_window)
        context_text = context_bundle.render() if context_bundle else ""

        # 6. tool plan (advisory; execution still passes A33 at the boundary)
        tool_plan: Dict[str, Any] = {}
        if self.tool_planner is not None:
            try:
                plan = self.tool_planner.plan_for_request(enhanced)
                tool_plan = plan.to_dict()
            except Exception:
                tool_plan = {}

        # 7. execution per action
        text, report, citations, task_id, provenance = self._execute(
            triage, message, enhanced, context_text, profile,
            allow_web=allow_web, project=project, continuity=continuity)

        # 8. verification pass on high-impact outputs
        verification: Dict[str, Any] = {}
        if triage.action in (Action.CODE, Action.RESEARCH,
                             Action.ORCHESTRATE) and self.critique_loop \
                is not None and triage.action != Action.ORCHESTRATE:
            verification = self._verify(text, enhanced, citations)

        # 9. memory update "where appropriate"
        memory_outcome = self._update_memory(session_id, project,
                                             retention_mode, message, text)

        # 10. attach refs + finalize
        if self.ledger is not None:
            if task_id:
                self.ledger.set_active_task(session_id, task_id)
            if report.get("report_id"):
                self.ledger.record_ref(session_id, "research",
                                        str(report["report_id"]),
                                        note=str(message)[:120])

        response = self._finish(session_id, project, trace_id, version_id,
                                kind=triage.action, text=text,
                                enhanced=enhanced, quality=quality,
                                continuity=continuity, triage=triage,
                                task_id=task_id, report=report,
                                citations=citations, provenance=provenance,
                                memory_text=message,
                                verification=verification,
                                memory_outcome=memory_outcome,
                                context_bundle=context_bundle,
                                tool_plan=tool_plan)
        if self.prompt_ledger is not None and version_id:
            try:
                self.prompt_ledger.record_result(
                    version_id, outcome="success", result=text,
                    latency_ms=0.0)
            except Exception:
                pass
        return response

    # -- execution branches ---------------------------------------------------------

    def _execute(self, triage: TriageResult, message: str, enhanced: Any,
                 context_text: str, profile: Optional[PreferenceProfile], *,
                 allow_web: bool, project: str,
                 continuity: Any) -> Tuple[str, Dict[str, Any], Tuple[str, ...],
                                           str, str]:
        action = triage.action

        if action == Action.RECOVER_CONTINUE:
            bits = ["Recovered from real session state:"]
            for item in continuity.items[:5]:
                bits.append("- " + str(item.get("label", ""))[:160])
            task_id = ""
            if self.submit_task is not None:
                requirement = ("continue: " + message.strip()[:400])
                first = next((str(i.get("id", "")) for i in continuity.items
                              if i.get("type") == "task"), "")
                if first:
                    requirement = (f"continue from task {first}: resume and "
                                   "carry on where it stopped")
                try:
                    task = self.submit_task(requirement) or {}
                    task_id = str(task.get("task_id", ""))[:64]
                    bits.append(f"Submitted as task {task_id or '?'}.")
                except Exception as exc:
                    bits.append(f"Could not submit the continuation: "
                                f"{type(exc).__name__}")
            return ("\n".join(bits)[:MAX_REPLY_CHARS], {}, (), task_id,
                    "delegated:continuity")

        if action == Action.RESEARCH:
            if self.deep_research is None:
                return ("Deep research is not attached to this assistant "
                        "instance (no research engine configured). I will "
                        "not fake it; the deterministic channel can still "
                        "answer from project state.", {}, (), "",
                        "deterministic")
            try:
                report = self.deep_research.run(message, allow_web=allow_web)
            except Exception as exc:
                return (f"Deep research failed: {type(exc).__name__}: "
                        f"{str(exc)[:200]} — reported as failed, not "
                        "backfilled.", {"error": str(exc)[:200]}, (), "",
                        "delegated:deep-research")
            payload = report.to_dict() if hasattr(report, "to_dict") else dict(report)
            # reports are ephemeral; mint a stable content-derived id so the
            # session can *reference* this research later (continuity), and
            # so refs never dangle on an id that was never real.
            if not payload.get("report_id"):
                import hashlib
                payload["report_id"] = "dr-" + hashlib.sha1(
                    message.encode("utf-8")).hexdigest()[:12]
            citations = tuple(
                str(c.get("cite") or c.get("url") or c.get("path") or "")
                for c in (payload.get("citations") or ()))[:12]
            unknowns = tuple(payload.get("unknowns") or ())
            text = payload.get("summary", "") or ""
            if unknowns:
                text += ("\n\nWhat is unknown: " + "; ".join(
                    str(u)[:160] for u in unknowns[:3]))
            if not text:
                text = ("No verified evidence found; I will not guess. "
                        "Consulted sources and their outcomes are listed in "
                        "the report.")
            return text[:MAX_REPLY_CHARS], payload, citations, "", \
                "delegated:deep-research"

        if action in (Action.CODE, Action.ORCHESTRATE):
            if action == Action.ORCHESTRATE and self.orchestrate is not None:
                try:
                    result = self.orchestrate(message) or {}
                except Exception as exc:
                    return (f"Orchestration submit failed: {type(exc).__name__}"
                            f": {str(exc)[:200]}", {}, (), "",
                            "deterministic")
                orch_id = str(result.get("orchestration_id", ""))[:64]
                return (f"Planned and dispatched through the orchestrator "
                        f"({orch_id or 'submitted'}); steps run as real "
                        "recorded agent runs behind the A33 gates.",
                        result, (), orch_id, "delegated:orchestrator"
                )
            if self.submit_task is not None:
                try:
                    task = self.submit_task(enhanced.enhanced[:4000]) or {}
                except Exception as exc:
                    return (f"Task submit failed: {type(exc).__name__}: "
                            f"{str(exc)[:160]}", {}, (), "", "deterministic")
                task_id = str(task.get("task_id", ""))[:64]
                return (f"I've queued that through the supervisor pipeline "
                        f"(task {task_id or '?'}): planning, routing, "
                        "generation, tests, review, security and acceptance "
                        "gates apply — the ChangeSet engine performs "
                        "writes only after authorization.",
                        task, (), task_id, "delegated:supervisor")
            # No task pipeline attached (CLI/standalone): answer with the
            # fabric + team, explicitly labelled as a proposal, never an
            # applied change.
            return self._fabric_reply(message, enhanced, context_text, profile)

        # DIRECT_ANSWER / fallback
        return self._direct_reply(message, enhanced, context_text, profile)

    def _direct_reply(self, message: str, enhanced: Any, context_text: str,
                      profile: Optional[PreferenceProfile]
                      ) -> Tuple[str, Dict[str, Any], Tuple[str, ...], str, str]:
        if self.live_answer is not None:
            try:
                reply = self.live_answer(message, {
                    "enhanced": enhanced, "context": context_text,
                    "profile": profile})
            except Exception:
                reply = None
            if reply is not None:
                text = str(getattr(reply, "text", "") or "")
                model = str(getattr(reply, "model", "") or "")
                return (text[:MAX_REPLY_CHARS], {}, (), "",
                        f"model:{model}" if model else "model")
        unknown = AssistantBehavior.evidence_report(context_text,
                                                    len(context_text or ""))
        deterministic = (
            "The deterministic channel has no live-model answer for that. "
            "What Forge can honestly say from real context:\n"
            + (context_text[:1500] if context_text else
               "(nothing retrieved — the memory/context layers hold no "
               "matching record)")
            + "\n\nI can research it, plan it, or run it as a supervised "
            "engineering task; say which.")
        if unknown and not context_text:
            deterministic = unknown
        return deterministic[:MAX_REPLY_CHARS], {}, (), "", "deterministic"

    def _fabric_reply(self, message: str, enhanced: Any, context_text: str,
                      profile: Optional[PreferenceProfile]
                      ) -> Tuple[str, Dict[str, Any], Tuple[str, ...], str, str]:
        """Standalone coding answer: model team or one routed call."""
        if self.model_team is not None:
            try:
                result = self.model_team.run(
                    message, capabilities=enhanced.capabilities,
                    complexity=3.0 if len(enhanced.subtasks) > 2 else 1.5,
                    context=context_text[:8000])
                payload = result.to_dict()
                text = payload.get("synthesis") or ""
                unmet = [a["role"] for a in payload.get("assignments", [])
                         if a.get("unmet")]
                head = ("Model-team proposal (no repository is attached to "
                        "this assistant — nothing was written; run it as a "
                        "task for the supervised pipeline).")
                if unmet:
                    head += (" Unmet roles reported honestly: "
                             + ", ".join(unmet) + ".")
                return (head + "\n\n" + text)[:MAX_REPLY_CHARS], payload, \
                    (), "", "delegated:model-team"
            except Exception as exc:
                return (f"Model-team run failed: {type(exc).__name__}: "
                        f"{str(exc)[:200]} — not hidden behind a canned "
                        "answer."), {}, (), "", "deterministic"
        if self.fabric is None:
            return ("No fabric and no task pipeline are attached: the "
                    "engineering answer would be a guess, and Forge does "
                    "not guess engineering."), {}, (), "", "deterministic"
        from forge.models.request import ModelRequest
        request = ModelRequest(prompt=enhanced.enhanced,
                               capability=enhanced.capabilities[0]
                               if enhanced.capabilities else "coding",
                               task="assistant:plan-only",
                               context=context_text[:8000],
                               complexity=2.0)
        if profile is not None:
            profile.apply_to_request(request)
        response = self.fabric.request(request)
        text = str(getattr(response, "text", "") or "")
        ok = bool(getattr(response, "success", False))
        provenance = "deterministic"
        model = str(getattr(response, "model", "") or "")
        metadata = getattr(response, "metadata", None) or {}
        if ok and model and not metadata.get("simulated"):
            provenance = f"model:{model}"
        return ((text or "The routed model returned no usable output.")
                [:MAX_REPLY_CHARS], {"ok": ok}, (), "", provenance)

    # -- verification / memory / bookkeeping -------------------------------------------

    def _verify(self, text: str, enhanced: Any,
                citations: Tuple[str, ...]) -> Dict[str, Any]:
        try:
            evidence = [{"cite": c} for c in citations]
            outcome = self.critique_loop.run(
                generate=lambda: text, enhanced=enhanced,
                evidence=evidence, claims=tuple(
                    line for line in text.splitlines()
                    if "[" in line)[:8])
            return outcome.to_dict() if hasattr(outcome, "to_dict") else dict(
                outcome)
        except Exception as exc:
            return {"performed": False,
                    "error": f"verification unavailable: "
                             f"{type(exc).__name__}"}

    def _update_memory(self, session_id: str, project: str,
                       retention_mode: str, user_text: str,
                       assistant_text: str) -> Dict[str, Any]:
        if self.memory is None:
            return {"skipped": "no memory service"}
        summary = (f"exchange: {user_text[:200]} -> {assistant_text[:240]}")
        try:
            outcome = self.memory.retain(
                summary, session_id=session_id, intent="auto",
                relevance=0.45, retention_mode=retention_mode,
                project=project, source="assistant-core",
                via="episodic-exchange", reason="exchange worth recalling "
                                                "for continuity")
            try:
                self.memory.push_short_term(session_id, "assistant",
                                            assistant_text, kind="answer")
            except Exception:
                pass
            return outcome if isinstance(outcome, dict) else {}
        except Exception as exc:
            return {"error": type(exc).__name__}

    def _record_version(self, session_id: str, project: str, trace_id: str,
                        enhanced: Any, quality: Any,
                        triage: TriageResult) -> str:
        if self.prompt_ledger is None:
            return ""
        try:
            version = self.prompt_ledger.record(
                enhanced=enhanced, trace_id=trace_id,
                session_id=session_id, model=triage.action,
                quality=quality, outcome="queued")
            return str(getattr(version, "version_id", "") or "")
        except Exception:
            return ""

    def _profile(self) -> Optional[PreferenceProfile]:
        if self.profile_provider is None:
            return None
        try:
            return self.profile_provider()
        except Exception:
            return None

    def _finish(self, session_id: str, project: str, trace_id: str,
                version_id: str, *, kind: str, text: str, enhanced: Any,
                quality: Any, continuity: Any, triage: TriageResult,
                task_id: str = "", report: Optional[Dict[str, Any]] = None,
                citations: Sequence[str] = (), provenance: str = "deterministic",
                memory_text: str = "", needs_clarification: bool = False,
                needs_confirmation: bool = False,
                verification: Optional[Dict[str, Any]] = None,
                memory_outcome: Optional[Dict[str, Any]] = None,
                context_bundle: Any = None,
                tool_plan: Optional[Dict[str, Any]] = None
                ) -> AssistantResponse:
        report = dict(report or {})
        if self.ledger is not None and text:
            try:
                self.ledger.record_turn(session_id, "assistant", text,
                                        kind=kind, task_id=task_id)
            except Exception:
                pass
        response = AssistantResponse(
            kind=kind, text=text[:MAX_REPLY_CHARS], session_id=session_id,
            trace_id=trace_id,
            intent={"goals": list(enhanced.goals),
                    "constraints": [dict(c) for c in enhanced.constraints],
                    "capabilities": list(enhanced.capabilities),
                    "output_format": enhanced.output_format,
                    "ambiguities": [dict(a) for a in enhanced.ambiguities]},
            quality=quality.to_dict() if hasattr(quality, "to_dict") else {},
            continuity=continuity.to_dict() if hasattr(continuity, "to_dict")
            else {},
            context_quality=(context_bundle.quality.to_dict()
                             if context_bundle is not None
                             and context_bundle.quality else {}),
            tool_plan=tool_plan or {},
            verification=verification or {},
            memory=memory_outcome or {"skipped":
                                      "no memory update for this action"},
            task_id=task_id, report=report,
            citations=tuple(citations), provenance=provenance,
            needs_clarification=needs_clarification,
            needs_confirmation=needs_confirmation)
        if self.audit is not None:
            try:
                self.audit("assistant", kind, {"ok": True,
                                               "trace": trace_id,
                                               "provenance": provenance})
            except Exception:
                pass
        return response
