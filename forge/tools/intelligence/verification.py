"""Tool result verification (A84 Stage E3).

After a tool runs, its result is *checked*, not trusted:

* **malformed output** — declared output schema keys must be present and of
  the expected primitive kind; a result missing its declared payload fails
  validation even if the tool "succeeded";
* **stale information** — tools marked ``staleness_bound`` must carry a
  retrieval timestamp inside the freshness horizon;
* **safe retry policy** — only idempotent, transport-class failures are
  retryable; mutating or schema failures are not retried blindly;
* **cross-check recommendation** — single-source factual results from an
  unverified source are flagged for cross-checking rather than treated as
  truth (Stage R: a search result is not the truth).

The verifier never *fixes* a result and never fabricates a replacement; it
returns a structured verdict so the caller (assistant core, supervisor, or
orchestrator) decides whether to retry, downgrade confidence, or report the
failure honestly.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional, Tuple

__all__ = ["ToolVerdict", "ToolVerifier"]

#: Default freshness horizon for staleness-bound tools (24h).
DEFAULT_STALE_AFTER_SECONDS = 24 * 3600.0

_TIMESTAMP_KEYS = ("retrieved_at", "retrieval_time", "fetched_at", "at")

_SCHEMA_KINDS = {
    "string": (str,),
    "int": (int,),
    "float": (float, int),
    "number": (int, float),
    "bool": (bool,),
    "list": (list, tuple),
    "object": (dict,),
    "path": (str,),
    "audio": (str, bytes),
    "image": (str, bytes),
    "base64": (str,),
    "candidates": (list, tuple),
    "plan": (dict, list, str),
    "ranking": (list, tuple),
    "vector": (list, tuple),
    "tool_calls": (list, tuple),
    "relative-path": (str,),
}


@dataclass(frozen=True)
class ToolVerdict:
    """Structured post-execution verdict for one tool result."""

    tool: str
    valid: bool
    malformed: Tuple[str, ...] = ()
    stale: bool = False
    age_seconds: Optional[float] = None
    retry_safe: bool = False
    retry_recommended: bool = False
    cross_check_recommended: bool = False
    confidence: float = 1.0
    reasons: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tool": self.tool,
            "valid": self.valid,
            "malformed": list(self.malformed),
            "stale": self.stale,
            "age_seconds": self.age_seconds,
            "retry_safe": self.retry_safe,
            "retry_recommended": self.retry_recommended,
            "cross_check_recommended": self.cross_check_recommended,
            "confidence": round(self.confidence, 4),
            "reasons": list(self.reasons),
        }


class ToolVerifier:
    """Validate tool results against declared schemas and freshness."""

    def __init__(self, registry: Any, *,
                 stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
                 now: Optional[Any] = None) -> None:
        self.registry = registry
        self.stale_after_seconds = float(stale_after_seconds)
        self._now = now or time.time

    # -- core API ------------------------------------------------------------

    def validate(self, tool_name: str, result: Any, *,
                 error: str = "", source_count: int = 1,
                 verified_source: Optional[bool] = None) -> ToolVerdict:
        """Check one execution outcome; never mutate or replace it."""
        descriptor = self.registry.get(tool_name) if self.registry else None
        reasons: list = []
        confidence = 1.0
        success = not error and result is not None

        if descriptor is None:
            return ToolVerdict(tool=tool_name, valid=False,
                               malformed=("tool is not registered",),
                               reasons=("registration missing; no schema to "
                                        "verify against",))
        payload = _as_mapping(result)

        # -- failure path: classify for retry policy ------------------------
        if not success:
            transport = _looks_transport(error or str(
                (payload or {}).get("error", "")))
            retry_safe = bool(descriptor.idempotent)
            retry_note = (
                "transport failure on an idempotent tool — one bounded "
                "retry is advisable" if (retry_safe and transport)
                else "retry is safe only for idempotent transport failures")
            return ToolVerdict(
                tool=tool_name, valid=False,
                retry_safe=retry_safe,
                retry_recommended=retry_safe and transport,
                confidence=0.0,
                reasons=(f"execution failed: {(error or 'no result')[:180]}",
                         retry_note))

        # -- malformed output detection --------------------------------------
        malformed: list = []
        for key, kind in (descriptor.output_schema or {}).items():
            if key not in (payload or {}):
                malformed.append(f"missing output key {key!r}")
                continue
            expected = _SCHEMA_KINDS.get(str(kind))
            if expected and not isinstance(payload[key], expected):
                malformed.append(f"output key {key!r} has wrong type "
                                 f"{type(payload[key]).__name__}")
        if malformed:
            reasons.extend(malformed)
            confidence = min(confidence, 0.25)

        # -- staleness ---------------------------------------------------------
        stale = False
        age: Optional[float] = None
        if descriptor.staleness_bound and payload:
            timestamp = _timestamp_of(payload)
            if timestamp is None:
                stale = True
                reasons.append("staleness-bound result carries no retrieval "
                               "timestamp")
                confidence = min(confidence, 0.6)
            else:
                age = max(0.0, self._now() - timestamp)
                if age > self.stale_after_seconds:
                    stale = True
                    reasons.append(f"result is {age / 3600.0:.1f}h old "
                                   "(beyond the freshness horizon)")
                    confidence = min(confidence, 0.5)

        # -- cross-check recommendation ---------------------------------------
        cross_check = False
        if source_count <= 1 and _makes_factual_claims(payload):
            if verified_source is False or verified_source is None:
                cross_check = True
                reasons.append("single unverified source — independent "
                               "corroboration recommended before treating "
                               "this as evidence")
                confidence = min(confidence, 0.75)

        valid = not malformed and not stale
        if success and not reasons:
            reasons.append("output matches the declared schema and freshness "
                           "policy")
        return ToolVerdict(tool=tool_name, valid=valid,
                            malformed=tuple(malformed), stale=stale,
                            age_seconds=age,
                            retry_safe=bool(descriptor.idempotent),
                            retry_recommended=False,
                            cross_check_recommended=cross_check,
                            confidence=max(0.0, min(1.0, confidence)),
                            reasons=tuple(reasons))


# -- helpers ---------------------------------------------------------------------

def _as_mapping(result: Any) -> Optional[Dict[str, Any]]:
    if isinstance(result, dict):
        return result
    if hasattr(result, "to_dict"):
        try:
            as_dict = result.to_dict()
            if isinstance(as_dict, dict):
                return as_dict
        except Exception:
            return None
    if isinstance(result, str):
        return {"output": result}
    if isinstance(result, (list, tuple)):
        return {"results": list(result)}
    return None


def _looks_transport(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in (
        "timeout", "timed out", "connection", "unreachable", "reset",
        "temporarily", "503", "502", "429", "rate limit"))


def _timestamp_of(payload: Dict[str, Any]) -> Optional[float]:
    for key in _TIMESTAMP_KEYS:
        value = payload.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
    return None


_FACT_MARKERS = ("result", "results", "answer", "text", "snippet", "finding",
                 "claims", "excerpt")


def _makes_factual_claims(payload: Optional[Dict[str, Any]]) -> bool:
    if not payload:
        return False
    return any(key in payload for key in _FACT_MARKERS)
