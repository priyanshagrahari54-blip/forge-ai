"""Native planner: natural-language engineering task → structured steps.

This is a *deterministic, repository-grounded* planner. It is explicitly not
a language model: it derives a step graph from an honest classification pass
over the task text, then grounds that classification in real repository
intelligence (existing file paths, symbol names, test mappings). Ambiguous
input is recorded as low-confidence with unresolved references left visible —
it never silently guesses, and it never pretends the deterministic output
equals neural understanding. Deep semantic interpretation of ambiguous tasks
is a *neural* capability (see :mod:`forge.native.capabilities`); when a
reasoning backend is available the engine asks it to enrich the plan, but the
validated deterministic structure remains authoritative.

Planner step kinds match the A81 requirement exactly:
``inspect``, ``reason``, ``edit``, ``test``, ``debug``, ``review``,
``finish`` — expressed as :class:`forge.native.state.StageKind` so the stage
axis used by the status panel is the same axis the planner emits.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from forge.native.state import StageKind

#: Step kinds alias the stage axis: one vocabulary, no parallel enum.
StepKind = StageKind


class TaskClass:
    """Deterministic task classes the planner understands."""

    ANALYZE = "analyze"          # read-only understanding/reporting
    IMPLEMENT = "implement"      # add new capability
    FIX = "fix"                  # repair broken behavior
    REFACTOR = "refactor"        # restructure without behavior change
    ADD_TESTS = "add_tests"      # extend test coverage
    REVIEW = "review"            # verification-focused, no edits
    TEST_ONLY = "test_only"      # run the suite, report results

    ALL = (ANALYZE, IMPLEMENT, FIX, REFACTOR, ADD_TESTS, REVIEW, TEST_ONLY)


#: Verb lexicons per class. These are transparent classification signals, not
#: "intelligence": they map the leading engineering verb of a request to a
#: plan shape, and everything beyond them must come from repository evidence
#: or a neural backend.
_CLASS_SIGNALS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    (TaskClass.FIX, ("fix", "repair", "resolve", "correct", "patch",
                     "restore", "recover", "debug")),
    (TaskClass.ADD_TESTS, ("test", "tests", "spec", "coverage")),
    (TaskClass.REFACTOR, ("refactor", "restructure", "rename", "reorganize",
                          "simplify", "cleanup", "tidy")),
    (TaskClass.ANALYZE, ("analyze", "analyse", "inspect", "explain",
                         "describe", "report", "summarize", "audit",
                         "inventory", "outline")),
    (TaskClass.REVIEW, ("review", "check", "verify", "validate", "lint")),
    (TaskClass.TEST_ONLY, ("run the tests", "run tests", "execute tests",
                           "test suite")),
    (TaskClass.IMPLEMENT, ("implement", "add", "create", "build", "write",
                           "introduce", "extend", "support", "generate")),
)

#: Words that mark an explicit *write* intent even inside ambiguous text.
_EDIT_INTENT_WORDS = ("implement", "add ", "create", "write", "change",
                      "modify", "fix", "repair", "refactor", "rename",
                      "remove", "delete", "extend", "introduce")

#: Mapping from a leading imperative verb to its class (first-verb signal is
#: the strongest deterministic cue available without a language model).
_LEADING_VERB_CLASS = {
    "fix": TaskClass.FIX, "repair": TaskClass.FIX, "resolve": TaskClass.FIX,
    "debug": TaskClass.FIX, "patch": TaskClass.FIX,
    "refactor": TaskClass.REFACTOR, "rename": TaskClass.REFACTOR,
    "optimize": TaskClass.REFACTOR, "cleanup": TaskClass.REFACTOR,
    "analyze": TaskClass.ANALYZE, "analyse": TaskClass.ANALYZE,
    "inspect": TaskClass.ANALYZE, "explain": TaskClass.ANALYZE,
    "describe": TaskClass.ANALYZE, "summarize": TaskClass.ANALYZE,
    "report": TaskClass.ANALYZE, "audit": TaskClass.ANALYZE,
    "review": TaskClass.REVIEW, "verify": TaskClass.REVIEW,
    "validate": TaskClass.REVIEW,
    "implement": TaskClass.IMPLEMENT, "add": TaskClass.IMPLEMENT,
    "create": TaskClass.IMPLEMENT, "build": TaskClass.IMPLEMENT,
    "write": TaskClass.IMPLEMENT, "run": TaskClass.TEST_ONLY,
    "test": TaskClass.ADD_TESTS, "document": TaskClass.ADD_TESTS,
}

#: Tokens like ``forge/native/planner.py`` or ``tests/test_x.py``.
_PATH_TOKEN = re.compile(
    r"[A-Za-z0-9_.\-/]+[./][A-Za-z0-9_.\-/]*")
_SYMBOLISH_TOKEN = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b")

#: Plan shapes: ordered step kinds per task class. ``edit``-class plans
#: always carry a bounded ``debug`` step so failed tests have a defined
#: repair path instead of an undefined "try again".
_PLAN_SHAPES: Dict[str, Tuple[StepKind, ...]] = {
    TaskClass.ANALYZE: (StepKind.INSPECT, StepKind.REASON, StepKind.FINISH),
    TaskClass.REVIEW: (StepKind.INSPECT, StepKind.REASON, StepKind.REVIEW,
                       StepKind.FINISH),
    TaskClass.TEST_ONLY: (StepKind.INSPECT, StepKind.TEST, StepKind.FINISH),
    TaskClass.IMPLEMENT: (StepKind.INSPECT, StepKind.REASON, StepKind.EDIT,
                          StepKind.TEST, StepKind.DEBUG, StepKind.REVIEW,
                          StepKind.FINISH),
    TaskClass.FIX: (StepKind.INSPECT, StepKind.REASON, StepKind.EDIT,
                    StepKind.TEST, StepKind.DEBUG, StepKind.REVIEW,
                    StepKind.FINISH),
    TaskClass.REFACTOR: (StepKind.INSPECT, StepKind.REASON, StepKind.EDIT,
                         StepKind.TEST, StepKind.DEBUG, StepKind.REVIEW,
                         StepKind.FINISH),
    TaskClass.ADD_TESTS: (StepKind.INSPECT, StepKind.REASON, StepKind.EDIT,
                          StepKind.TEST, StepKind.DEBUG, StepKind.FINISH),
}

#: Steps whose main output cannot exist without a neural backend.
_NEURAL_CORE_STEPS = frozenset({StepKind.EDIT, StepKind.DEBUG})

#: Which engine capabilities (see forge.native.capabilities) each step uses.
_STEP_CAPABILITIES: Dict[StepKind, Tuple[str, ...]] = {
    StepKind.INSPECT: ("repository_analysis",),
    StepKind.REASON: ("planning_structure", "task_understanding"),
    StepKind.EDIT: ("code_generation", "tool_execution"),
    StepKind.TEST: ("test_execution",),
    StepKind.DEBUG: ("failure_classification", "repair_generation"),
    StepKind.REVIEW: ("verification", "review_narration"),
    StepKind.FINISH: ("memory",),
}


@dataclass
class NativeStep:
    """One planned step: kind, rationale, grounded targets, status."""

    id: str
    kind: StepKind
    description: str
    depends_on: Tuple[str, ...] = ()
    #: Repository files this step must look at (grounded, existing paths).
    target_files: Tuple[str, ...] = ()
    #: Test files selected for edit/test steps from the test mapping.
    test_targets: Tuple[str, ...] = ()
    #: Why this step exists — the exact signals that produced it.
    rationale: str = ""
    #: Execution status: planned | running | done | skipped_no_model |
    #: skipped_no_write_authorization | failed.
    status: str = "planned"
    #: Optional per-step execution summary (real counts, not prose).
    result: Dict[str, Any] = field(default_factory=dict)

    @property
    def requires_neural(self) -> bool:
        """True when the step's *primary output* needs neural inference.

        EDIT (code generation) and DEBUG (repair generation) are blocked
        without a model. REASON and REVIEW are not: they keep full
        deterministic behavior and only *enrich* (narration, suggestions)
        when a model exists — enrichment is optional, not a dependency.
        """
        return self.kind in _NEURAL_CORE_STEPS

    @property
    def capabilities(self) -> Tuple[str, ...]:
        return _STEP_CAPABILITIES.get(self.kind, ())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "description": self.description,
            "depends_on": list(self.depends_on),
            "target_files": list(self.target_files),
            "test_targets": list(self.test_targets),
            "rationale": self.rationale,
            "status": self.status,
            "requires_neural": self.requires_neural,
            "result": dict(self.result),
        }


@dataclass
class NativePlan:
    """Validated, executable plan for one engineering task."""

    task_text: str
    task_class: str
    confidence: str
    steps: List[NativeStep] = field(default_factory=list)
    #: Repository references in the task that matched real files.
    matched_files: Tuple[str, ...] = ()
    #: Task words that matched real symbols in the index.
    matched_symbols: Tuple[str, ...] = ()
    #: Path-like tokens that matched nothing — surfaced, never dropped.
    unresolved_references: Tuple[str, ...] = ()
    notes: List[str] = field(default_factory=list)

    def requires_neural(self) -> bool:
        return any(step.requires_neural for step in self.steps)

    def has_edit_step(self) -> bool:
        return any(step.kind == StepKind.EDIT for step in self.steps)

    def step(self, kind: StepKind) -> Optional[NativeStep]:
        for step in self.steps:
            if step.kind == kind:
                return step
        return None

    def validate(self) -> List[str]:
        """Return structured problems (empty when the plan is executable).

        Checks: unique ids, every ``depends_on`` reference exists, no
        self-dependency, and acyclicity via Kahn's algorithm (graphlib is
        3.9+; this stays 3.8-safe).
        """
        problems: List[str] = []
        seen: Dict[str, NativeStep] = {}
        for step in self.steps:
            if step.id in seen:
                problems.append(f"duplicate step id: {step.id}")
            seen[step.id] = step
        for step in self.steps:
            for dep in step.depends_on:
                if dep == step.id:
                    problems.append(f"step {step.id} depends on itself")
                elif dep not in seen:
                    problems.append(
                        f"step {step.id} depends on unknown step {dep}")
        # Kahn cycle check.
        remaining = {step.id: set(step.depends_on) for step in self.steps}
        progressed = True
        while remaining and progressed:
            progressed = False
            for step_id in list(remaining):
                if not (remaining[step_id] & set(remaining)):
                    del remaining[step_id]
                    progressed = True
        if remaining:
            problems.append(
                "plan contains a dependency cycle: "
                + ", ".join(sorted(remaining)))
        return problems

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_text": self.task_text[:500],
            "task_class": self.task_class,
            "confidence": self.confidence,
            "steps": [step.to_dict() for step in self.steps],
            "matched_files": list(self.matched_files),
            "matched_symbols": list(self.matched_symbols),
            "unresolved_references": list(self.unresolved_references),
            "notes": list(self.notes),
        }


@dataclass
class TaskClassification:
    """The deterministic classification signal set behind a plan."""

    task_class: str = TaskClass.IMPLEMENT
    confidence: str = "low"
    matched_signals: List[str] = field(default_factory=list)
    edit_intent: bool = False
    #: Honest note about what this classification is (and is not).
    method: str = ("deterministic verb-lexicon + repository-evidence; "
                   "semantic interpretation of ambiguous intent requires "
                   "a neural backend")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_class": self.task_class,
            "confidence": self.confidence,
            "matched_signals": list(self.matched_signals),
            "edit_intent": self.edit_intent,
            "method": self.method,
        }


class NativePlanner:
    """Convert an engineering task into a structured, validated plan."""

    #: Hard caps keep planning cheap on very low-RAM machines.
    MAX_TARGET_FILES = 12
    MAX_SYMBOLS = 12

    def __init__(self, intelligence: Any = None) -> None:
        #: ``RepositoryIntelligence`` (or anything exposing the same
        #: attributes) grounds targets; ``None`` yields file-less plans that
        #: are still structurally valid.
        self.intelligence = intelligence

    # -- public API -----------------------------------------------------------

    def classify(self, task_text: str) -> TaskClassification:
        """Deterministic task-class signal extraction (documented heuristics).

        The *first* class lexicon with any hit wins, in the order declared in
        ``_CLASS_SIGNALS`` (fixes outrank generic "add"; explicit test phrasing
        outranks write verbs). No repository access happens here.
        """
        lowered = " ".join((task_text or "").lower().split())
        if not lowered:
            raise ValueError("cannot plan an empty task")
        result = TaskClassification()
        result.edit_intent = any(word in lowered
                                  for word in _EDIT_INTENT_WORDS)
        hits: Dict[str, List[str]] = {}
        for name, signals in _CLASS_SIGNALS:
            found = [signal for signal in signals if signal in lowered]
            if name == TaskClass.ADD_TESTS and found \
                    and not result.edit_intent:
                # "tests" alone is a mention (e.g. "describe its tests"),
                # not a request to add any: it needs a write verb too.
                found = []
            if found:
                hits[name] = found
        # A leading imperative verb is the strongest signal; the priority
        # scan only decides when the sentence does not start with one.
        first_word = lowered.split(" ", 1)[0].strip(".,:;!?")
        leading = _LEADING_VERB_CLASS.get(first_word)
        if leading == TaskClass.ADD_TESTS:
            if not result.edit_intent:
                # "test the module ..." without a write verb means
                # "run the tests", not "author tests":
                leading = TaskClass.TEST_ONLY
        if leading is not None:
            best: Optional[str] = leading
            result.matched_signals = sorted(
                set([first_word] + hits.get(leading, [])))
        else:
            best = None
            for name, _signals in _CLASS_SIGNALS:
                if name in hits:
                    best = name
                    break
            result.matched_signals = sorted(set(hits.get(best or "", [])))
        result.task_class = best or TaskClass.IMPLEMENT
        result.confidence = "low" if best is None else "medium"
        return result

    def plan(self, task_text: str,
             intelligence: Any = None) -> NativePlan:
        """Build and validate the plan for ``task_text``."""
        if not task_text or not task_text.strip():
            raise ValueError("cannot plan an empty task")
        intelligence = intelligence if intelligence is not None \
            else self.intelligence
        classification = self.classify(task_text)
        matched_files, matched_symbols, unresolved = self._ground_references(
            task_text, intelligence)
        if matched_files or matched_symbols:
            classification.confidence = ("high"
                                         if classification.confidence != "low"
                                         else "medium")
        elif classification.confidence == "low":
            classification.confidence = "low"

        plan = NativePlan(
            task_text=" ".join(task_text.split()),
            task_class=classification.task_class,
            confidence=classification.confidence,
            matched_files=tuple(matched_files),
            matched_symbols=tuple(matched_symbols),
            unresolved_references=tuple(unresolved),
        )
        if classification.task_class == TaskClass.ANALYZE and \
                classification.edit_intent:
            plan.notes.append(
                "task mixes read-only and edit verbs; defaulting to the "
                "safer analyze plan — restate with an explicit write verb "
                "to plan edits")
            plan.task_class = TaskClass.ANALYZE
            classification.edit_intent = False

        shapes = _PLAN_SHAPES[plan.task_class]
        previous_id: Optional[str] = None
        for index, kind in enumerate(shapes):
            step_id = "s%d" % (index + 1)
            description, rationale = self._step_text(
                kind, plan, matched_files, classification)
            step = NativeStep(
                id=step_id,
                kind=kind,
                description=description,
                depends_on=(previous_id,) if previous_id else (),
                target_files=tuple(matched_files) if kind in (
                    StepKind.INSPECT, StepKind.REASON, StepKind.EDIT) else (),
                test_targets=self._tests_for(matched_files, intelligence)
                if kind in (StepKind.TEST, StepKind.EDIT, StepKind.DEBUG)
                else (),
                rationale=rationale,
            )
            plan.steps.append(step)
            previous_id = step_id
        if plan.task_class == TaskClass.TEST_ONLY:
            test_step = plan.step(StepKind.TEST)
            if test_step is not None:
                test_step.test_targets = tuple(matched_files)

        problems = plan.validate()
        if problems:
            raise ValueError("planner produced an invalid plan: "
                             + "; ".join(problems))
        return plan

    # -- grounding helpers ------------------------------------------------------

    def _all_files(self, intelligence: Any) -> Sequence[str]:
        if intelligence is None:
            return ()
        architecture = getattr(intelligence, "architecture", None)
        files = list(getattr(architecture, "source_files", ()) or ())
        files += list(getattr(architecture, "test_files", ()) or ())
        return list(dict.fromkeys(files))

    def _ground_references(self, task_text: str, intelligence: Any
                           ) -> Tuple[List[str], List[str], List[str]]:
        """Match task tokens against real files and symbols in the repo.

        Two independent mechanisms (both cheap and exact):
        path-like tokens are compared against the repository inventory, and
        symbol-ish words are looked up in the symbol index.
        """
        known_files = [path for path in self._all_files(intelligence)]
        by_suffix: Dict[str, str] = {}
        for path in known_files:
            parts = path.split("/")
            for count in range(1, len(parts) + 1):
                by_suffix.setdefault("/".join(parts[-count:]), path)
        matched_files: List[str] = []
        unresolved: List[str] = []
        for token in _PATH_TOKEN.findall(task_text):
            candidate = token.strip("./").lower()
            hit = by_suffix.get(candidate)
            if hit is None:
                hit = by_suffix.get(token.strip("./"))
            if hit is not None:
                if hit not in matched_files:
                    matched_files.append(hit)
            elif "." in candidate and ("/" in candidate
                                       or candidate.endswith((".py", ".js",
                                                               ".md", ".txt"))):
                unresolved.append(token)
        matched_symbols: List[str] = []
        symbols = getattr(intelligence, "symbols", None) if intelligence \
            else None
        if symbols is not None:
            # Ordered, de-duplicated word scan: iterating a set() would make
            # symbol-match order depend on the interpreter's hash seed, and
            # plan/ccontext fingerprints must be stable across processes.
            ordered_words = list(dict.fromkeys(task_text.split()))
            for word in ordered_words:
                clean = word.strip("`'\"().,:;!?[]{}")
                if not clean or not _SYMBOLISH_TOKEN.match(clean):
                    continue
                if any(clean.lower() in matched for matched in matched_files):
                    continue
                try:
                    found = symbols.find(clean)
                except Exception:
                    found = []
                for symbol in found[:2]:
                    name = getattr(symbol, "name", "")
                    if name and name not in matched_symbols:
                        matched_symbols.append(name)
                if len(matched_symbols) >= self.MAX_SYMBOLS:
                    break
        return (matched_files[:self.MAX_TARGET_FILES],
                matched_symbols[:self.MAX_SYMBOLS],
                unresolved[:self.MAX_TARGET_FILES])

    def _tests_for(self, files: Sequence[str], intelligence: Any
                   ) -> Tuple[str, ...]:
        """Select tests related to the planned change targets (or all mapped
        tests when no targets were named). Never invents test paths."""
        tests = getattr(intelligence, "tests", None) if intelligence else None
        if tests is None:
            return ()
        selected: List[str] = []
        if files:
            for path in files:
                try:
                    mapped = tests.tests_for_source(path)
                except Exception:
                    mapped = []
                for test in mapped:
                    if test not in selected:
                        selected.append(test)
        else:
            mapping = getattr(tests, "test_to_sources", {}) or {}
            selected = sorted(mapping.keys())
        return tuple(selected[:self.MAX_TARGET_FILES])

    def _step_text(self, kind: StepKind, plan: NativePlan,
                   matched_files: Sequence[str],
                   classification: TaskClassification
                   ) -> Tuple[str, str]:
        """Describe one step and *why* it exists, from real signals only."""
        targets = ", ".join(matched_files[:5]) if matched_files \
            else "repository-wide (no explicit file references)"
        evidence = ("signals: %s; targets: %s"
                    % (", ".join(classification.matched_signals)
                       or "none (default class)", targets))
        if kind == StepKind.INSPECT:
            return ("Inspect repository inventory, imports, symbols, "
                    "dependencies, and tests relevant to the task.",
                    "Every plan starts from real repository state. " + evidence)
        if kind == StepKind.REASON:
            return ("Reason over the task and evidence: confirm the task "
                    "class, resolve targets, and finalize step data.",
                    "Deterministic structure + optional neural enrichment; "
                    "confidence: %s. " % plan.confidence + evidence)
        if kind == StepKind.EDIT:
            return ("Construct the change proposal, validate it through the "
                    "ChangeSet engine, and apply it only if authorized.",
                    "Class '%s' implies code changes; generation requires a "
                    "neural backend. " % plan.task_class + evidence)
        if kind == StepKind.TEST:
            return ("Run the mapped tests (targeted when possible, full "
                    "suite otherwise) and record the real result.",
                    "Edits are only meaningful against executed tests. "
                    + evidence)
        if kind == StepKind.DEBUG:
            return ("On failure: classify it, collect failure context, "
                    "request a repair plan, apply authorized repairs, and "
                    "re-verify within the bounded retry budget.",
                    "Bounded repair path defined up front; repairs are "
                    "model output and remain gated.")
        if kind == StepKind.REVIEW:
            return ("Independent review: diff validation, security scan, "
                    "and review gate over the changed files.",
                    "Verification gates run on the actual diff; a failed "
                    "check stays failed.")
        return ("Record outcomes to project memory and emit the final "
                "report.",
                "Memory + reporting close the loop with measured data.")
