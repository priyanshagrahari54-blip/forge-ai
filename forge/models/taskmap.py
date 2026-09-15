"""Task-kind routing for the Model Fabric (A83).

The fabric already routes by *capability* and hard bounds. What it could not
do is answer "this is a big job, send it somewhere capable" — because nothing
mapped an engineering task onto a model *class*.

This module adds that mapping, and it is deliberately deterministic: the task
kind is decided from measurable signals (prompt size, context size, whether
the task is analysis or generation) plus the capability the caller already
declared. No model is asked to classify its own workload.

The classes are a posture, not a vendor list:

``lightweight``
    short, mechanical work. Cheap and fast beats clever.
``coding``
    code generation and editing at real size.
``reasoning``
    architecture, diagnosis, multi-step analysis.
``long-context``
    the input does not fit a normal window.
``heavy``
    the caller explicitly asked for the most capable remote model available.

A request never *requires* a class that no registered model can serve: the
class is a preference expressed as capability + bounds, and the existing
fallback ladder still applies.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

#: Rough characters per token; deliberately conservative.
CHARS_PER_TOKEN = 4
#: Context above this size means the task is a large-repository analysis.
LARGE_CONTEXT_CHARS = 60_000
#: Prompt above this size means substantial generation.
LARGE_PROMPT_CHARS = 6_000

TRIVIAL = "trivial"
CODE_SMALL = "code-small"
CODE_LARGE = "code-large"
ARCHITECTURE = "architecture"
DEBUGGING = "debugging"
REPO_ANALYSIS = "repo-analysis"
DOCUMENTATION = "documentation"
HEAVY = "heavy"
TASK_KINDS = (TRIVIAL, CODE_SMALL, CODE_LARGE, ARCHITECTURE, DEBUGGING,
              REPO_ANALYSIS, DOCUMENTATION, HEAVY)

#: capability -> default task kind when no signal says otherwise
CAPABILITY_DEFAULTS: Dict[str, str] = {
    "coding": CODE_SMALL,
    "reasoning": ARCHITECTURE,
    "planning": ARCHITECTURE,
    "debugging": DEBUGGING,
    "testing": CODE_SMALL,
    "review": CODE_SMALL,
    "security": CODE_SMALL,
    "research": REPO_ANALYSIS,
    "documentation": DOCUMENTATION,
    "long_context": REPO_ANALYSIS,
}

#: Signals that mark an architecture/diagnosis task.
REASONING_SIGNALS = (
    "architect", "design", "trade-off", "tradeoff", "root cause", "diagnose",
    "why does", "analyse", "analyze", "refactor plan", "migration plan",
    "should we", "compare", "evaluate",
)


@dataclass(frozen=True)
class TaskProfile:
    """The routing posture for one kind of task."""

    kind: str
    model_class: str
    capability: str
    #: Extra capabilities the model must also support.
    required_capabilities: Tuple[str, ...] = ()
    min_context_window: int = 0
    complexity: float = 1.0
    #: Soft preferences; the policy can still override them.
    prefer_free: Optional[bool] = None
    prefer_local: Optional[bool] = None
    max_latency_ms: Optional[float] = None
    #: Why this profile was chosen — carried into telemetry.
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind, "model_class": self.model_class,
            "capability": self.capability,
            "required_capabilities": list(self.required_capabilities),
            "min_context_window": self.min_context_window,
            "complexity": self.complexity,
            "prefer_free": self.prefer_free,
            "prefer_local": self.prefer_local,
            "max_latency_ms": self.max_latency_ms,
            "reason": self.reason,
        }


PROFILES: Dict[str, TaskProfile] = {
    TRIVIAL: TaskProfile(
        kind=TRIVIAL, model_class="lightweight", capability="coding",
        complexity=1.0, prefer_free=True, prefer_local=True,
        max_latency_ms=4_000.0,
        reason="short mechanical work; cheap and fast beats clever"),
    CODE_SMALL: TaskProfile(
        kind=CODE_SMALL, model_class="coding", capability="coding",
        complexity=2.0, prefer_free=True,
        reason="code generation at ordinary size"),
    CODE_LARGE: TaskProfile(
        kind=CODE_LARGE, model_class="coding", capability="coding",
        required_capabilities=("structured_output",),
        min_context_window=32_000, complexity=4.0, prefer_free=False,
        reason="large code generation needs a real context window"),
    ARCHITECTURE: TaskProfile(
        kind=ARCHITECTURE, model_class="reasoning", capability="reasoning",
        min_context_window=16_000, complexity=5.0, prefer_free=False,
        reason="architecture and trade-off analysis"),
    DEBUGGING: TaskProfile(
        kind=DEBUGGING, model_class="reasoning", capability="debugging",
        required_capabilities=("reasoning",),
        min_context_window=16_000, complexity=4.0, prefer_free=False,
        reason="diagnosis needs reasoning over a failure, not fluency"),
    REPO_ANALYSIS: TaskProfile(
        kind=REPO_ANALYSIS, model_class="long-context", capability="research",
        required_capabilities=("long_context",),
        min_context_window=128_000, complexity=3.0, prefer_free=False,
        reason="the input is larger than a normal context window"),
    DOCUMENTATION: TaskProfile(
        kind=DOCUMENTATION, model_class="lightweight",
        capability="documentation", complexity=2.0, prefer_free=True,
        reason="documentation is high volume and low risk"),
    HEAVY: TaskProfile(
        kind=HEAVY, model_class="heavy", capability="reasoning",
        min_context_window=32_000, complexity=8.0, prefer_free=False,
        prefer_local=False,
        reason="the caller asked for the most capable model available"),
}


def profile_for(kind: str) -> TaskProfile:
    """Return the profile for a task kind, or raise on an unknown kind."""
    try:
        return PROFILES[kind]
    except KeyError:
        raise ValueError(
            "unknown task kind %r; expected one of %s"
            % (kind, ", ".join(TASK_KINDS))) from None


def classify_task(*, capability: str = "coding", prompt: str = "",
                  context: str = "", task: str = "",
                  complexity: Optional[float] = None,
                  heavy: bool = False) -> TaskProfile:
    """Decide the task kind from measurable signals.

    Order matters and is documented, because it is the difference between a
    deterministic mapping and a pile of heuristics:

    1. an explicit ``heavy`` request always wins;
    2. a context that will not fit a normal window makes it repository
       analysis, whatever the capability says;
    3. otherwise the capability's default applies, promoted to a larger
       variant when the prompt is big or the caller declared high complexity.
    """
    text = " ".join(filter(None, (task, prompt))).lower()
    context_chars = len(context or "")
    prompt_chars = len(prompt or "")

    if heavy:
        return profile_for(HEAVY)
    if context_chars >= LARGE_CONTEXT_CHARS:
        profile = profile_for(REPO_ANALYSIS)
        return _with_reason(profile, "%d context characters exceeds the %d "
                                     "threshold" % (context_chars,
                                                    LARGE_CONTEXT_CHARS))
    base = CAPABILITY_DEFAULTS.get(str(capability).strip().lower())
    if base is None:
        base = TRIVIAL
    if base in (CODE_SMALL,) and (prompt_chars >= LARGE_PROMPT_CHARS
                                  or (complexity or 0) >= 3.0):
        return _with_reason(
            profile_for(CODE_LARGE),
            "prompt of %d characters and/or declared complexity %s"
            % (prompt_chars, complexity))
    if base == ARCHITECTURE and not any(
            signal in text for signal in REASONING_SIGNALS) and (
            prompt_chars < LARGE_PROMPT_CHARS and not context):
        # A "reasoning" capability with no analytical content and no context
        # is small mechanical work wearing a reasoning label.
        return _with_reason(
            profile_for(TRIVIAL),
            "capability was %r but no analysis signal was present"
            % capability)
    profile = profile_for(base)
    if prompt_chars >= LARGE_PROMPT_CHARS and profile.min_context_window < 32_000:
        profile = _replace_context(profile, max(profile.min_context_window,
                                                32_000))
    return _with_reason(profile, "capability %r, prompt %d chars, context %d "
                                 "chars" % (capability, prompt_chars,
                                            context_chars))


def _with_reason(profile: TaskProfile, reason: str) -> TaskProfile:
    return TaskProfile(
        kind=profile.kind, model_class=profile.model_class,
        capability=profile.capability,
        required_capabilities=profile.required_capabilities,
        min_context_window=profile.min_context_window,
        complexity=profile.complexity, prefer_free=profile.prefer_free,
        prefer_local=profile.prefer_local,
        max_latency_ms=profile.max_latency_ms, reason=reason)


def _replace_context(profile: TaskProfile, window: int) -> TaskProfile:
    return TaskProfile(
        kind=profile.kind, model_class=profile.model_class,
        capability=profile.capability,
        required_capabilities=profile.required_capabilities,
        min_context_window=window, complexity=profile.complexity,
        prefer_free=profile.prefer_free, prefer_local=profile.prefer_local,
        max_latency_ms=profile.max_latency_ms, reason=profile.reason)


def request_for(profile: TaskProfile, *, prompt: str = "", context: str = "",
                task: str = "", **kwargs: Any) -> Any:
    """Build a :class:`~forge.models.request.ModelRequest` from a profile."""
    from forge.models.request import ModelRequest
    return ModelRequest(
        prompt=prompt, capability=profile.capability,
        required_capabilities=profile.required_capabilities,
        context=context, task=task,
        min_context_window=profile.min_context_window,
        complexity=profile.complexity,
        prefer_free=kwargs.pop("prefer_free", profile.prefer_free),
        prefer_local=kwargs.pop("prefer_local", profile.prefer_local),
        max_latency_ms=kwargs.pop("max_latency_ms", profile.max_latency_ms),
        metadata={"task_kind": profile.kind,
                  "model_class": profile.model_class,
                  "routing_reason": profile.reason, **kwargs})


def routing_table() -> List[Dict[str, Any]]:
    """The whole mapping, for the cockpit and for ``forge models tasks``."""
    return [PROFILES[kind].to_dict() for kind in TASK_KINDS]
