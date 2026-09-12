"""LOCAL / SERVER / HYBRID execution selector (A81, requirement 5).

The router is a **pure, deterministic function** of:

- the execution mode setting (LOCAL | SERVER | HYBRID);
- policy (``LocalPolicy.allow_local`` / ``allow_server``);
- capability (what the lightweight client may do: inspection only);
- model availability (a model-needing task is never local — the G560
  loads no model);
- resource availability (RAM floor, CPU floor);
- task size (requirement chars vs the configured cap);
- server reachability (last known server health).

The same inputs always yield the same decision, and every decision
records a human-readable ``reason`` that the desktop shows in the
decision log. SECURITY: a LOCAL decision only ever authorizes the
bounded :mod:`forge.client.local_exec` operations — never the pipeline —
and the *server* re-authorizes everything server-side anyway.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from forge.client.estimator import KIND_INSPECT, TaskEstimate, estimate
from forge.client.resources import ResourceSnapshot


@dataclass(frozen=True)
class ServerStatus:
    """Last-known server facts used by HYBRID decisions."""
    reachable: bool
    model_ready: bool = False
    workers: int = 0


@dataclass(frozen=True)
class ExecutionDecision:
    execution: str                 # "LOCAL" | "SERVER"
    reason: str
    estimate: TaskEstimate
    resources: Optional[ResourceSnapshot] = None
    details: dict = field(default_factory=dict)

    @property
    def is_local(self) -> bool:
        return self.execution == "LOCAL"


def decide(mode: str, requirement: str, policy, server: ServerStatus,
           resources: Optional[ResourceSnapshot]) -> ExecutionDecision:
    """Select LOCAL vs SERVER. Raises nothing; refusals are decisions.

    Ordering (documented, deterministic):
      1. mode SERVER  -> SERVER (policy may refuse: no path).
      2. mode LOCAL   -> LOCAL only if every gate passes, else refusal
         recorded as a failed decision (never silently escalate).
      3. mode HYBRID  -> LOCAL iff (policy, capability, model, size,
         resources, server not required) all allow; else SERVER.
    """
    estimate_result = estimate(requirement)
    mode = (mode or "hybrid").strip().lower()

    if mode == "server":
        if not policy.allow_server:
            return ExecutionDecision(
                "SERVER", "refused: policy disallows server execution "
                "(allow_server=false); task not submitted",
                estimate_result, resources)
        return ExecutionDecision(
            "SERVER", "mode=server", estimate_result, resources,
            {"model_ready": server.model_ready})

    if mode == "local":
        ok, reason = _local_gates(estimate_result, policy, resources,
                                  require_light=True)
        if ok:
            return ExecutionDecision("LOCAL", reason, estimate_result,
                                     resources)
        return ExecutionDecision(
            "SERVER" if (policy.allow_server and server.reachable)
            else "LOCAL",
            reason + ("; LOCAL refused, no usable fallback configured"
                      if not (policy.allow_server and server.reachable)
                      else ""),
            estimate_result, resources)

    # HYBRID (default): automatic selection.
    if not policy.allow_server:
        ok, reason = _local_gates(estimate_result, policy, resources,
                                  require_light=True)
        return ExecutionDecision(
            "LOCAL" if ok else "LOCAL",
            reason if ok else reason + " (allow_server=false; cannot "
            "delegate — task refused)",
            estimate_result, resources)
    if not server.reachable:
        ok, reason = _local_gates(estimate_result, policy, resources,
                                  require_light=True)
        if ok:
            return ExecutionDecision("LOCAL", "server unreachable; "
                                     "light task allowed locally: "
                                     + reason, estimate_result, resources)
        return ExecutionDecision(
            "SERVER", "server unreachable and task not allowed locally: "
            + reason + " (will fail until the server returns)",
            estimate_result, resources)
    ok, reason = _local_gates(estimate_result, policy, resources,
                              require_light=True)
    if ok:
        return ExecutionDecision("LOCAL", reason, estimate_result,
                                 resources)
    return ExecutionDecision("SERVER", reason, estimate_result, resources,
                             {"model_ready": server.model_ready,
                              "workers": server.workers})


def _local_gates(est: TaskEstimate, policy, resources: Optional[
        ResourceSnapshot], *, require_light: bool) -> tuple[bool, str]:
    """All LOCAL gates in fixed order; first failure wins (recorded)."""
    if not policy.allow_local:
        return False, "policy disallows local execution (allow_local=false)"
    if est.needs_model:
        return False, ("task requires a model (client loads no model) "
                       f"[signals: {','.join(est.signals[:4]) or 'default'}]"
                       )
    if est.kind != KIND_INSPECT:
        return False, "task is not a light inspection task"
    if est.chars > policy.max_task_chars:
        return False, (f"task too large for local execution "
                       f"({est.chars} > {policy.max_task_chars} chars)")
    if resources is None:
        return False, "no resource snapshot available (fail closed)"
    if not resources.adequate(min_free_ram_mb=policy.min_free_ram_mb):
        return False, (f"insufficient local resources "
                       f"(free {resources.free_ram_mb}MB < "
                       f"{policy.min_free_ram_mb}MB, "
                       f"cpus={resources.cpus})")
    return True, (f"light local task ({est.chars} chars, "
                  f"free {resources.free_ram_mb}MB)")
