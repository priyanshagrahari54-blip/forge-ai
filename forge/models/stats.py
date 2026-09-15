"""Routing statistics: measurement feeding back into model choice (A83).

``forge.models.telemetry`` already records what happened. Nothing turned it
into a routing signal, so a model that fails one capability in four could keep
being picked forever. This module closes that loop with a bounded, on-disk
summary per ``(model, capability)``:

attempts · successes · failures · success rate · failure rate · latency p50 /
p95 · total and mean tokens · cumulative cost · quality score.

The numbers are only used to **re-rank candidates the policy already deemed
usable** and to explain why. They never widen eligibility: a model the policy
forbids stays forbidden no matter how good its statistics look, because the
policy encodes constraints (privacy, licensing, capability gaps) that
statistics cannot override.
"""
from __future__ import annotations

import json
import math
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

DEFAULT_PATH = Path(".forge/model_stats.json")
MAX_ENTRIES = 200
#: Samples kept per (model, capability) for percentile work.
MAX_SAMPLES = 200


@dataclass
class CapabilityStats:
    """Measured behaviour of one model on one capability."""

    model: str
    capability: str
    attempts: int = 0
    successes: int = 0
    failures: int = 0
    timeouts: int = 0
    refused: int = 0
    latency_ms: List[float] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated_cost: float = 0.0
    #: Sum of quality scores and the count they came from (0..5 per score).
    quality_total: float = 0.0
    quality_count: int = 0
    updated_at: float = 0.0

    # -- derived ---------------------------------------------------------

    @property
    def success_rate(self) -> float:
        return round(self.successes / self.attempts, 4) if self.attempts else 0.0

    @property
    def failure_rate(self) -> float:
        return round((self.failures + self.timeouts) / self.attempts, 4) \
            if self.attempts else 0.0

    @property
    def mean_latency_ms(self) -> float:
        return round(sum(self.latency_ms) / len(self.latency_ms), 1) \
            if self.latency_ms else 0.0

    def percentile_latency_ms(self, percentile: float) -> float:
        if not self.latency_ms:
            return 0.0
        ordered = sorted(self.latency_ms)
        index = int(math.ceil(percentile / 100.0 * len(ordered))) - 1
        return round(ordered[max(0, min(len(ordered) - 1, index))], 1)

    @property
    def p50_latency_ms(self) -> float:
        return self.percentile_latency_ms(50)

    @property
    def p95_latency_ms(self) -> float:
        return self.percentile_latency_ms(95)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def mean_tokens(self) -> float:
        return round(self.total_tokens / self.attempts, 1) if self.attempts \
            else 0.0

    @property
    def mean_quality(self) -> float:
        return round(self.quality_total / self.quality_count, 3) \
            if self.quality_count else 0.0

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["success_rate"] = self.success_rate
        data["failure_rate"] = self.failure_rate
        data["p50_latency_ms"] = self.p50_latency_ms
        data["p95_latency_ms"] = self.p95_latency_ms
        data["total_tokens"] = self.total_tokens
        data["mean_tokens"] = self.mean_tokens
        data["mean_quality"] = self.mean_quality
        data.pop("latency_ms")
        data["samples"] = len(self.latency_ms)
        return data


