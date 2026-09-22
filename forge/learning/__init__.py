"""Learning layers (A84 Stage I).

Forge improves behaviour through *controlled evidence*, never by training
model weights from conversations. The layers here are ledgers + readers:

* :mod:`operational` — which tools work, which routes fail, latency,
  provider reliability, task success rates, error patterns (I1);
* :mod:`preferences` — user-preference observations that only ever produce
  *proposed* profile updates; applying one is an explicit user/gate action
  (I2, and C3's control);
* :class:`forge.prompt_intelligence.strategies.PromptStrategyLedger` —
  prompt-strategy outcomes feed enhancement emphasis (I3);
* :mod:`routing` — bounded routing priors for model/specialist/tool/fallback
  selection (I4) that can nudge *preference scores* only — never security,
  authorization, capability requirements or explicit user choices.

Existing layers stay authoritative and are reused, not replaced:
failure fingerprints remain in :class:`forge.learning.failures.FailureLedger`
(A59), model outcomes keep flowing through the fabric's telemetry/feedback,
and agent self-improvement stays inside the A58 loop. These modules add the
cross-cutting operational view without duplicating those stores.
"""
from forge.learning.operational import OperationalLedger
from forge.learning.preferences import PreferenceObserver
from forge.learning.routing import RoutingPriors

__all__ = ["OperationalLedger", "PreferenceObserver", "RoutingPriors"]
