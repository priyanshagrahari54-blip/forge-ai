"""Forge Native AI Engine — first-party orchestration for low-power machines.

The native engine is Forge's own software-engineering brain: it understands a
task, plans structured steps, inspects the repository, builds budgeted
context, drives coding through the existing authorized tool layers, verifies
every change, runs bounded debug cycles, and records durable memory. It owns
the *workflow*; heavy language-model inference stays optional and replaceable
through the reasoning-backend interface, so the engine never depends on a
mandatory external provider (Ollama, OpenAI, Gemini, ...).

Layering (all reuse of existing Forge infrastructure, no parallel security
model):

* :mod:`forge.native.planner` — natural-language task → structured step plan
  (inspect/reason/edit/test/debug/review/finish). Deterministic; explicitly
  *not* a language model.
* :mod:`forge.native.reasoning` — the stable reasoning-backend interface with
  three backends: the native deterministic backend (free, always available),
  a local neural-model interface, and a remote neural-model interface. Both
  neural interfaces route through the existing
  :class:`forge.models.fabric.ModelFabric` — one abstraction for every model.
* :mod:`forge.native.context` — task-relevance context assembly on top of
  :class:`forge.intelligence.repository.RepositoryIntelligence`, related
  tests, recent changes, previous failures, and memory. Never "the whole
  repository".
* :mod:`forge.native.coding` — inspect → propose → validate → authorized
  apply → test, reusing the A32 ChangeSet engine, policy gate, and runtime.
* :mod:`forge.native.verification` — compile/syntax, tests, build/lint,
  security, and diff validation gates; a failed check stays failed.
* :mod:`forge.native.debugging` — failure → classify → collect context →
  repair plan → authorized repair → verify, bounded retries.
* :mod:`forge.native.memory` — project memory (decisions, strategies,
  failures, patterns, verification results) on the durable
  :class:`forge.memory.MemoryStore`, with credential rejection.
* :mod:`forge.native.state` — live status snapshot consumed by the CLI and
  the desktop app's Native AI panel.
* :mod:`forge.native.training` — interfaces for future Forge-trained models
  (datasets, validation, jobs, evaluation, versioning, promotion, rollback).
  No trained model exists until a real training run produces one; nothing
  here fabricates training.

The engine refuses to claim work it did not do: without a neural backend,
change *generation* is reported as ``NEURAL_REQUIRED`` while repository
analysis, planning, testing, verification, and memory keep working (see
:mod:`forge.native.capabilities`).

Python 3.8 / Windows 7 friendly: stdlib-only at import time, no new
dependencies, no threads at construction, bounded memory by default.
"""
from __future__ import annotations

from forge.native.capabilities import (
    CAPABILITIES,
    CapabilityInfo,
    capability_matrix,
    requires_neural,
)
from forge.native.state import (
    EngineState,
    NativeStatusSnapshot,
    StageKind,
    read_snapshot,
    write_snapshot,
)
from forge.native.planner import NativePlan, NativePlanner, NativeStep, StepKind
from forge.native.memory import MemoryCategory, NativeMemory
from forge.native.reasoning import (
    NativeDeterministicBackend,
    ReasoningBackend,
    ReasoningHub,
    ReasoningKind,
    ReasoningRefusal,
    ReasoningRequest,
    ReasoningResult,
    RefusalCode,
)
from forge.native.context import NativeContext, NativeContextEngine
from forge.native.verification import NativeVerificationReport, NativeVerifier
from forge.native.coding import NativeCodingEngine
from forge.native.debugging import DebugCycle, NativeDebugLoop, NativeDebugResult
from forge.native.reporting import NativeRunReport
from forge.native.panel import format_native_status
from forge.native.engine import NativeAIEngine, NativeRunResult
from forge.native.training import NativeTrainingFabric

__all__ = [
    "CAPABILITIES",
    "CapabilityInfo",
    "capability_matrix",
    "requires_neural",
    "EngineState",
    "NativeStatusSnapshot",
    "StageKind",
    "read_snapshot",
    "write_snapshot",
    "NativePlan",
    "NativePlanner",
    "NativeStep",
    "StepKind",
    "MemoryCategory",
    "NativeMemory",
    "NativeDeterministicBackend",
    "ReasoningBackend",
    "ReasoningHub",
    "ReasoningKind",
    "ReasoningRefusal",
    "ReasoningRequest",
    "ReasoningResult",
    "RefusalCode",
    "NativeContext",
    "NativeContextEngine",
    "NativeVerificationReport",
    "NativeVerifier",
    "NativeCodingEngine",
    "DebugCycle",
    "NativeDebugLoop",
    "NativeDebugResult",
    "NativeRunReport",
    "format_native_status",
    "NativeAIEngine",
    "NativeRunResult",
    "NativeTrainingFabric",
]
