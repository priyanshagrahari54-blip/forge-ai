"""Reasoning backend interface: stable, vendor-neutral, replaceable.

The engine never calls a model vendor directly. It asks a :class:`ReasoningHub`
to ``respond(request)`` and the hub selects a backend:

1. :class:`NativeDeterministicBackend` — always available, uses **no** neural
   inference. It performs structured reasoning the engine can trust
   (classification, plan refinement against repository evidence, failure
   categorization, verification transformation) and *refuses* the
   generative kinds (``REPAIR``/``GENERATE``/``NARRATE``) with
   ``NEURAL_REQUIRED``. It never emits fabricated code or prose pretending to
   be model output: deterministic code is not a language model, and this
   module is where that boundary is enforced.
2. :class:`LocalNeuralBackend` / :class:`RemoteNeuralBackend` — interfaces,
   not implementations of vendors. Both wrap *one* abstraction: the existing
   Model Fabric (:class:`forge.models.fabric.ModelFabric`). A local backend
   prefers local/free providers (small local model, a future Forge-trained
   model registered as a provider); a remote backend prefers configured
   remote providers. They activate only when a *real* (non-fallback) model is
   registered — ``local-fallback`` never counts as neural, so the engine can
   never claim a model it does not have.

Selection rules (deterministic, explained on request):

* Structural kinds (UNDERSTAND/REFINE_PLAN/SELECT_TARGETS/DIAGNOSE/REVIEW)
  run on the deterministic backend; neural answers may be attached as
  clearly-labeled ``suggestions`` only.
* Generative kinds (REPAIR/GENERATE/NARRATE) need a neural backend. With
  none available, the refusal is returned honestly and the engine records
  the affected step as ``skipped_no_model``.
* A neural failure is reported as a failure (``BACKEND_ERROR`` /
  ``INVALID_MODEL_OUTPUT``). It never silently "falls back" to a fabricated
  answer.

Every result carries provenance: ``neural`` flag, backend name, model/provider
when a real model answered, and measured latency.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from time import perf_counter
from typing import Any, Dict, List, Optional, Protocol, Tuple

from forge.models.capabilities import normalize_capability
from forge.models.request import ModelRequest


class ReasoningKind(str, Enum):
    """The reasoning operations the engine can request."""

    UNDERSTAND = "understand"
    REFINE_PLAN = "refine_plan"
    SELECT_TARGETS = "select_targets"
    DIAGNOSE = "diagnose"
    REVIEW = "review"
    REPAIR = "repair"
    GENERATE = "generate"
    NARRATE = "narrate"

    @classmethod
    def generative(cls) -> "frozenset[ReasoningKind]":
        """Kinds whose output is *content* — only a neural backend may do
        these; refusing is the correct behavior without a model."""
        return frozenset({cls.REPAIR, cls.GENERATE, cls.NARRATE})


class RefusalCode(str, Enum):
    NEURAL_REQUIRED = "NEURAL_REQUIRED"
    BACKEND_ERROR = "BACKEND_ERROR"
    INVALID_MODEL_OUTPUT = "INVALID_MODEL_OUTPUT"
    NOT_CONFIGURED = "NOT_CONFIGURED"


@dataclass
class ReasoningRequest:
    """A structured reasoning request. ``context`` is rendered text already
    produced by the context engine (budgeted, fingerprinted)."""

    kind: ReasoningKind
    task: str
    context: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    #: Optional hard bound on model output size (characters) for tiny RAM.
    max_output_chars: int = 6000
    capability: str = "coding"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": ReasoningKind(self.kind).value,
            "task": self.task[:400],
            "context_chars": len(self.context or ""),
            "payload_keys": sorted((self.payload or {}).keys()),
            "capability": self.capability,
        }


@dataclass
class ReasoningResult:
    """One backend answer. ``ok=False`` always carries a refusal code."""

    ok: bool
    kind: str
    backend: str
    neural: bool
    data: Dict[str, Any] = field(default_factory=dict)
    refusal: str = ""
    message: str = ""
    model: str = ""
    provider: str = ""
    latency_ms: float = 0.0
    #: Provenance label for every consumer (UI/report/dataset).
    generated_by: str = ""

    def __post_init__(self) -> None:
        if not self.generated_by:
            self.generated_by = self.backend

    @classmethod
    def refused(cls, kind: ReasoningKind, backend: str, code: RefusalCode,
                 message: str, data: Optional[Dict[str, Any]] = None
                 ) -> "ReasoningResult":
        return cls(False, ReasoningKind(kind).value, backend, False,
                   data=dict(data or {}), refusal=RefusalCode(code).value,
                   message=message)

    @classmethod
    def produced(cls, kind: ReasoningKind, backend: str, neural: bool,
                 data: Dict[str, Any], *, model: str = "", provider: str = "",
                 latency_ms: float = 0.0) -> "ReasoningResult":
        return cls(True, ReasoningKind(kind).value, backend, neural,
                   data=dict(data), model=model, provider=provider,
                   latency_ms=latency_ms,
                   generated_by=("%s(%s)" % (backend, model)
                                 if neural and model else backend))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "kind": self.kind,
            "backend": self.backend,
            "neural": self.neural,
            "refusal": self.refusal,
            "message": self.message,
            "model": self.model,
            "provider": self.provider,
            "latency_ms": self.latency_ms,
            "generated_by": self.generated_by,
            "data": self.data,
        }


class ReasoningBackend(Protocol):
    """The stable interface every reasoning backend implements."""

    name: str
    kind: str  # "deterministic" | "local-neural" | "remote-neural"
    is_neural: bool

    def available(self) -> Tuple[bool, str]:
        """``(available, detail)`` — detail explains unavailability."""
        ...

    def responds_to(self, kind: ReasoningKind) -> bool:
        ...

    def respond(self, request: ReasoningRequest) -> ReasoningResult:
        ...


class ReasoningRefusal(Exception):
    """Raised by callers that cannot continue without the refused answer."""

    def __init__(self, code: RefusalCode, message: str) -> None:
        super().__init__(message)
        self.code = RefusalCode(code).value
        self.message = message

    def to_dict(self) -> Dict[str, Any]:
        return {"refusal": self.code, "message": self.message}


# ---------------------------------------------------------------------------
# Deterministic backend
# ---------------------------------------------------------------------------

#: Structured failure categories produced by the deterministic backend.
_FAILURE_RULES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("syntax", ("SyntaxError", "IndentationError", "TabError",
                "unexpected indent", "invalid syntax")),
    ("import", ("ModuleNotFoundError", "ImportError", "cannot import name")),
    ("collection", ("collected 0 items", "no tests ran",
                    "ERROR collecting", "FixModule import error")),
    ("assertion", ("assert", "AssertionError", "assertion")),
    ("timeout", ("TimeoutExpired", "timeout", "timed out")),
    ("fixture", ("fixture ", "error at teardown", "error at setup")),
)


class NativeDeterministicBackend:
    """Forge's own structured reasoning — no model, no fabrication.

    What it *does*: verb/repository classification (via the planner), plan
    refinement grounded in repository intelligence, failure categorization,
    and turning measured verification results into findings.
    What it *refuses*: producing code, repairs, or explanatory prose — those
    are generative kinds and are refused with ``NEURAL_REQUIRED``.
    """

    name = "native-deterministic"
    kind = "deterministic"
    is_neural = False

    def available(self) -> Tuple[bool, str]:
        return True, "always available; requires no model and no network"

    def responds_to(self, kind: ReasoningKind) -> bool:
        return ReasoningKind(kind) not in ReasoningKind.generative()

    def respond(self, request: ReasoningRequest) -> ReasoningResult:
        kind = ReasoningKind(request.kind)
        if kind in ReasoningKind.generative():
            return ReasoningResult.refused(
                kind, self.name, RefusalCode.NEURAL_REQUIRED,
                "%s requires neural inference; the deterministic backend "
                "refuses to fabricate content. Configure a local or remote "
                "model backend (see `forge native-ai status`)."
                % kind.value)
        handler = getattr(self, "_handle_" + kind.value, None)
        if handler is None:  # pragma: no cover - guards interface drift
            return ReasoningResult.refused(
                kind, self.name, RefusalCode.NOT_CONFIGURED,
                "no deterministic handler for reasoning kind %r"
                % kind.value)
        try:
            data = handler(request)
        except Exception as exc:
            return ReasoningResult(False, kind.value, self.name, False,
                                   refusal=RefusalCode.BACKEND_ERROR.value,
                                   message=str(exc))
        return ReasoningResult.produced(kind, self.name, False, data)

    # -- structured handlers ---------------------------------------------------

    @staticmethod
    def _handle_understand(request: ReasoningRequest) -> Dict[str, Any]:
        from forge.native.planner import NativePlanner
        intelligence = request.payload.get("intelligence")
        planner = NativePlanner(intelligence)
        classification = planner.classify(request.task)
        return {"classification": classification.to_dict()}

    @staticmethod
    def _handle_refine_plan(request: ReasoningRequest) -> Dict[str, Any]:
        from forge.native.planner import NativePlanner
        intelligence = request.payload.get("intelligence")
        planner = NativePlanner(intelligence)
        plan = planner.plan(request.task, intelligence)
        return {"plan": plan.to_dict(), "valid": not plan.validate()}

    @staticmethod
    def _handle_select_targets(request: ReasoningRequest) -> Dict[str, Any]:
        """Rank repository files for the task using existing relevance data.

        Deterministic scoring from the context engine's selection (already
        intelligence-driven); this handler only reshapes it for the report.
        """
        candidates = request.payload.get("candidates") or []
        ranked: List[Dict[str, Any]] = []
        for item in candidates:
            if isinstance(item, dict) and item.get("path"):
                ranked.append({"path": item["path"],
                               "score": float(item.get("score", 0.0)),
                               "reason": item.get("reason", "")})
        ranked.sort(key=lambda entry: (-entry["score"], entry["path"]))
        return {"targets": ranked[:20]}

    @staticmethod
    def _handle_diagnose(request: ReasoningRequest) -> Dict[str, Any]:
        output = str(request.payload.get("failure_output") or "")
        category = "unknown"
        evidence: List[str] = []
        lowered = output.lower()
        for name, markers in _FAILURE_RULES:
            for marker in markers:
                if marker.lower() in lowered:
                    category = name
                    evidence = [line for line in output.splitlines()
                                if marker.lower() in line.lower()][:5]
                    break
            if category != "unknown":
                break
        return {
            "category": category,
            "evidence_lines": evidence,
            "tail": output[-1200:],
            "method": "deterministic output pattern rules",
        }

    @staticmethod
    def _handle_review(request: ReasoningRequest) -> Dict[str, Any]:
        """Turn measured verification results into findings (no invention)."""
        verification = request.payload.get("verification") or {}
        findings: List[Dict[str, Any]] = []
        for gate in verification.get("gates", []):
            if not isinstance(gate, dict):
                continue
            if gate.get("executed") and not gate.get("passed"):
                findings.append({
                    "severity": "HIGH",
                    "source": "verification-gate",
                    "gate": gate.get("name", "?"),
                    "detail": str(gate.get("details", ""))[:800],
                })
        return {
            "findings": findings,
            "verdict": "REQUEST_CHANGES" if findings else "APPROVE",
            "note": "deterministic review over executed verification gates",
        }


# ---------------------------------------------------------------------------
# Neural backends (interfaces over the Model Fabric)
# ---------------------------------------------------------------------------

_REPAIR_CONTRACT = (
    "Return ONLY a JSON object: "
    '{"explanation": str, "changes": {"<repo-relative/path>": "<full new '
    'file content>"}}. Use the smallest safe change. Never modify tests to '
    "hide failures. Paths must be relative to the repository root; never "
    "reference .git/, .forge/, or absolute paths."
)
_GENERATE_CONTRACT = (
    "Return ONLY a JSON object: "
    '{"summary": str, "changes": {"<repo-relative/path>": "<full new file '
    'content>"}}. Every path relative to the repository root. If the task '
    "cannot be done safely with the given context, return "
    '{"summary": str, "changes": {}} and explain in "summary".'
)


class FabricNeuralBackend:
    """One model interface for all neural reasoning, routed by the Fabric.

    The fabric is the single abstraction (A81 layer 11): this backend never
    imports a vendor SDK; providers are whatever the host fabric registered
    (Ollama, OpenAI-compatible, a future small local model, or a future
    Forge-trained model). Availability is honest: a fabric whose only model is
    the deterministic placeholder reports *unavailable*, and calls that fail
    produce failures, never fabricated success.
    """

    is_neural = True

    def __init__(self, fabric: Any, *, name: str = "fabric-neural",
                 kind: str = "local-neural", prefer_local: bool = True,
                 prefer_free: bool = True, model_data_policy: Any = None,
                 probe_on_select: bool = False) -> None:
        self.fabric = fabric
        self.name = name
        self.kind = kind
        self.prefer_local = bool(prefer_local)
        self.prefer_free = bool(prefer_free)
        self.model_data_policy = model_data_policy
        #: When True, ``available()`` runs the network-probing readiness
        #: check. Off by default: state checks must never block a 2 GB
        #: laptop; generation attempts learn availability by trying.
        self.probe_on_select = bool(probe_on_select)

    # -- availability ----------------------------------------------------------

    def available(self) -> Tuple[bool, str]:
        if self.fabric is None:
            return False, "no Model Fabric attached"
        from forge.models.readiness import (
            check_fabric_readiness,
            fabric_has_real_model,
        )
        try:
            if not fabric_has_real_model(self.fabric):
                return False, ("no real (non-fallback) model registered in "
                               "the Model Fabric")
        except Exception as exc:
            return False, "fabric inspection failed: %s" % exc
        if self.probe_on_select:
            try:
                report = check_fabric_readiness(self.fabric,
                                                 probe_network=True,
                                                 timeout=5.0)
            except Exception as exc:
                return False, "readiness probe failed: %s" % exc
            if not report.ready:
                reasons = "; ".join(
                    check.detail for check in report.failures[:3])
                return False, "models registered but unreachable: %s" % (
                    reasons or "probe reported not-ready")
        return True, "real model registered in the Model Fabric"

    def responds_to(self, kind: ReasoningKind) -> bool:
        return True  # neural backends can answer every kind

    # -- generation ------------------------------------------------------------

    def respond(self, request: ReasoningRequest) -> ReasoningResult:
        kind = ReasoningKind(request.kind)
        if self.fabric is None:
            return ReasoningResult.refused(
                kind, self.name, RefusalCode.NOT_CONFIGURED,
                "no Model Fabric attached to this backend")
        prompt = self._prompt(kind, request)
        model_request = ModelRequest(
            prompt=prompt,
            capability=normalize_capability(request.capability)
            or "coding",
            context=(request.context or "")[:16000],
            task=(request.task or "")[:4000],
            prefer_local=self.prefer_local,
            prefer_free=self.prefer_free,
            metadata=(
                {"model_data_policy": self.model_data_policy}
                if self.model_data_policy is not None else {}),
        )
        started = perf_counter()
        try:
            response = self.fabric.generate(model_request)
        except Exception as exc:
            return ReasoningResult(False, kind.value, self.name, True,
                                   refusal=RefusalCode.BACKEND_ERROR.value,
                                   message="fabric call raised: %s" % exc,
                                   latency_ms=(perf_counter() - started)
                                   * 1000.0)
        latency_ms = (perf_counter() - started) * 1000.0
        if not getattr(response, "success", False):
            return ReasoningResult(False, kind.value, self.name, True,
                                   refusal=RefusalCode.BACKEND_ERROR.value,
                                   message=str(getattr(response, "error", "")
                                               or "model call failed"),
                                   model=str(getattr(response, "model", "")),
                                   provider=str(
                                       getattr(response, "provider", "")),
                                   latency_ms=latency_ms)
        # Routing may fall through to the deterministic placeholder when a
        # registered real model is unreachable mid-call. A placeholder answer
        # is NOT a model answer: report it as NEURAL_REQUIRED so the engine
        # refuses honestly instead of parsing an empty "changes" payload.
        from forge.models.readiness import is_fallback_response
        try:
            used_fallback = is_fallback_response(
                self.fabric, str(getattr(response, "model", "") or ""),
                str(getattr(response, "provider", "") or ""))
        except Exception:
            used_fallback = False
        if used_fallback:
            return ReasoningResult.refused(
                kind, self.name, RefusalCode.NEURAL_REQUIRED,
                "no real model answered (the deterministic placeholder is "
                "not a model and does not generate content); configure a "
                "reachable local or remote model (`forge doctor` explains "
                "what is missing)",
                data={"fallback_refusal": True})
        text = str(getattr(response, "text", "") or "")
        if len(text) > request.max_output_chars:
            text = text[:request.max_output_chars]
        return self._interpret(kind, request, text, response, latency_ms)

    # -- interpretation ----------------------------------------------------------

    def _prompt(self, kind: ReasoningKind,
                request: ReasoningRequest) -> str:
        if kind == ReasoningKind.REPAIR:
            failure = str(request.payload.get("failure_output")
                          or "")[-4000:]
            context = str(request.payload.get("failure_context") or "")
            head = ("Diagnose and fix this failing test result. "
                    + _REPAIR_CONTRACT)
            return "\n".join(filter(None, [
                head, "TASK: " + request.task[:2000],
                "FAILURE:\n" + failure, "FAILURE CONTEXT:\n" + context]))
        if kind == ReasoningKind.GENERATE:
            return "\n".join([
                "Implement the task as a small, safe change set. "
                + _GENERATE_CONTRACT,
                "TASK: " + request.task[:2000],
            ])
        if kind == ReasoningKind.NARRATE:
            return ("Summarize this engineering result in plain language for "
                    "an operator (2-4 sentences, no invented facts):\n\n"
                    + json.dumps(request.payload, sort_keys=True,
                                 default=str)[:6000])
        return "\n".join([
            "Analyze the task against the repository context. Return ONLY a "
            "JSON object {\"assessment\": str, \"targets\": [str]} where "
            "targets lists repo-relative files you consider relevant.",
            "TASK: " + request.task[:2000],
        ])

    def _interpret(self, kind: ReasoningKind, request: ReasoningRequest,
                   text: str, response: Any,
                   latency_ms: float) -> ReasoningResult:
        model = str(getattr(response, "model", "") or "")
        provider = str(getattr(response, "provider", "") or "")
        meta = {"model": model, "provider": provider,
                "latency_ms": round(latency_ms, 1)}
        if kind == ReasoningKind.NARRATE:
            return ReasoningResult.produced(
                kind, self.name, True,
                {"narration": text[:2000], **meta},
                model=model, provider=provider, latency_ms=latency_ms)
        payload = _parse_json_object(text)
        if payload is None:
            return ReasoningResult(
                False, kind.value, self.name, True,
                refusal=RefusalCode.INVALID_MODEL_OUTPUT.value,
                message="model output was not a parseable JSON object",
                data={"raw_excerpt": text[:800], **meta},
                model=model, provider=provider, latency_ms=latency_ms)
        if kind in (ReasoningKind.REPAIR, ReasoningKind.GENERATE):
            changes = payload.get("changes")
            if not isinstance(changes, dict):
                return ReasoningResult(
                    False, kind.value, self.name, True,
                    refusal=RefusalCode.INVALID_MODEL_OUTPUT.value,
                    message='model JSON lacked a "changes" object',
                    data={"raw_excerpt": text[:800], **meta},
                    model=model, provider=provider, latency_ms=latency_ms)
            safe_changes: Dict[str, str] = {}
            for path, content in changes.items():
                if isinstance(path, str) and isinstance(content, str):
                    safe_changes[path] = content
            return ReasoningResult.produced(
                kind, self.name, True,
                {"changes": safe_changes,
                 "explanation": str(payload.get("explanation")
                                    or payload.get("summary") or ""),
                 "raw_text": text[:8000],
                 **meta},
                model=model, provider=provider, latency_ms=latency_ms)
        return ReasoningResult.produced(
            kind, self.name, True, {"model_response": payload, **meta},
            model=model, provider=provider, latency_ms=latency_ms)


class LocalNeuralBackend(FabricNeuralBackend):
    """Local neural inference (small local model on the G560-class host, or
    a Forge-trained model exposed as a provider)."""

    def __init__(self, fabric: Any, **kwargs: Any) -> None:
        kwargs.setdefault("name", "local-neural")
        kwargs.setdefault("kind", "local-neural")
        kwargs.setdefault("prefer_local", True)
        kwargs.setdefault("prefer_free", True)
        super().__init__(fabric, **kwargs)


class RemoteNeuralBackend(FabricNeuralBackend):
    """Remote neural inference interface over the same Model Fabric
    abstraction (e.g. an OpenAI-compatible endpoint registered in Forge).
    Strictly optional; nothing in the engine depends on it."""

    def __init__(self, fabric: Any, **kwargs: Any) -> None:
        kwargs.setdefault("name", "remote-neural")
        kwargs.setdefault("kind", "remote-neural")
        kwargs.setdefault("prefer_local", False)
        kwargs.setdefault("prefer_free", False)
        super().__init__(fabric, **kwargs)


# ---------------------------------------------------------------------------
# Hub
# ---------------------------------------------------------------------------

def _parse_json_object(text: str) -> Optional[Dict[str, Any]]:
    """Best-effort extraction of a JSON object from model output.

    Uses the coder's tolerant loader (fenced/balanced-brace handling) so one
    parsing rule governs model output everywhere in Forge.
    """
    from forge.agents.coder import loads_model_json
    try:
        payload, _repaired = loads_model_json(text)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


class ReasoningHub:
    """Backend registry + deterministic selection policy.

    Order is fixed and explainable: for generative kinds, local neural first
    (free-first on the target hardware), then remote neural; structural kinds
    always run on the deterministic backend and — only when
    ``use_neural_suggestions`` is enabled and a neural backend answers —
    attach the model's take as labeled suggestions. No hidden fallback: if a
    selected neural backend fails, the failure is returned.
    """

    def __init__(self, backends: List[ReasoningBackend],
                 use_neural_suggestions: bool = False) -> None:
        self.backends: List[ReasoningBackend] = list(backends)
        self.use_neural_suggestions = bool(use_neural_suggestions)
        #: Selection log for observability (last 32 decisions).
        self.decisions: List[Dict[str, Any]] = []

    # -- management -----------------------------------------------------------

    def register(self, backend: ReasoningBackend) -> None:
        for existing in self.backends:
            if getattr(existing, "name", "") == getattr(backend, "name", ""):
                raise ValueError("duplicate reasoning backend name: %s"
                                 % backend.name)
        self.backends.append(backend)

    def backend(self, name: str) -> Optional[ReasoningBackend]:
        for candidate in self.backends:
            if getattr(candidate, "name", "") == name:
                return candidate
        return None

    # -- selection ----------------------------------------------------------------

    def _first_available(self, predicate, note_all: bool = False
                         ) -> Tuple[Optional[ReasoningBackend], List[str]]:
        notes: List[str] = []
        for candidate in self.backends:
            if not predicate(candidate):
                if note_all:
                    notes.append("%s: does not handle these kinds (%s)"
                                 % (candidate.name,
                                    "deterministic refuses to fabricate "
                                    "generative content"
                                    if not candidate.is_neural else
                                    "wrong scope"))
                continue
            try:
                ok, detail = candidate.available()
            except Exception as exc:  # availability must never explode
                ok, detail = False, "availability check failed: %s" % exc
            notes.append("%s: %s" % (candidate.name, detail))
            if ok:
                return candidate, notes
        return None, notes

    def select(self, kind: ReasoningKind) -> Tuple[Optional[ReasoningBackend],
                                                    Dict[str, Any]]:
        kind = ReasoningKind(kind)
        generative = kind in ReasoningKind.generative()
        if generative:
            chosen, notes = self._first_available(
                lambda b: getattr(b, "is_neural", False), note_all=True)
            explanation = {
                "kind": kind.value,
                "policy": "generative kinds require a neural backend "
                          "(local preferred, then remote)",
                "chosen": getattr(chosen, "name", "") if chosen else "",
                "notes": notes,
            }
            return chosen, explanation
        chosen, notes = self._first_available(
            lambda b: not getattr(b, "is_neural", False))
        if chosen is None:  # deterministic backend should always exist
            chosen, _ = self._first_available(lambda b: True)
        neural, neural_notes = self._first_available(
            lambda b: getattr(b, "is_neural", False))
        explanation = {
            "kind": kind.value,
            "policy": "structural kinds run deterministically; neural "
                      "answers are labeled suggestions",
            "chosen": getattr(chosen, "name", "") if chosen else "",
            "neural_available": neural is not None,
            "notes": notes + neural_notes,
        }
        return chosen, explanation

    # -- execution ------------------------------------------------------------

    def respond(self, request: ReasoningRequest) -> ReasoningResult:
        kind = ReasoningKind(request.kind)
        backend, explanation = self.select(kind)
        self.decisions.append({"selection": explanation})
        del self.decisions[:-32]
        if backend is None:
            return ReasoningResult.refused(
                kind, "none", RefusalCode.NEURAL_REQUIRED,
                "no reasoning backend is available: "
                + "; ".join(explanation.get("notes", [])))
        if not backend.responds_to(kind):
            return ReasoningResult.refused(
                kind, backend.name, RefusalCode.NEURAL_REQUIRED,
                "backend %r does not handle %r" % (backend.name, kind.value))
        result = backend.respond(request)
        if (not result.ok and result.refusal
                == RefusalCode.NEURAL_REQUIRED.value):
            # A non-neural backend that somehow accepted a generative kind
            # still cannot fake it — re-assert the refusal at the hub.
            result.data.setdefault("selection", explanation)
            return result
        if (self.use_neural_suggestions
                and kind not in ReasoningKind.generative()
                and result.ok):
            suggestion = self._suggestion(kind, request)
            if suggestion is not None:
                result.data = dict(result.data)
                result.data["neural_suggestion"] = suggestion
        if result.ok:
            result.data = dict(result.data)
            result.data.setdefault("selection", explanation)
        return result

    def _suggestion(self, kind: ReasoningKind,
                    request: ReasoningRequest) -> Optional[Dict[str, Any]]:
        """Ask the neural backend for its take; failures never affect the
        deterministic answer (the suggestion is advisory and labeled)."""
        neural, _notes = self._first_available(
            lambda b: getattr(b, "is_neural", False))
        if neural is None or not neural.responds_to(kind):
            return None
        try:
            answer = neural.respond(request)
        except Exception as exc:  # suggestions must never break the run
            return {"ok": False, "error": str(exc)[:300]}
        if not answer.ok:
            return {"ok": False, "refusal": answer.refusal,
                    "message": answer.message[:300]}
        return {"ok": True, "data": answer.data,
                "model": answer.model, "provider": answer.provider}

    # -- status -------------------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        """Report the two backend roles separately — never conflate them.

        ``active_backend`` is the *structural* engine (deterministic:
        planning, classification, diagnosis) and is always the native one
        when present. ``generative_backend`` is the backend code/repair
        requests route to (only ever a neural one, only when one is
        registered); ``None`` means generative steps will be refused with
        ``NEURAL_REQUIRED``. Registration is reported as registration:
        whether the endpoint actually answers is learned from real calls,
        never asserted here.
        """
        entries: List[Dict[str, Any]] = []
        structural: Optional[Dict[str, Any]] = None
        generative: Optional[Dict[str, Any]] = None
        for candidate in self.backends:
            try:
                ok, detail = candidate.available()
            except Exception as exc:
                ok, detail = False, str(exc)
            entry = {
                "name": candidate.name,
                "kind": candidate.kind,
                "neural": bool(candidate.is_neural),
                "available": bool(ok),
                "detail": detail,
            }
            entries.append(entry)
            if not ok:
                continue
            if not candidate.is_neural and structural is None:
                structural = {"name": candidate.name, "neural": False,
                              "role": "structural",
                              "detail": "planning, classification, and "
                                        "diagnosis run deterministically"}
            if candidate.is_neural and generative is None:
                generative = {"name": candidate.name, "neural": True,
                              "role": "generative",
                              "detail": detail,
                              "verification": "unverified until a real "
                                               "call succeeds"}
        return {"backends": entries,
                "active_backend": structural or (generative or {}),
                "generative_backend": generative,
                "generative_ready": generative is not None}