class StatsStore:
    """Bounded, thread-safe routing statistics for one repository."""

    def __init__(self, root: str | Path = ".", path: str | Path | None = None,
                 *, persist: bool = True) -> None:
        self.root = Path(root).resolve()
        self.path = self.root / Path(path if path is not None
                                     else DEFAULT_PATH)
        self.persist = persist
        self._entries: Dict[Tuple[str, str], CapabilityStats] = {}
        self._lock = threading.RLock()
        self._loaded = False

    # -- persistence -----------------------------------------------------

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, OSError):
            return
        if not isinstance(payload, dict):
            return
        for item in payload.get("entries", []):
            if not isinstance(item, dict):
                continue
            model = str(item.get("model", "") or "")
            capability = str(item.get("capability", "") or "")
            if not model or not capability:
                continue
            entry = CapabilityStats(model=model, capability=capability)
            for name in ("attempts", "successes", "failures", "timeouts",
                         "refused", "prompt_tokens", "completion_tokens",
                         "quality_count"):
                value = item.get(name)
                if isinstance(value, int):
                    setattr(entry, name, max(0, value))
            for name in ("estimated_cost", "quality_total", "updated_at"):
                value = item.get(name)
                if isinstance(value, (int, float)):
                    setattr(entry, name, float(value))
            samples = item.get("latency_samples")
            if isinstance(samples, list):
                entry.latency_ms = [float(value) for value in samples
                                    if isinstance(value, (int, float))][
                    -MAX_SAMPLES:]
            self._entries[(model, capability)] = entry

    def _save(self) -> None:
        if not self.persist:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            entries = sorted(
                self._entries.values(),
                key=lambda item: (-item.attempts, item.model,
                                  item.capability))[:MAX_ENTRIES]
            payload = {"version": 1, "entries": [
                {**{key: value for key, value in item.to_dict().items()
                    if key != "samples"},
                 "latency_samples": item.latency_ms[-MAX_SAMPLES:]}
                for item in entries]}
            self.path.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8")
        except OSError:
            # Statistics are an optimisation; never let them break a run.
            pass

    # -- recording -------------------------------------------------------

    def record(self, feedback: Any, *, quality: float = 0.0) -> None:
        """Record one :class:`~forge.models.fabric.RouterFeedback`.

        The classification of an attempt comes from the real fields on that
        dataclass — ``success``, plus the ``error`` text when it failed — so
        a timeout and a refusal are counted as what they were rather than
        lumped into one "failure" bucket.
        """
        with self._lock:
            self._load()
            model = str(getattr(feedback, "model", "") or "")
            capability = str(getattr(feedback, "capability", "") or "")
            if not model or not capability:
                return
            entry = self._entries.setdefault(
                (model, capability),
                CapabilityStats(model=model, capability=capability))
            entry.attempts += 1
            if getattr(feedback, "success", False):
                entry.successes += 1
            else:
                error = str(getattr(feedback, "error", "") or "").lower()
                if "timeout" in error or "timed out" in error:
                    entry.timeouts += 1
                elif "refus" in error or "policy" in error:
                    entry.refused += 1
                else:
                    entry.failures += 1
            latency = getattr(feedback, "latency_ms", None)
            if isinstance(latency, (int, float)) and latency > 0:
                entry.latency_ms.append(float(latency))
                entry.latency_ms = entry.latency_ms[-MAX_SAMPLES:]
            prompt = getattr(feedback, "input_tokens", 0) or 0
            completion = getattr(feedback, "output_tokens", 0) or 0
            if isinstance(prompt, (int, float)):
                entry.prompt_tokens += int(prompt)
            if isinstance(completion, (int, float)):
                entry.completion_tokens += int(completion)
            if quality > 0:
                entry.quality_total += float(quality)
                entry.quality_count += 1
            entry.updated_at = float(
                getattr(feedback, "timestamp", 0.0) or 0.0)
            if len(self._entries) > MAX_ENTRIES:
                # Drop the least-used entries rather than growing forever.
                keep = sorted(
                    self._entries.values(),
                    key=lambda item: (-item.attempts, item.model,
                                      item.capability))[:MAX_ENTRIES]
                self._entries = {(item.model, item.capability): item
                                 for item in keep}
            self._save()

    # -- reading ---------------------------------------------------------

    def get(self, model: str, capability: str) -> Optional[CapabilityStats]:
        with self._lock:
            self._load()
            return self._entries.get((str(model), str(capability)))

    def snapshot(self) -> List[Dict[str, Any]]:
        with self._lock:
            self._load()
            return [entry.to_dict()
                    for entry in sorted(
                        self._entries.values(),
                        key=lambda item: (-item.attempts, item.model,
                                          item.capability))]

    def score(self, model: str, capability: str, *,
              min_attempts: int = 3) -> Tuple[float, str]:
        """Reliability score in ``[0, 1]`` with the reason for it.

        With fewer than ``min_attempts`` observations the model has not earned
        a reputation, so it scores a neutral ``1.0`` and says why — a new or
        rarely used model must not be penalised for having no data.
        """
        entry = self.get(model, capability)
        if entry is None or entry.attempts < min_attempts:
            observed = 0 if entry is None else entry.attempts
            return 1.0, "only %d observation(s); too few to rank" % observed
        base = 1.0 - entry.failure_rate
        # Refusals are cheap but still waste a round trip.
        if entry.attempts:
            base -= 0.5 * (entry.refused / entry.attempts)
        if entry.quality_count:
            # 0..5 quality mapped onto 0.8..1.0 so quality can nudge, not
            # dominate: reliability matters more than a reviewer's score.
            base *= 0.8 + 0.2 * min(1.0, max(0.0, entry.mean_quality / 5.0))
        return round(max(0.0, min(1.0, base)), 4), (
            "%d attempts, %.0f%% success, p95 %.0f ms, mean quality %.2f"
            % (entry.attempts, entry.success_rate * 100,
               entry.p95_latency_ms, entry.mean_quality))


