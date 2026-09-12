"""Free-first capability matrix for the Native AI Engine.

Single source of truth for *which capabilities work without a neural model*
and which require neural inference. The CLI, the desktop status panel, the
self-test, and the documentation all read this matrix, so the labels in
``docs/A81-NATIVE-AI-ENGINE.md`` can never drift from the code (a test
asserts every capability is documented).

Honesty contract:

* ``requires_neural=False`` means the capability is implemented with real
  deterministic code (repository analysis, planning structures, gated
  tool execution, test running, verification, memory). It does **not** mean a
  language model is involved, and deterministic orchestration is never sold
  as neural equivalence.
* ``requires_neural=True`` means the capability produces semantic content
  (code, repairs, explanations) that only a neural backend can supply.
  Without one, the engine refuses with ``NEURAL_REQUIRED`` instead of
  fabricating output.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CapabilityInfo:
    """One named engine capability and its model dependency label."""

    id: str
    label: str
    requires_neural: bool
    #: What the capability does without a model (free mode) — empty when the
    #: capability is unavailable without a model.
    free_mode: str = ""
    #: What changes when a neural backend is attached — empty for free-only
    #: capabilities.
    with_model: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "requires_neural": self.requires_neural,
            "free_mode": self.free_mode,
            "with_model": self.with_model,
        }


CAPABILITIES = (
    CapabilityInfo(
        id="repository_analysis",
        label="Repository analysis (inventory, imports, symbols, "
              "dependencies, tests)",
        requires_neural=False,
        free_mode="Full: uses the existing RepositoryIntelligence index.",
    ),
    CapabilityInfo(
        id="planning_structure",
        label="Structured task planning (inspect/reason/edit/test/debug/"
              "review/finish steps)",
        requires_neural=False,
        free_mode="Full: deterministic plan construction with repository-"
                  "grounded targets and dependency validation.",
        with_model="A neural backend may additionally enrich step "
                   "descriptions; the deterministic structure stays "
                   "authoritative.",
    ),
    CapabilityInfo(
        id="context_construction",
        label="Budgeted context construction (relevance, tests, recent "
              "changes, failures, memory)",
        requires_neural=False,
        free_mode="Full: relevance-ranked, token-budgeted context; the "
                  "repository is never dumped wholesale.",
    ),
    CapabilityInfo(
        id="deterministic_orchestration",
        label="Deterministic orchestration (stage machine, checkpoints, "
              "status snapshots)",
        requires_neural=False,
        free_mode="Full: the engine's own state machine.",
    ),
    CapabilityInfo(
        id="tool_execution",
        label="Tool execution through the A32/A33 policy system "
              "(files, terminal, git, tests)",
        requires_neural=False,
        free_mode="Full: every action passes the existing policy gate; the "
                  "engine has no write path that skips it.",
    ),
    CapabilityInfo(
        id="test_execution",
        label="Test execution (targeted or full suite)",
        requires_neural=False,
        free_mode="Full: runs the real pytest command through the "
                  "permissioned runtime and records the real exit code.",
    ),
    CapabilityInfo(
        id="verification",
        label="Verification gates (compile/syntax, tests, build, lint, "
              "security, diff validation)",
        requires_neural=False,
        free_mode="Full: real subprocess checks; failed checks stay failed.",
    ),
    CapabilityInfo(
        id="memory",
        label="Project memory (decisions, strategies, failures, patterns, "
              "verification results)",
        requires_neural=False,
        free_mode="Full: durable bounded entries; secrets are never stored.",
    ),
    CapabilityInfo(
        id="failure_classification",
        label="Failure classification (syntax/import/assertion/collection/"
              "timeout/other)",
        requires_neural=False,
        free_mode="Full: deterministic traceback/test-output analysis.",
        with_model="A neural diagnosis narrative is attached as model-"
                   "provenance metadata when a backend answers.",
    ),
    CapabilityInfo(
        id="task_understanding",
        label="Deep semantic understanding of ambiguous natural-language "
              "tasks",
        requires_neural=True,
        free_mode="Partial: verb-class + repository-token classification "
                  "(explicitly deterministic heuristics, not a language "
                  "model); ambiguous input is recorded as low-confidence.",
        with_model="A neural backend classifies/refines intent through the "
                   "same structured interface.",
    ),
    CapabilityInfo(
        id="code_generation",
        label="Code / change generation (proposed file contents)",
        requires_neural=True,
        with_model="Proposals come from the model through the Model Fabric, "
                   "then validation + authorization (never trusted output).",
    ),
    CapabilityInfo(
        id="repair_generation",
        label="Repair generation (fixing failing tests)",
        requires_neural=True,
        free_mode="Partial: the failure is classified and context is "
                  "collected; the repair itself is refused with "
                  "NEURAL_REQUIRED, never faked.",
        with_model="Bounded repair cycles: model proposes, the ChangeSet "
                   "engine validates, the policy gate authorizes, tests "
                   "re-verify.",
    ),
    CapabilityInfo(
        id="review_narration",
        label="Narrative code-review commentary",
        requires_neural=True,
        free_mode="Partial: the deterministic review gate (severity-"
                  "classified findings) always runs.",
        with_model="Model reviewer findings are merged through the existing "
                   "review gate; the deterministic gate remains mandatory.",
    ),
    CapabilityInfo(
        id="explanation",
        label="Free-form explanations and summaries",
        requires_neural=True,
        free_mode="Partial: structured, template-rendered status reports "
                  "built from real run data.",
        with_model="Model prose is labeled with model/provider provenance.",
    ),
    CapabilityInfo(
        id="model_training",
        label="Forge-trained model production (datasets, training, "
              "evaluation, promotion)",
        requires_neural=True,
        free_mode="Interfaces only: dataset collection/validation and "
                  "version management run locally; no trainer ships.",
        with_model="Future: training jobs and evaluation require real "
                   "compute/runtime wired through the Model Fabric.",
    ),
)

_BY_ID = {capability.id: capability for capability in CAPABILITIES}


def capability_matrix() -> tuple[CapabilityInfo, ...]:
    """Return the frozen capability matrix."""
    return CAPABILITIES


def capability_for(capability_id: str) -> "CapabilityInfo | None":
    return _BY_ID.get(capability_id)


def requires_neural(capability_id: str) -> bool:
    """True when the named capability needs neural inference.

    Unknown ids fail closed: needing a model is the safer answer than
    silently advertising a capability that does not exist.
    """
    capability = _BY_ID.get(capability_id)
    if capability is None:
        return True
    return capability.requires_neural


def free_capabilities() -> tuple[CapabilityInfo, ...]:
    """Capabilities that work with no neural model configured."""
    return tuple(c for c in CAPABILITIES if not c.requires_neural)


def neural_capabilities() -> tuple[CapabilityInfo, ...]:
    """Capabilities whose full behavior requires a neural backend."""
    return tuple(c for c in CAPABILITIES if c.requires_neural)
