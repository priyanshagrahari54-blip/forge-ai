"""The policy-aware routing engine (Session 11).

This upgrades the fabric's capability router into a routing *engine*: it
consumes the Session 10 resource governor, the A33 data policy, the network
policy, model verification state, and quality/latency evidence, and it returns
a decision that explains itself.

Inputs (``RoutingRequest``)
    capability, task type, complexity, context size, latency target, resource
    budget, privacy classification, network policy, cost budget, model
    availability, model quality, hardware profile.

Outputs (``RoutingPlan``)
    ``selected_model``, ``selected_backend``, ``reason``, ``fallback_path``,
    ``policy_result``, ``resource_result`` — plus every candidate that was
    rejected and why.

Rules that are enforced here, not documented:

* An unavailable or unverified model is never selected as though it were ready.
* Capability requirements are never relaxed (a vision request is never sent to
  a text-only model).
* ``SECRET`` content never reaches an external provider; ``CONFIDENTIAL``
  needs an explicit policy allowance.
* A profile that denies local model loading (``g560``) removes local
  candidates outright — the thin client never becomes an inference machine.
* A network policy of ``off``/``server-only`` removes remote candidates.
* Free-first stays the default posture: a paid provider is only chosen when
  the policy allows paid *and* nothing free can serve.
* The engine restricts; it never grants. It cannot loosen an A33 verdict.

Python floor: 3.8 (Windows 7 reference target). Stdlib only.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import (Any, Dict, List, Optional, Sequence, Tuple)

from forge.models.fallback import (FallbackLadder, FallbackPlan,
                                   TerminalState)
from forge.models.identity import (AvailabilityState, ModelIdentity,
                                   VerificationState)

__all__ = [
    "PolicyCheck",
    "PolicyResult",
    "ResourceCheck",
    "ResourceResult",
    "RoutingEngine",
    "RoutingPlan",
    "RoutingRequest",
    "RoutingState",
]

#: Bound on how much text is inspected when a caller did not classify.
CLASSIFY_CHAR_BOUND = 8192


class RoutingState:
    """Terminal vocabulary of a routing decision (plain strings)."""

    ROUTED = "routed"
    NEEDS_MODEL = TerminalState.NEEDS_MODEL.value
    POLICY_DENIED = TerminalState.POLICY_DENIED.value
    RESOURCE_DENIED = TerminalState.RESOURCE_DENIED.value
    UNVERIFIED = TerminalState.UNVERIFIED.value
    DETERMINISTIC = "deterministic"


@dataclass
class PolicyCheck:
    name: str
    allowed: bool
    detail: str = ""
    #: ``allow`` | ``deny`` | ``require_approval``
    decision: str = "allow"

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "allowed": bool(self.allowed),
                "decision": self.decision, "detail": self.detail[:400]}


@dataclass
class PolicyResult:
    allowed: bool = True
    decision: str = "allow"
    reason: str = ""
    data_classification: str = ""
    network_policy: str = ""
    checks: List[PolicyCheck] = field(default_factory=list)

    def deny(self, name: str, detail: str, *,
             decision: str = "deny") -> "PolicyResult":
        self.checks.append(PolicyCheck(name, False, detail, decision))
        self.allowed = False
        self.decision = decision
        if not self.reason:
            self.reason = "%s: %s" % (name, detail)
        return self

    def allow(self, name: str, detail: str = "") -> "PolicyResult":
        self.checks.append(PolicyCheck(name, True, detail, "allow"))
        return self

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowed": bool(self.allowed),
            "decision": self.decision,
            "reason": self.reason[:400],
            "data_classification": self.data_classification,
            "network_policy": self.network_policy,
            "checks": [check.to_dict() for check in self.checks],
        }


@dataclass
class ResourceCheck:
    name: str
    ok: bool
    detail: str = ""
    value: Any = None
    limit: Any = None

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "ok": bool(self.ok),
                "detail": self.detail[:400], "value": self.value,
                "limit": self.limit}


@dataclass
class ResourceResult:
    allowed: bool = True
    reason: str = ""
    profile: str = ""
    device_class: str = ""
    model_loading_allowed: bool = True
    network_policy: str = ""
    checks: List[ResourceCheck] = field(default_factory=list)

    def deny(self, name: str, detail: str, *, value: Any = None,
             limit: Any = None) -> "ResourceResult":
        self.checks.append(ResourceCheck(name, False, detail, value, limit))
        self.allowed = False
        if not self.reason:
            self.reason = "%s: %s" % (name, detail)
        return self

    def ok(self, name: str, detail: str = "", *, value: Any = None,
           limit: Any = None) -> "ResourceResult":
        self.checks.append(ResourceCheck(name, True, detail, value, limit))
        return self

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowed": bool(self.allowed),
            "reason": self.reason[:400],
            "profile": self.profile,
            "device_class": self.device_class,
            "model_loading_allowed": bool(self.model_loading_allowed),
            "network_policy": self.network_policy,
            "checks": [check.to_dict() for check in self.checks],
        }


@dataclass
class RoutingRequest:
    """Everything the engine is allowed to consider."""

    capability: str = ""
    required_capabilities: Tuple[str, ...] = ()
    task_type: str = ""
    complexity: float = 1.0
    #: Estimated context size in tokens (0 = unknown).
    context_size: int = 0
    latency_target_ms: Optional[float] = None
    max_output_tokens: Optional[int] = None
    timeout: Optional[float] = None
    #: Explicit selection; the engine still refuses it when unsafe.
    model: str = ""
    backend: str = ""
    #: ``public`` | ``internal`` | ``confidential`` | ``secret`` ("" = derive).
    classification: str = ""
    #: Text used to derive a classification when none was declared.
    prompt: str = ""
    context_text: str = ""
    network_policy: str = ""
    cost_budget_usd: Optional[float] = None
    max_cost_per_token: Optional[float] = None
    prefer_free: Optional[bool] = None
    prefer_local: Optional[bool] = None
    resource_budget: Dict[str, Any] = field(default_factory=dict)
    hardware_profile: str = ""
    #: Require a verified model (default). Turning it off is explicit and is
    #: recorded in the plan.
    require_verified: bool = True
    #: Allow the deterministic non-neural rung when no model can serve.
    allow_deterministic: bool = True
    task_id: str = ""
    attempt_id: str = ""
    trace_id: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def effective_capabilities(self) -> Tuple[str, ...]:
        if self.required_capabilities:
            return tuple(self.required_capabilities)
        if self.capability:
            return (self.capability,)
        return ()

    def to_dict(self) -> Dict[str, Any]:
        """Metadata only: never the prompt or context text."""
        return {
            "capability": self.capability,
            "required_capabilities": list(self.effective_capabilities()),
            "task_type": self.task_type,
            "complexity": float(self.complexity or 1.0),
            "context_size": int(self.context_size or 0),
            "latency_target_ms": self.latency_target_ms,
            "max_output_tokens": self.max_output_tokens,
            "timeout": self.timeout,
            "model": self.model,
            "backend": self.backend,
            "classification": self.classification,
            "prompt_chars": len(self.prompt or ""),
            "context_chars": len(self.context_text or ""),
            "network_policy": self.network_policy,
            "cost_budget_usd": self.cost_budget_usd,
            "max_cost_per_token": self.max_cost_per_token,
            "prefer_free": self.prefer_free,
            "prefer_local": self.prefer_local,
            "resource_budget": dict(self.resource_budget),
            "hardware_profile": self.hardware_profile,
            "require_verified": bool(self.require_verified),
            "allow_deterministic": bool(self.allow_deterministic),
            "task_id": self.task_id,
            "attempt_id": self.attempt_id,
            "trace_id": self.trace_id,
        }


@dataclass
class RoutingPlan:
    """An explainable routing decision."""

    selected_model: Optional[ModelIdentity] = None
    selected_backend: str = ""
    reason: str = ""
    reasons: Tuple[str, ...] = ()
    fallback_path: Tuple[str, ...] = ()
    ladder: Optional[FallbackPlan] = None
    policy_result: PolicyResult = field(default_factory=PolicyResult)
    resource_result: ResourceResult = field(default_factory=ResourceResult)
    state: str = RoutingState.NEEDS_MODEL
    error_code: str = ""
    score: float = 0.0
    factors: Dict[str, Any] = field(default_factory=dict)
    #: Every candidate considered, with its verdict (explainability).
    considered: List[Dict[str, Any]] = field(default_factory=list)
    rejected: List[Dict[str, Any]] = field(default_factory=list)
    #: Bounds the engine applied (clamped timeout/output/context).
    bounds: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    latency_ms: float = 0.0

    @property
    def chosen(self) -> bool:
        return self.selected_model is not None

    @property
    def selected_model_id(self) -> str:
        return self.selected_model.model_id if self.selected_model else ""

    @property
    def deterministic(self) -> bool:
        return bool(self.selected_model is not None
                    and not self.selected_model.metadata.get("neural", True))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "selected_model": self.selected_model_id,
            "selected_backend": self.selected_backend,
            "reason": self.reason[:600],
            "reasons": [item[:300] for item in self.reasons],
            "fallback_path": list(self.fallback_path),
            "ladder": self.ladder.to_dict() if self.ladder else None,
            "policy_result": self.policy_result.to_dict(),
            "resource_result": self.resource_result.to_dict(),
            "state": self.state,
            "error_code": self.error_code,
            "score": round(float(self.score or 0.0), 4),
            "factors": dict(self.factors),
            "considered": [dict(item) for item in self.considered],
            "rejected": [dict(item) for item in self.rejected],
            "bounds": dict(self.bounds),
            "latency_ms": round(float(self.latency_ms or 0.0), 3),
            "created_at": self.created_at,
        }


class RoutingEngine:
    """Policy- and resource-aware selection over a model catalog."""

    def __init__(self, catalog: Any = None, *, governor: Any = None,
                 policy: Any = None, telemetry: Any = None,
                 data_policy: Any = None,
                 ladder: Optional[FallbackLadder] = None,
                 identities: Optional[Sequence[ModelIdentity]] = None,
                 deterministic_identity: Optional[ModelIdentity] = None,
                 clock: Any = time.perf_counter) -> None:
        self.catalog = catalog
        self.governor = governor
        #: The existing fabric routing policy (cost/free/local posture).
        self.policy = policy
        self.telemetry = telemetry
        #: The A33 model data policy (``ModelDataPolicy``); optional.
        self.data_policy = data_policy
        self.ladder = ladder
        self._identities = list(identities or ())
        self.deterministic_identity = deterministic_identity
        self._clock = clock
        self.history: List[Dict[str, Any]] = []

    # -- candidate sources ------------------------------------------------

    def identities(self) -> List[ModelIdentity]:
        """All identities the engine may consider (catalog first)."""
        found: List[ModelIdentity] = []
        if self.catalog is not None:
            try:
                found.extend(self.catalog.list())
            except Exception:
                found = []
        if not found:
            found.extend(self._identities)
        return found

    def set_policy(self, policy: Any) -> None:
        self.policy = policy

    def set_governor(self, governor: Any) -> None:
        self.governor = governor

    def set_data_policy(self, data_policy: Any) -> None:
        self.data_policy = data_policy

    # -- routing ---------------------------------------------------------

    def route(self, request: RoutingRequest) -> RoutingPlan:
        started = self._clock()
        plan = RoutingPlan()
        policy = self.policy
        prefer_free = _policy_flag(policy, "prefer_free", True)
        prefer_local = _policy_flag(policy, "prefer_local", True)
        if request.prefer_free is not None:
            prefer_free = bool(request.prefer_free)
        if request.prefer_local is not None:
            prefer_local = bool(request.prefer_local)
        allow_remote = _policy_flag(policy, "allow_remote", True)
        allow_paid = _policy_flag(policy, "allow_paid", True)

        # -- environment: profile, governor, network policy ---------------
        environment = self._environment(request)
        plan.resource_result.profile = environment["profile"]
        plan.resource_result.device_class = environment["device_class"]
        plan.resource_result.model_loading_allowed = \
            environment["model_loading_allowed"]
        network_policy = (request.network_policy
                          or environment["network_policy"])
        plan.policy_result.network_policy = network_policy
        plan.resource_result.network_policy = network_policy

        # -- privacy classification --------------------------------------
        classification = self._classify(request)
        plan.policy_result.data_classification = classification

        # -- global (request-level) policy checks -------------------------
        global_policy = PolicyResult(network_policy=network_policy,
                                     data_classification=classification)
        self._check_network_policy(global_policy, network_policy,
                                   allow_remote=allow_remote)
        self._check_cost_budget(global_policy, request, environment)
        plan.policy_result = global_policy

        # -- bounds the engine applies ------------------------------------
        bounds = self._bounds(request, environment)
        plan.bounds = bounds

        # -- candidate evaluation -----------------------------------------
        required = request.effective_capabilities()
        candidates: List[ModelIdentity] = []
        scored: List[Tuple[float, ModelIdentity, ResourceResult]] = []
        reasons: List[str] = []

        for identity in self.identities():
            verdict = self._evaluate(
                identity, request, required=required,
                classification=classification, network_policy=network_policy,
                allow_remote=allow_remote, allow_paid=allow_paid,
                prefer_free=prefer_free, prefer_local=prefer_local,
                environment=environment, bounds=bounds, policy=policy)
            record = verdict["record"]
            plan.considered.append(record)
            if not verdict["ok"]:
                plan.rejected.append(record)
                reasons.append("%s rejected (%s)"
                               % (identity.model_id, record["reason"]))
                if verdict.get("denial") == "resource" \
                        and plan.state == RoutingState.NEEDS_MODEL:
                    plan.resource_result = verdict["resource"]
                elif verdict.get("denial") == "policy" \
                        and not plan.policy_result.allowed:
                    pass
                continue
            candidates.append(identity)
            scored.append((verdict["score"], identity, verdict["resource"]))

        # Explicit selection wins only when it is safe.
        if request.model:
            explicit = [entry for entry in candidates
                        if _matches(entry, request.model)]
            if not explicit:
                wanted = [identity for identity in self.identities()
                          if _matches(identity, request.model)]
                why = ("unknown model %r" % request.model) if not wanted else \
                    ("model %r was rejected: %s"
                     % (request.model,
                        next((item["reason"] for item in plan.rejected
                              if item["model_id"] == wanted[0].model_id),
                             "not selectable")))
                return self._fail(plan, request, started,
                                  RoutingState.NEEDS_MODEL if not wanted
                                  else _state_for_rejection(plan, wanted[0]),
                                  "EXPLICIT_MODEL_REFUSED", why, reasons)
            scored = [(score + 1.0, identity, resource)
                      for score, identity, resource in scored
                      if _matches(identity, request.model)]
            reasons.append("explicit model %r requested and accepted"
                           % request.model)

        if request.backend:
            filtered = [(score, identity, resource)
                        for score, identity, resource in scored
                        if identity.backend_id == request.backend]
            if not filtered:
                return self._fail(
                    plan, request, started, RoutingState.NEEDS_MODEL,
                    "EXPLICIT_BACKEND_REFUSED",
                    "no selectable model is served by backend %r"
                    % request.backend, reasons)
            scored = filtered
            reasons.append("explicit backend %r requested and accepted"
                           % request.backend)

        if not scored:
            state, code, why = self._no_candidate(request, required,
                                                  plan, reasons)
            self._align_plan_verdicts(plan, state, why)
            ladder = self._build_ladder([], request, note=why)
            plan.ladder = ladder
            plan.fallback_path = tuple(step.model_id for step in ladder.steps)
            if ladder.steps and request.allow_deterministic \
                    and self.deterministic_identity is not None:
                plan.selected_model = self.deterministic_identity
                plan.selected_backend = "deterministic"
                plan.state = RoutingState.DETERMINISTIC
                plan.reason = ("no model can serve this request (%s); "
                               "falling back to the deterministic non-neural "
                               "strategy, which does not synthesize model "
                               "output" % why)
                plan.error_code = code
                plan.latency_ms = (self._clock() - started) * 1000.0
                self._record(plan, request)
                return plan
            return self._fail(plan, request, started, state, code, why,
                              reasons)

        scored.sort(key=lambda entry: (-entry[0],
                                       0 if entry[1].free else 1,
                                       0 if entry[1].local else 1,
                                       entry[1].model_id))
        score, chosen, resource = scored[0]
        plan.selected_model = chosen
        plan.selected_backend = chosen.backend_id
        plan.score = score
        plan.resource_result = resource
        plan.state = RoutingState.ROUTED
        plan.factors = self._factors(chosen, request, score)
        reasons.append("selected %s on backend %s (score=%.4f)"
                       % (chosen.model_id, chosen.backend_id, score))
        plan.reasons = tuple(reasons[-12:])
        plan.reason = self._summarize(chosen, request, resource, score)
        ladder = self._build_ladder(
            [identity for _s, identity, _r in scored], request,
            preferred_model_id=chosen.model_id)
        plan.ladder = ladder
        plan.fallback_path = tuple(step.model_id for step in ladder.steps)
        plan.latency_ms = (self._clock() - started) * 1000.0
        self._record(plan, request)
        return plan

    # -- evaluation of one candidate --------------------------------------

    def _evaluate(self, identity: ModelIdentity, request: RoutingRequest, *,
                  required: Tuple[str, ...], classification: str,
                  network_policy: str, allow_remote: bool, allow_paid: bool,
                  prefer_free: bool, prefer_local: bool,
                  environment: Dict[str, Any], bounds: Dict[str, Any],
                  policy: Any) -> Dict[str, Any]:
        record: Dict[str, Any] = {
            "model_id": identity.model_id,
            "backend_id": identity.backend_id,
            "availability_state": identity.availability_state,
            "verification_state": identity.verification_state,
            "local": bool(identity.local),
            "free": bool(identity.free),
            "ok": False,
            "reason": "",
            "denial": "",
        }
        resource = ResourceResult(
            profile=environment["profile"],
            device_class=environment["device_class"],
            model_loading_allowed=environment["model_loading_allowed"],
            network_policy=network_policy)

        def reject(reason: str, *, denial: str = "") -> Dict[str, Any]:
            record["reason"] = reason
            record["denial"] = denial
            record["resource"] = resource
            record["score"] = 0.0
            return {"ok": False, "record": record, "resource": resource,
                    "score": 0.0, "denial": denial}

        # 1. capability — never relaxed.
        missing = [item for item in required if not identity.supports(item)]
        if missing:
            return reject("missing capabilities %s" % ", ".join(missing))

        #: Absolute policy (2, 3) has already run: what follows is about the
        #: identity's own state, not about whether this request may use it.
        if identity.availability_state == AvailabilityState.POLICY_DENIED.value:
            return reject("policy denied", denial="policy")
        if identity.availability_state == AvailabilityState.RESOURCE_DENIED.value:
            return reject("resource denied", denial="resource")
        # 2. data policy (A33): SECRET never leaves the machine. Absolute:
        #: it is checked before verification, because a candidate that
        #: policy forbids must never be reported as merely "unverified" —
        #: that would tell an operator to prove a model Forge may not use.
        if classification:
            verdict = self._data_policy_verdict(classification, identity,
                                                request)
            if verdict != "allow":
                record["policy_decision"] = verdict
                return reject("%s data may not reach %s (%s)"
                              % (classification,
                                 "a local model" if identity.local
                                 else "an external provider", verdict),
                              denial="policy")

        # 3. network policy (and the device profile's local-loading rule).
        if not identity.local:
            if not allow_remote:
                return reject("routing policy does not allow remote providers",
                              denial="policy")
            if network_policy == "off":
                return reject("network policy is 'off'", denial="policy")
            if network_policy == "server-only" and not _is_server_endpoint(
                    identity, environment):
                return reject("network policy is 'server-only' and this "
                              "provider is not the configured Forge Server",
                              denial="policy")
        else:
            if not environment["model_loading_allowed"]:
                reason = ("the %r device profile denies local model loading"
                          % environment["profile"])
                #: Record the denial on the candidate's own verdict: a
                #: resource refusal that reports ``allowed=True`` would
                #: contradict the state it produced.
                resource.deny("model_loading", reason,
                              value=str(environment["profile"]),
                              limit="no local model loading")
                return reject(reason, denial="resource")

        # 4. availability / verification.
        if not identity.usable:
            detail = ("availability_state=%s is not selectable"
                      % identity.availability_state)
            #: States that mean "no real check has proven this model": the
            #: refusal is a verification refusal, and saying so points the
            #: operator at `forge models verify` instead of at a missing
            #: model. A fingerprint mismatch lands here too (FAILED).
            unproven = (AvailabilityState.DISCOVERED.value,
                        AvailabilityState.CONFIGURED.value,
                        AvailabilityState.UNVERIFIED.value,
                        AvailabilityState.FAILED.value)
            if identity.availability_state in unproven and not identity.verified:
                detail += (" (verification_state=%s: a model never becomes "
                           "ready without real verification)"
                           % identity.verification_state)
                return reject(detail, denial="verification")
            return reject(detail)
        if request.require_verified and not identity.verified:
            return reject("verification_state=%s (an unverified model is "
                          "never selected as ready)"
                          % identity.verification_state,
                          denial="verification")

        # 5. context fit — a real bound, not a preference.
        needed = max(int(request.context_size or 0),
                     int(bounds.get("context_tokens") or 0))
        if identity.context_limit and needed and \
                identity.context_limit < needed:
            return reject("context limit %d < required %d"
                          % (identity.context_limit, needed))

        # 6. cost posture (free-first).
        if not identity.free and not allow_paid:
            return reject("routing policy does not allow paid providers",
                          denial="policy")
        cap = request.max_cost_per_token
        if cap is None:
            cap = _policy_value(policy, "max_cost_per_token")
        if cap is not None and identity.cost_per_token > float(cap):
            return reject("cost per token %.8f exceeds the %.8f cap"
                          % (identity.cost_per_token, float(cap)),
                          denial="policy")

        # 7. latency target — a filter when stated, otherwise a score signal.
        latency_cap = _policy_value(policy, "max_latency_ms")
        target = request.latency_target_ms
        if target is not None and identity.latency_ms and \
                identity.latency_ms > float(target) * 4.0:
            # Only a gross miss filters; a close miss just scores lower, so a
            # slightly slow model is not discarded on stale evidence.
            return reject("observed latency %.0fms is far beyond the %.0fms "
                          "target" % (identity.latency_ms, float(target)))
        if latency_cap is not None and identity.latency_ms > float(latency_cap):
            record["latency_relaxed"] = True

        # 8. resource governor: model memory, RAM, concurrency, cost.
        denial = self._check_resources(identity, request, resource,
                                       environment, bounds)
        if not resource.allowed:
            return reject(resource.reason, denial=denial or "resource")

        score = self._score(identity, request, prefer_free=prefer_free,
                            prefer_local=prefer_local, bounds=bounds)
        record["ok"] = True
        record["score"] = round(score, 4)
        record["reason"] = "accepted"
        record["resource"] = resource
        return {"ok": True, "record": record, "resource": resource,
                "score": score, "denial": ""}

    # -- resource governor integration ------------------------------------

    def _check_resources(self, identity: ModelIdentity,
                         request: RoutingRequest, resource: ResourceResult,
                         environment: Dict[str, Any],
                         bounds: Dict[str, Any]) -> str:
        governor = self.governor
        size = int(identity.memory_requirements.resident_bytes
                   or identity.memory_requirements.weights_bytes
                   or identity.size_bytes or 0)

        # device profile / model loading
        if identity.local and not environment["model_loading_allowed"]:
            resource.deny("device_profile",
                          "profile %r denies local model loading"
                          % environment["profile"],
                          value=environment["profile"], limit="no local load")
            return "resource"
        resource.ok("device_profile",
                    "profile %r permits this model" % environment["profile"],
                    value=environment["profile"])

        if governor is not None:
            # model memory budget
            try:
                allowed, reason = governor.check_model_load(size)
            except Exception as exc:
                allowed, reason = False, "governor error: %s" % (exc,)
            if identity.local and not allowed:
                resource.deny("model_memory", reason, value=size,
                              limit=environment.get("model_memory_mb"))
                return "resource"
            resource.ok("model_memory", reason or "within budget", value=size,
                        limit=environment.get("model_memory_mb"))

            # RAM headroom (honest: unmeasurable is reported, not invented)
            available_mb = environment.get("memory_available_mb")
            required_mb = int(identity.memory_requirements.min_available_mb
                              or 0)
            if available_mb is None:
                resource.ok("ram", "available memory is not measurable on "
                                   "this platform", value=None)
            elif required_mb and available_mb < required_mb:
                resource.deny("ram",
                              "model needs %d MB available, %d MB measured"
                              % (required_mb, available_mb),
                              value=available_mb, limit=required_mb)
                return "resource"
            else:
                resource.ok("ram", "%s MB available" % available_mb,
                            value=available_mb, limit=required_mb or None)

            # concurrency / model slots
            usage = environment.get("usage") or {}
            now = int(usage.get("concurrency_now") or 0)
            capacity = int(usage.get("concurrency_capacity") or 0)
            requested_slots = int(
                request.resource_budget.get("concurrency_slots") or 1)
            if capacity and now + requested_slots > capacity:
                resource.deny("concurrency",
                              "%d of %d worker slots in use; %d requested"
                              % (now, capacity, requested_slots),
                              value=now, limit=capacity)
                return "resource"
            resource.ok("concurrency",
                        "%d/%d slots in use" % (now, capacity),
                        value=now, limit=capacity)

            # cost budget
            cap = float(environment.get("max_cost_usd") or 0.0)
            budget = request.cost_budget_usd
            spent = float(usage.get("cost_usd") or 0.0)
            effective = cap if budget is None else min(
                cap or float(budget), float(budget))
            if effective and not identity.free and spent >= effective:
                resource.deny("cost_budget",
                              "cost budget of $%.2f is exhausted (spent "
                              "$%.4f)" % (effective, spent),
                              value=spent, limit=effective)
                return "resource"
            if effective and identity.cost_per_token > 0.0 and \
                    (effective - spent) < identity.cost_per_token:
                #: Free-first, and honest about the arithmetic: if the
                #: remaining budget cannot cover a single token of this
                #: model, selecting it would promise a spend it cannot fund.
                #: We never estimate output length, so the check is exact.
                resource.deny("cost_budget",
                              "remaining budget $%.6f cannot cover one token "
                              "of this model ($%.6f/token)"
                              % (max(0.0, effective - spent),
                                 identity.cost_per_token),
                              value=spent, limit=effective)
                return "resource"
            if effective:
                resource.ok("cost_budget", "budget $%.2f, spent $%.4f"
                            % (effective, spent), value=spent,
                            limit=effective)

        # CPU / device profile of the backend
        cpu = environment.get("cpu_count")
        threads = int(identity.metadata.get("min_cpu_threads") or 0)
        if cpu and threads and cpu < threads:
            resource.deny("cpu", "model wants %d threads, %d CPU(s) present"
                          % (threads, cpu), value=cpu, limit=threads)
            return "resource"
        resource.ok("cpu", "%s CPU(s) available" % (cpu if cpu else "unknown"),
                    value=cpu, limit=threads or None)

        # output/timeout bounds are always applied (never a denial)
        resource.ok("output_limit",
                    "output bound %s tokens" % bounds.get("max_output_tokens"),
                    value=bounds.get("max_output_tokens"))
        resource.ok("timeout", "timeout bound %.1fs"
                    % float(bounds.get("timeout") or 0.0),
                    value=bounds.get("timeout"))
        return ""

    # -- scoring ----------------------------------------------------------

    def _score(self, identity: ModelIdentity, request: RoutingRequest, *,
               prefer_free: bool, prefer_local: bool,
               bounds: Dict[str, Any]) -> float:
        reliability = max(0.0, min(1.0, float(identity.reliability or 0.0)))
        quality = max(0.0, min(1.0, float(identity.quality or 0.0)))
        latency = 1.0 / (1.0 + max(0.0, float(identity.latency_ms or 0.0))
                         / 1000.0)
        cost = 1.0 / (1.0 + max(0.0, float(identity.cost_per_token or 0.0))
                      * 1_000_000.0)
        free = (1.0 if identity.free else 0.0) if prefer_free else 0.5
        local = (1.0 if identity.local else 0.0) if prefer_local else 0.5
        needed = max(int(request.context_size or 0),
                     int(bounds.get("context_tokens") or 0), 1)
        limit = int(identity.context_limit or 0)
        context_fit = min(1.0, limit / float(needed)) if limit else 0.5
        complexity = max(0.0, float(request.complexity or 1.0))
        complexity_fit = (min(1.0, limit / (4096.0 * complexity))
                          if limit else 0.3)
        health = {"ready": 1.0, "loaded": 0.95, "degraded": 0.5}.get(
            identity.availability_state, 0.7)
        # Latency target: a model well inside the target scores higher.
        target = request.latency_target_ms
        if target and identity.latency_ms:
            latency = min(latency, 1.0 / (1.0 + max(
                0.0, identity.latency_ms - float(target)) / 1000.0))
        return (reliability * 0.22
                + quality * 0.14
                + latency * 0.12
                + cost * 0.08
                + free * 0.10
                + local * 0.10
                + context_fit * 0.09
                + complexity_fit * 0.05
                + health * 0.10)

    def _factors(self, identity: ModelIdentity, request: RoutingRequest,
                 score: float) -> Dict[str, Any]:
        return {
            "score": round(float(score), 4),
            "reliability": identity.reliability,
            "quality": identity.quality,
            "latency_ms": identity.latency_ms,
            "cost_per_token": identity.cost_per_token,
            "free": identity.free,
            "local": identity.local,
            "context_limit": identity.context_limit,
            "availability_state": identity.availability_state,
            "verification_state": identity.verification_state,
            "capability": request.capability,
            "complexity": request.complexity,
            "task_type": request.task_type,
        }

    def _summarize(self, identity: ModelIdentity, request: RoutingRequest,
                   resource: ResourceResult, score: float) -> str:
        bits = [
            "model=%s" % identity.model_id,
            "backend=%s" % identity.backend_id,
            "capability=%s" % (request.capability or "-"),
            "state=%s" % identity.availability_state,
            "verification=%s" % identity.verification_state,
            "score=%.4f" % score,
            "local=%s" % identity.local,
            "free=%s" % identity.free,
            "profile=%s" % resource.profile,
        ]
        if resource.reason:
            bits.append("resource=%s" % resource.reason)
        return "; ".join(bits)

    # -- policy helpers ---------------------------------------------------

    def _data_policy_verdict(self, classification: str,
                             identity: ModelIdentity,
                             request: RoutingRequest) -> str:
        """``allow`` / ``deny`` / ``require_approval`` for one candidate."""
        level = str(classification or "").lower()
        if level == "secret" and not identity.local:
            # Never weakened: SECRET does not go to an external provider,
            # whatever the configured data policy says.
            return "deny"
        policy = self.data_policy
        if policy is None:
            if level == "confidential" and not identity.local:
                return "require_approval"
            return "allow"
        authorized = bool(request.metadata.get("data_authorized", False))
        try:
            decision = policy.evaluate(level, local=bool(identity.local),
                                       authorized=authorized)
        except Exception:
            return "deny"
        value = getattr(decision, "value", str(decision)).lower()
        if value in ("allow",):
            return "allow"
        if value in ("require_approval", "approval"):
            return "allow" if authorized else "require_approval"
        return "deny"

    def _classify(self, request: RoutingRequest) -> str:
        declared = str(request.classification or "").strip().lower()
        if declared in ("public", "internal", "confidential", "secret"):
            # A declaration still cannot launder detected secret material.
            try:
                from forge.security.classification import classify_text
                sample = ("%s\n%s" % (request.prompt or "",
                                      request.context_text or ""))
                sample = sample[:CLASSIFY_CHAR_BOUND]
                if sample.strip():
                    detected = classify_text(sample, declared=declared)
                    return getattr(detected, "value", declared)
            except Exception:
                pass
            return declared
        sample = ("%s\n%s" % (request.prompt or "",
                              request.context_text or ""))[:CLASSIFY_CHAR_BOUND]
        if not sample.strip():
            return ""
        try:
            from forge.security.classification import classify_text
            return getattr(classify_text(sample), "value", "internal")
        except Exception:
            return "internal"

    def _check_network_policy(self, result: PolicyResult, network_policy: str,
                              *, allow_remote: bool) -> None:
        if network_policy == "off":
            result.checks.append(PolicyCheck(
                "network_policy", allow_remote is False,
                "network policy is 'off': no outbound inference is permitted",
                "deny" if allow_remote else "allow"))
            if allow_remote:
                result.reason = result.reason or (
                    "network policy is 'off' but the routing policy allows "
                    "remote providers; remote candidates are refused")
            return
        result.allow("network_policy",
                     "network policy %r" % (network_policy or "unset"))

    def _check_cost_budget(self, result: PolicyResult,
                           request: RoutingRequest,
                           environment: Dict[str, Any]) -> None:
        cap = float(environment.get("max_cost_usd") or 0.0)
        if request.cost_budget_usd is not None:
            cap = request.cost_budget_usd if not cap else min(
                cap, float(request.cost_budget_usd))
        spent = float((environment.get("usage") or {}).get("cost_usd") or 0.0)
        if cap and spent >= cap:
            result.deny("cost_budget",
                        "cost budget of $%.2f is exhausted (spent $%.4f)"
                        % (cap, spent))
        elif cap:
            result.allow("cost_budget",
                         "budget $%.2f, spent $%.4f" % (cap, spent))
        else:
            result.allow("cost_budget",
                         "no explicit cost budget; free-first posture applies")

    # -- environment ------------------------------------------------------

    def _environment(self, request: RoutingRequest) -> Dict[str, Any]:
        governor = self.governor
        environment: Dict[str, Any] = {
            "profile": "default",
            "device_class": "unknown",
            "model_loading_allowed": True,
            "network_policy": "off",
            "model_memory_mb": 0,
            "max_cost_usd": 0.0,
            "memory_available_mb": None,
            "cpu_count": None,
            "usage": {},
            "max_task_wall_seconds": 0.0,
        }
        if governor is None:
            try:
                from forge.core.resource_governor import (
                    ResourceGovernor, select_profile)
                governor = ResourceGovernor(
                    select_profile(request.hardware_profile))
                self.governor = governor
            except Exception:
                return environment
        try:
            snapshot = governor.snapshot()
        except Exception:
            return environment
        profile = snapshot.get("profile") or {}
        resources = snapshot.get("resources") or {}
        environment.update({
            "profile": str(profile.get("name") or "default"),
            "device_class": "thin-client"
            if not profile.get("model_loading_allowed", True) else "general",
            "model_loading_allowed": bool(
                profile.get("model_loading_allowed", True)),
            "network_policy": str(profile.get("network_policy") or "off"),
            "model_memory_mb": int(profile.get("model_memory_mb") or 0),
            "max_cost_usd": float(profile.get("max_cost_usd") or 0.0),
            "max_task_wall_seconds": float(
                profile.get("max_task_wall_seconds") or 0.0),
            "memory_available_mb": resources.get("memory_available_mb"),
            "cpu_count": resources.get("cpu_count"),
            "usage": snapshot.get("usage") or {},
        })
        return environment

    def _bounds(self, request: RoutingRequest,
                environment: Dict[str, Any]) -> Dict[str, Any]:
        wall = float(environment.get("max_task_wall_seconds") or 0.0)
        timeout = request.timeout
        if timeout is None:
            timeout = wall or 120.0
        if wall:
            timeout = min(float(timeout), wall)
        timeout = max(0.5, float(timeout))
        output = request.max_output_tokens
        output = int(output) if output else 2048
        output = max(1, min(output, 32768))
        context_tokens = max(int(request.context_size or 0), 0)
        return {
            "timeout": round(timeout, 3),
            "timeout_clamped_by_profile": bool(
                wall and request.timeout and float(request.timeout) > wall),
            "max_output_tokens": output,
            "context_tokens": context_tokens,
        }

    # -- no-candidate diagnosis -------------------------------------------

    def _align_plan_verdicts(self, plan: "RoutingPlan", state: str,
                             why: str) -> None:
        """Make the plan-level verdicts agree with the refusal.

        A request refused by the resource governor must not report
        ``resource_result.allowed = True`` just because the device itself can
        run Forge, and a policy refusal must not read as "policy allowed".
        The per-candidate verdicts already exist; this lifts the relevant one
        to the plan so a reader (CLI, agent, evidence feed) sees one answer
        instead of two contradictory ones.
        """
        if state == RoutingState.RESOURCE_DENIED:
            source = next((item.get("resource") for item in plan.considered
                           if item.get("denial") == "resource"
                           and item.get("resource") is not None), None)
            if source is not None:
                plan.resource_result = source
            else:
                plan.resource_result.deny("resource", why or
                                          "refused by the resource governor")
        elif state == RoutingState.POLICY_DENIED and plan.policy_result.allowed:
            plan.policy_result.deny("routing", why or
                                    "refused by policy")

    def _no_candidate(self, request: RoutingRequest,
                      required: Tuple[str, ...], plan: RoutingPlan,
                      reasons: List[str]) -> Tuple[str, str, str]:
        considered = plan.considered
        if not considered:
            return (RoutingState.NEEDS_MODEL, "NO_MODEL",
                    "no model is registered for capability %s"
                    % (", ".join(required) or "<any>"))
        capability_misses = [item for item in considered
                             if "missing capabilities" in item["reason"]]
        if len(capability_misses) == len(considered):
            return (RoutingState.NEEDS_MODEL, "CAPABILITY_UNSUPPORTED",
                    "no registered model supports %s"
                    % (", ".join(required) or "<any>"))
        resource_denials = [item for item in considered
                            if item.get("denial") == "resource"]
        policy_denials = [item for item in considered
                          if item.get("denial") == "policy"]
        verification = [item for item in considered
                        if item.get("denial") == "verification"]
        #: A verification denial outranks the others, and that is not an
        #: accident of ordering: absolute policy (capability, A33 data policy,
        #: network policy, device profile) is evaluated *before* verification,
        #: so a candidate refused for verification is one this request was
        #: allowed to use and that simply has not been proven yet. Naming that
        #: is the actionable answer ("run `forge models verify`"); reporting a
        #: different candidate's policy denial would hide it.
        if verification:
            return (RoutingState.UNVERIFIED, "UNVERIFIED",
                    "no candidate is verified; run `forge models verify` "
                    "(configuration alone is never availability)")
        if resource_denials and len(resource_denials) == len(
                [item for item in considered if not item["ok"]]):
            return (RoutingState.RESOURCE_DENIED, "RESOURCE_DENIED",
                    "every candidate was refused by the resource governor "
                    "(%s)" % resource_denials[0]["reason"])
        if policy_denials and len(policy_denials) == len(
                [item for item in considered if not item["ok"]]):
            return (RoutingState.POLICY_DENIED, "POLICY_DENIED",
                    "every candidate was refused by policy (%s)"
                    % policy_denials[0]["reason"])
        if resource_denials:
            return (RoutingState.RESOURCE_DENIED, "RESOURCE_DENIED",
                    "no selectable model: %s" % resource_denials[0]["reason"])
        if policy_denials:
            return (RoutingState.POLICY_DENIED, "POLICY_DENIED",
                    "no selectable model: %s" % policy_denials[0]["reason"])
        first = next((item for item in considered if not item["ok"]), None)
        return (RoutingState.NEEDS_MODEL, "NO_MODEL",
                "no selectable model (%s)" % (first["reason"] if first
                                              else "none available"))

    def _build_ladder(self, candidates: Sequence[ModelIdentity],
                      request: RoutingRequest, *,
                      preferred_model_id: str = "",
                      note: str = "") -> FallbackPlan:
        policy = self.policy
        ladder = self.ladder or FallbackLadder(
            allow_remote=_policy_flag(policy, "allow_remote", True),
            allow_paid=_policy_flag(policy, "allow_paid", True),
            require_verified=request.require_verified)
        ladder.allow_remote = _policy_flag(policy, "allow_remote", True)
        ladder.allow_paid = _policy_flag(policy, "allow_paid", True)
        ladder.require_verified = bool(request.require_verified)
        return ladder.build(candidates, preferred_model_id=preferred_model_id,
                            deterministic_model_id=(
                                self.deterministic_identity.model_id
                                if self.deterministic_identity else
                                "deterministic:local-fallback"),
                            note=note)

    def _fail(self, plan: RoutingPlan, request: RoutingRequest, started: float,
              state: str, code: str, reason: str,
              reasons: List[str]) -> RoutingPlan:
        plan.state = state
        plan.error_code = code
        plan.reason = reason
        plan.selected_model = None
        plan.selected_backend = ""
        reasons.append("refused: %s" % reason)
        plan.reasons = tuple(reasons[-12:])
        if not plan.policy_result.allowed:
            plan.policy_result.reason = plan.policy_result.reason or reason
        plan.latency_ms = (self._clock() - started) * 1000.0
        self._record(plan, request)
        return plan

    def _record(self, plan: RoutingPlan, request: RoutingRequest) -> None:
        event = {
            "model": plan.selected_model_id,
            "backend": plan.selected_backend,
            "state": plan.state,
            "capability": request.capability,
            "score": round(float(plan.score or 0.0), 4),
            "fallback_path": list(plan.fallback_path),
            "policy_allowed": plan.policy_result.allowed,
            "resource_allowed": plan.resource_result.allowed,
            "classification": plan.policy_result.data_classification,
            "profile": plan.resource_result.profile,
            "considered": len(plan.considered),
            "rejected": len(plan.rejected),
            "error_code": plan.error_code,
            "latency_ms": round(float(plan.latency_ms or 0.0), 3),
            "task_id": request.task_id,
            "trace_id": request.trace_id,
        }
        self.history.append(event)
        if len(self.history) > 200:
            self.history = self.history[-200:]
        if self.telemetry is not None:
            try:
                self.telemetry.record("routing", **event)
            except Exception:
                pass


def _matches(identity: ModelIdentity, wanted: str) -> bool:
    target = (wanted or "").strip()
    if not target:
        return False
    return target in (identity.model_id, identity.name)


def _policy_flag(policy: Any, name: str, default: bool) -> bool:
    value = getattr(policy, name, None)
    return default if value is None else bool(value)


def _policy_value(policy: Any, name: str) -> Any:
    return getattr(policy, name, None)


def _is_server_endpoint(identity: ModelIdentity,
                        environment: Dict[str, Any]) -> bool:
    """True when a remote identity is the operator's configured Forge Server."""
    endpoint = str(identity.metadata.get("server_endpoint") or "").strip()
    if not endpoint:
        return False
    allowed = str(environment.get("server_endpoint") or "").strip()
    if not allowed:
        # ``server-only`` without a configured server permits nothing remote.
        return False
    return endpoint.rstrip("/") == allowed.rstrip("/")


def _state_for_rejection(plan: RoutingPlan,
                         identity: ModelIdentity) -> str:
    for item in plan.rejected:
        if item["model_id"] != identity.model_id:
            continue
        denial = item.get("denial")
        if denial == "resource":
            return RoutingState.RESOURCE_DENIED
        if denial == "policy":
            return RoutingState.POLICY_DENIED
        if denial == "verification":
            return RoutingState.UNVERIFIED
    return RoutingState.NEEDS_MODEL