def rerank(decision: Any, store: StatsStore, *, capability: str = "",
           max_penalty: float = 0.5) -> Any:
    """Re-rank a :class:`~forge.models.fabric.RouteDecision` by reliability.

    ``RouteDecision.candidates`` is an ordered tuple of model *names*, and the
    decision's own ``model`` is the one the router picked. This reorders that
    tuple by measured reliability and records the evidence in ``factors``.

    Two limits are deliberate:

    * it never widens eligibility — only the order of names the router
      already produced changes, so a model the policy forbade stays absent;
    * it never silently swaps ``decision.model``. The router owns model
      construction and its fallback ladder; this function only reports which
      candidate measured best, via ``factors["reliability_preferred"]``. A
      caller that wants to act on that has to do so explicitly, which keeps
      the choice attributable.
    """
    names = [str(item) for item in
             (getattr(decision, "candidates", ()) or ())]
    if len(names) < 2:
        return decision
    target = str(capability or "")
    if not target:
        model = getattr(decision, "model", None)
        target = str(getattr(model, "capability", "") or "")
    scored: List[Tuple[float, int, str, str]] = []
    for index, name in enumerate(names):
        score, reason = store.score(name, target)
        penalty = max(0.0, min(max_penalty, 1.0 - score))
        scored.append((score - penalty, index, name, reason))
    scored.sort(key=lambda item: (-item[0], item[1]))
    ordered = [item[2] for item in scored]
    if ordered != names:
        decision.candidates = tuple(ordered)
        decision.factors["reranked_by_reliability"] = 1.0
    for _, _, name, reason in scored:
        score, _ = store.score(name, target)
        decision.factors["reliability:%s" % name] = score
        decision.factors["reliability_evidence:%s" % name] = reason
    decision.factors["reliability_preferred"] = ordered[0]
    return decision


def render(stats: Sequence[Dict[str, Any]], *, limit: int = 20) -> str:
    """A compact table for the CLI."""
    if not stats:
        return "no model routing statistics recorded yet"
    lines = ["%-24s %-14s %6s %7s %7s %9s %9s %7s" % (
        "model", "capability", "att", "succ", "fail", "p50 ms", "p95 ms",
        "quality")]
    for item in stats[:limit]:
        lines.append("%-24s %-14s %6d %6.0f%% %6.0f%% %9.1f %9.1f %7s" % (
            item.get("model", "")[:24], item.get("capability", "")[:14],
            item.get("attempts", 0),
            item.get("success_rate", 0.0) * 100,
            item.get("failure_rate", 0.0) * 100,
            item.get("p50_latency_ms", 0.0),
            item.get("p95_latency_ms", 0.0),
            ("%.2f" % item["mean_quality"]) if item.get("quality_count")
            else "—"))
    return "\n".join(lines)
