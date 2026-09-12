# A81 — Forge Native AI Engine

First-party software-engineering engine for low-power installations.
Target hardware of record: **Lenovo G560 — Windows 7, 32-bit, Python
3.8.10, 2 GB RAM**. The G560 is the *lightweight Forge client*: the engine
itself owns planning, repository work, orchestration, verification, and
memory, while heavy neural inference stays **optional and replaceable**
through one stable reasoning-backend interface. No external AI provider
(Ollama, OpenAI, Gemini, …) is ever mandatory.

Source: `forge/native/` · CLI: `forge native-ai` · panel: Native AI tab of
`forge desktop` · integration: `forge run --native`.

---

## 1. What this is — and what it deliberately is not

**Is**: the central engine for task understanding, planning, repository
inspection, context construction, action planning, tool execution,
verification, debugging, memory, and final reporting (layers 1–10 below).

**Is not**:

* a fake chatbot — there is no conversational veneer; every output is a
  structured record of work that actually executed;
* a claim that deterministic code is equivalent to a language model — the
  deterministic backend does structured, auditable reasoning (classifiers,
  repository evidence, gates) and **refuses** generative work with
  `NEURAL_REQUIRED` instead of imitating a model;
* a replacement for the existing stack — the Supervisor, Model Fabric, A32
  ChangeSet engine, and A33 policy platform stay authoritative and are
  reused directly (see §10 for the one deliberate overlap).

## 2. Architecture

```
 forge native-ai "<task>"            forge run --native "<task>"          Desktop → Native AI tab
        │                                     │                                   ▲
        ▼                                     ▼                                   │
 ┌───────────────────────────── NativeAIEngine (forge/native/engine.py) ─────────┐
 │ understand → inspect → plan → context → reason → execute steps → verify →     │
 │ memorize → report                       (stage machine, .forge/native/state) ─│
 └──┬──────────┬──────────┬─────────┬──────────┬──────────┬─────────┬─────────┬─┘
    ▼          ▼          ▼         ▼          ▼          ▼         ▼         │
 planner   RepoIntell.  context   reasoning  coding     verifier  memory      │
 (§4)      (§5, reuse)   (§5)     (§6)      (§7,reuse)  (§8)      (§9)        │
                                     │          │                              │
                              ReasoningHub   ChangeApplier + PolicyGate        │
                              (§6 backends)  + CheckpointManager (A32/A33)     │
                                     │                                         │
                              ModelFabric (A31/A46 — one model abstraction)────┘
                                     │                       snapshot read by UI
                              real models only
```

| Module | Responsibility |
|---|---|
| `forge/native/engine.py` | Central lifecycle, cancellation, run records, status |
| `forge/native/planner.py` | Task → structured steps (inspect/reason/edit/test/debug/review/finish) |
| `forge/native/context.py` | Budgeted, provenance-labeled context assembly |
| `forge/native/reasoning.py` | Reasoning backend interface + 3 backend kinds + hub |
| `forge/native/coding.py` | inspect → propose → validate → authorized apply → test |
| `forge/native/verification.py` | compile/tests/build/lint/security/diff gates |
| `forge/native/debugging.py` | Bounded debug loop with failure classification |
| `forge/native/memory.py` | Five-category durable project memory, credential-guarded |
| `forge/native/state.py` | Engine state machine + atomic status snapshot |
| `forge/native/panel.py` | Tk-free status-panel text renderer |
| `forge/native/capabilities.py` | Free-vs-neural capability matrix (single source of truth) |
| `forge/native/training.py` | Future model-training interfaces (honest, inert by default) |
| `forge/native/reporting.py` | Redacted run reports |
| `forge/native/selftest.py` | Deterministic self-check battery behind `forge native-ai test` |

## 3. Capability matrix (free-first labels)

The matrix lives in `forge/native/capabilities.py`; the CLI, panel, docs and
tests all read it (a test asserts this file lists every capability id).
`[free]` works with **no neural model at all**; `[model]` needs one.

| Capability | Mode | Notes |
|---|---|---|
| `repository_analysis` | **[free]** | Full — file inventory, imports, symbols, dependencies, tests, relationships (reuses `RepositoryIntelligence`, incl. change tracking via git and `source_context()` per-file relationships). |
| `planning_structure` | **[free]** | Full — step kinds, dependency chains, validation (acyclicity, grounded targets). A neural backend may enrich step descriptions; structure stays deterministic and authoritative. |
| `context_construction` | **[free]** | Full — relevance, tests, recent changes, failures, memory; token-budgeted with truncation labels; never a repo dump. |
| `deterministic_orchestration` | **[free]** | Full — stage machine, checkpoint lifecycle, snapshot persistence, run records. |
| `tool_execution` | **[free]** | Full — files/terminal/git/tests through the A32/A33 permission chain; no bypass path exists in the engine. |
| `test_execution` | **[free]** | Full — targeted or full-suite pytest, real exit codes, recorded verbatim. |
| `verification` | **[free]** | Full — compile, tests, build, lint (when configured), security, diff validation. Failed stays failed. |
| `memory` | **[free]** | Full — decisions, strategies, failures, patterns, verification results; secrets redacted or refused. |
| `failure_classification` | **[free]** | Full — deterministic categories (syntax/import/collection/assertion/timeout/fixture/unknown); with a model, a narrative diagnosis is attached as labeled metadata. |
| `task_understanding` | **[model]** for depth | Free mode: verb-lexicon classification + repository-token grounding, marked low-confidence when ambiguous (explicitly heuristics, not language understanding). Model mode: neural classification arrives through the same structured interface. |
| `code_generation` | **[model]** | Proposals come from the routed model via the Model Fabric, then pass the A32 ChangeSet engine and A33 policy gate like any other change. Without a model: refused (`NEURAL_REQUIRED`), no file writes. |
| `repair_generation` | **[model]** | Same gate path as code generation, inside the bounded debug loop. The failure classification and context collection are free; only the fix itself needs a model. |
| `review_narration` | **[model]** for prose | The deterministic review gate always runs — A32's own `ReviewGate` over the real diff (conflict markers, dynamic exec, test weakening, severity verdict) plus the verification pipeline's diff review. A failed review blocks the run: applied changes are rolled back and the run reports `FAILED`. Model reviewer commentary is merged as additional findings and the gate remains mandatory. |
| `explanation` | **[model]** for prose | Template-rendered structured reports are always free; prose summaries require a model and carry its provenance. |
| `model_training` | **[model]** + real runs | Interfaces only (§8). Dataset collection/validation work locally over real records; no trainer ships. |

Final statuses encode this honestly: an edit-class plan without a model
finishes as **`NEEDS_MODEL`** (never `COMPLETED`), and any applied change
that fails tests or verification is **rolled back** and reported `FAILED`.

## 4. Planner (layer 2)

`NativePlanner.plan(task, intelligence)` returns a `NativePlan`:

* **task classes**: `analyze`, `implement`, `fix`, `refactor`, `add_tests`,
  `review`, `test_only` — chosen by deterministic verb signals, then
  confidence-upgraded only by repository evidence;
* **step kinds** are exactly the required seven: `inspect, reason, edit,
  test, debug, review, finish` (shared enum with the status stage axis —
  `StepKind is StageKind`);
* **grounding**: path-like tokens are matched against the real inventory,
  words against the symbol index; unmatched references are reported in
  `unresolved_references`, never dropped; test targets come from the
  existing test mapping;
* **validation**: unique ids, existing dependencies, acyclicity (Kahn — no
  3.9 `graphlib`); invalid plans raise at plan time, not at execution time;
* mixed read/write verbs resolve to the **safer** `analyze` shape with a
  note.

The classification is transparent heuristics, and every report carries that
provenance ("deterministic verb-lexicon + repository-evidence; semantic
interpretation of ambiguous intent requires a neural backend").

## 5. Repository intelligence + context engine (layers 3–4)

Reused, not rebuilt: `RepositoryIntelligence` (file inventory, imports,
symbols, dependency graph, architecture, test mapping, runtime commands),
`AgentContextBuilder` (relevance → dependency expansion → test selection →
token budget → deterministic fingerprint), and git change tracking.

`NativeContextEngine` adds the A81-specific sections — plan summary,
related tests for the plan targets, recent changes (git status/last commits,
marked `unavailable` outside a worktree), previous failures and decisions
from memory — and enforces:

* **budget**: default 1600 estimated tokens (half the supervisor's), lowest
  priority sections trimmed first, every cut recorded (`truncated_chars`);
* **labels**: every section is `present` / `empty` / `unavailable` — an
  empty context stays visibly empty;
* **stability**: the same inputs produce the same fingerprint (deterministic
  ordering), so context identity is comparable across retries and records.

## 6. Reasoning interface (layer 5) — one stable abstraction

```
ReasoningRequest(kind, task, context, payload) → ReasoningResult(ok|refusal)
kinds: understand · refine_plan · select_targets · diagnose · review   (structural)
       repair · generate · narrate                                       (generative)
refusals: NEURAL_REQUIRED · BACKEND_ERROR · INVALID_MODEL_OUTPUT · NOT_CONFIGURED
```

* **Native deterministic backend** — always available; implements the
  structural kinds with real, inspectable logic (classification via the
  planner, target ranking via the context selection, failure categorization,
  verification-to-findings transformation). Refuses every generative kind by
  design. **Never** emits code, and never rewords template text as model
  output.
* **Local neural backend** / **Remote neural backend** — interfaces over the
  **Model Fabric only** (`forge.models.fabric.ModelFabric`; module-scope
  vendor imports in `forge/native/` are test-forbidden). They activate only
  when a **real (non-fallback) model is registered**; the offline
  placeholder never counts. Model output is parsed with the same tolerant
  JSON loader the A32 coder uses; unparsable output is
  `INVALID_MODEL_OUTPUT` — the raw excerpt is retained for diagnosis and
  nothing is applied.
* **Hub selection** (deterministic, explained): generative kinds → first
  available neural backend (local before remote, free-first); structural
  kinds → always deterministic; a neural *suggestion* on structural kinds
  requires opting in (`use_neural_suggestions`) and is stored as labeled
  advisory data. A failed neural call is a failure; there is no silent
  fallback to fabricated content.
* Status separation: the panel distinguishes *active structural backend*
  from *generative backend*, and **registered ≠ verified** — liveness text
  comes from call-derived health transitions only, never from
  configuration.

## 7. Coding engine and tool system (layers 6–7)

`NativeCodingEngine` composes the existing controlled layers — there is no
second implementation of any of them:

1. **inspect**: `read_file` via the permissioned `ToolRuntime` (rooted,
   traversal-safe);
2. **propose**: through `ReasoningHub` (`generate`/`repair`) — refused with
   `NEURAL_REQUIRED` without a model;
3. **validate**: `ChangeApplier.dry_run` (paths, secrets, invalid Python,
   size; per-change policy preview; fingerprint) — zero writes;
4. **apply authorized changes**: `ChangeApplier.apply` — every change passes
   the A33 policy gate (ALLOW/DENY/REQUIRE_APPROVAL); DENY is never
   escalated, model-proposed deletes stay rejected (`allow_delete=False`),
   and the engine keeps a run-level `CheckpointManager` for exact rollback;
5. **tests**: the constrained `run_tests` tool (current interpreter, `-B`,
   `-m pytest`, repository-relative paths only), with the same sanitizer as
   `TestDebugLoop`;
6. **failure inspection + repair plans + bounded retries**: §9.

Tool/policy system (layer 7) is unchanged by design: the engine attaches one
`PermissionManager` (agent `forge-native-ai`) to one `ToolRuntime`; file,
terminal, git, browser, and desktop actions all keep flowing through A33.
The engine adds **no** new tool and **no** approval path. It never
self-authorizes: `approved` comes from the caller (CLI flag, cockpit
approval, A33 token) — there is no engine API that flips it.

## 8. Verification (layer 8)

`NativeVerifier.verify(changed_files, diff_text, …)` aggregates:

* **compile/syntax** — `ast.parse` + in-memory `compile()` of the changed
  `.py` files (bounded: ≤400 files, ≤2 MiB each; when nothing changed, a
  deterministic bounded sample so "no issues" is still a measured statement —
  traversal prunes `.git`, `node_modules`, virtualenvs and other vendored
  trees instead of walking them);
* **tests** — real pytest (full suite re-run whenever files were actually
  edited — targeted runs alone can't prove repo-wide health; scoped off
  only for no-edit runs and then labeled `executed=False`);
* **build** — `compileall`, and **lint/type** — only when configured in
  `pyproject.toml` (ruff/mypy); the pipeline's pass-when-unconfigured policy
  is surfaced via `evidence.configured=False`, never hidden;
* **security** — the A32 security gate (secrets, dangerous execution,
  credential files, protected paths) over candidate files;
* **diff validation** — declared paths re-checked independently (unsafe
  paths, `.env`, declared-but-missing, conflict markers). Without a git
  worktree the material half is **skipped and said to be** (never passed).

Aggregation: `all_passed` = AND over *executed* gates; every gate scoped off
by the caller — tests, build, and lint alike — is recorded as
`executed=False, "scoped off"`, never silently dropped; skipped gates appear
in `skipped`; the run status becomes PARTIAL-labeled, and any failed gate
keeps the run failed. A failed check remaining a failure is asserted in the
tests.

## 9. Debug loop (layer 9)

`NativeDebugLoop` runs exactly the required cycle per attempt:
failure → classify (deterministic, via the hub) → collect context
(traceback frames resolved to repo files, read through the permissioned
runtime, bounded to 3 files) → repair plan (hub `repair`; refused without a
neural backend) → validate (dry-run) → authorized repair (apply through the
gate) → verify (re-run the real test command) → repeat, clamped to
`0..10` retries after the initial run.

Every cycle records the classification, evidence lines, proposal provenance
(model/provider/latency), validation issues, policy decisions, retest
result, and the recorded reason. Stop reasons are explicit:
`tests_passed · neural_required · policy_blocked · backend_error ·
invalid_proposal · apply_failed · no_progress · retry_bound_reached ·
not_executed` (`no_progress`: the model repeated an already-failed change
byte-for-byte, so the loop stops instead of burning another retest).

Repairs that *were* applied are recorded on the loop result's
`changed_files`, and the engine merges them into its own change ledger:
repair files are part of the rollback set and part of the verification
scan (security + diff validation), exactly like first-pass edits. A denied
or expired debug run therefore cannot leave half-repaired state on disk.
The A32 `TestDebugLoop` stays what it was for the supervisor; the native
loop adds the memory/verification coupling rather than replacing it.

## 10. Memory, sessions, and the existing stack (layers 10, 11)

Memory (layer 10) writes five categories through the durable
`forge.memory.MemoryStore` (`decisions`, `strategies`, `failures`,
`patterns`, `verification`), one bounded JSON file per entry under
`.forge/memory/native/`. Writes run redaction **first** (shared
`forge.core.report` patterns), refuse the entry outright if secret material
survives redaction, and recall is newest-first and size-bounded. Sessions
and the cockpit approval/event plumbing remain in the A34 control plane;
the engine's run records (`.forge/native/runs/<run-id>.json`) are the join
point the control plane and dataset builder can read.

Model Fabric (layer 11) is untouched and remains the only model
abstraction. Both neural backends receive the fabric object; the engine
never registers providers, never bypasses routing, and never reads
credentials. A future **custom Forge-trained model** plugs in by registering
a provider in the fabric (e.g. a quantized GGUF served locally or a native
loader) — the reasoning interface picks it up automatically (see §12).

Deliberate overlap (disclosed): the Supervisor orchestrates
proposal→apply→test→debug→review→commit for cockpit/control-plane runs;
`NativeAIEngine` orchestrates understand→…→memory→report for native runs on
the same primitives, without commits. Neither reimplements the other's
gates; `forge run --native` chooses the engine, plain `forge run` keeps the
supervisor path byte-for-byte.

## 11. Free-first operation (layer 12)

With no model configured, `forge native-ai "…"`/`forge run --native` still
perform repository analysis, planning, budgeted context, deterministic
orchestration, real test execution, all verification gates, failure
classification, memory, and reporting. Generative steps are refused
explicitly: the report lists them under `skipped_neural`, the plan embeds an
`edit_refusal` with `NEURAL_REQUIRED` and the note "no model was called and
no code was generated; nothing was written", and the exit code is 3.
`forge native-ai status` prints the `[free]`/`[model]` matrix and the exact
backend state.

## 12. How future models connect (layers 5/11/13)

* **Local model (e.g. small quantized model via Ollama already supported by
  the fabric, or a dedicated loader)**: make it reachable/configured, the
  fabric registers it, the hub's local backend becomes generative-ready.
  On a G560-class host, expect inference to be slow or offloaded to another
  machine (`forge compute`); the engine keeps running regardless.
* **Remote model**: register the provider in the fabric (config
  `.forge/models.yaml` / env). The remote backend uses the same interface;
  availability is decided by registration + call-time health feedback;
  secrets never appear in logs or reports.
* **Future Forge-trained model**: produce a dataset from real captured runs
  (§13), validate it, run training **outside** this codebase (no trainer
  ships — `TrainingJobStore.run()` raises `TrainingRuntimeNotConfigured`),
  then register the artifact as a fabric provider and create a version
  manifest. `ModelEvaluator` measures it against real tasks; promotion
  requires a recorded passing evaluation; rollback switches the pointer.
  **Until a real training run produces an artifact, the status surface
  reports `trained_models: 0` and no model is claimed.**

## 13. Training interfaces (layer 13, honest by construction)

| Interface | Real behavior today |
|---|---|
| `DatasetBuilder` | Scans `.forge/native/runs/*.json`; includes **only** runs that ended `COMPLETED`, had `dataset_capture=True`, and contain actual model output text with verification ≠ FAIL. Empty input ⇒ `empty: true` + reason; no files, no synthetic examples. |
| `DatasetValidator` | JSONL schema, byte caps, exact-duplicate counting, secret-pattern rejection (a dataset with credentials is invalid). |
| `TrainingJobStore` | Records jobs against validated datasets; `run()` refuses with `TrainingRuntimeNotConfigured` — jobs are *recorded*, never "succeeded". |
| `ModelEvaluator` / `BenchmarkComparator` | Evaluation needs a fabric with a real model (else refuses); comparisons operate only on recorded measurement summaries. |
| `ModelVersionStore` | Registration requires an existing artifact file; promotion requires a passing recorded evaluation; rollback restores the previous pointer. Manifests + pointer under `.forge/native/training/models/`. |
| `NativeTrainingFabric.status()` | Counts on-disk artifacts — the ground truth, which is zero until training actually runs. |

Dataset capture is opt-in per engine (`dataset_capture=True`) because it is
the only place raw model text may be retained locally (redacted) — the rest
of the system stores measured metadata, never transcripts.

## 14. Desktop + CLI integration (layers 16–17)

* **Panel** — the desktop app's **Native AI** tab renders the persisted
  snapshot (written atomically on every stage transition, so CLI runs are
  visible too): engine state, task state, current stage, reasoning backends,
  model backend, verification state, retry state, files changed, snapshot
  age/freshness, and the capability labels. Rendering lives in
  `forge/native/panel.py` (no tkinter) and is headless-tested; the tab only
  rewrites its text when content changes.
* **CLI** — `forge native-ai` (bare ⇒ status), `forge native-ai status`,
  `forge native-ai test` (deterministic 10-check self-test in a temp
  fixture — nothing in the user's repo changes), `forge native-ai run
  "<task>"` (also `forge native-ai "<task>"`), `forge native-ai plan
  "<task>"` (the planner's grounding + full step list, plan only —
  nothing is executed), and `forge native-ai history [--limit N]
  [--run <id>]` (the persisted run records, newest first, or one full
  record). All accept `--root --project --mode --json --config --approve
  --force --max-debug-retries --context-tokens --no-memory` where
  applicable. `forge run --native` routes the same task
  text through the engine instead of the raw supervisor.
* **Exit codes** — `0` completed · `1` failed (including policy **DENY**,
  which is a refusal, not a pending approval) · `2` blocked (approval
  required and not grantable non-interactively) · `3` needs-model (analysis
  ran; generative steps refused honestly).

## 15. Security posture (layer 15)

Reused permission system, zero new trust surface. The engine: cannot
self-authorize (approval/token always comes from the caller and is re-checked
per write); cannot bypass policy (all writes are `ChangeApplier.apply` under
`PolicyGate`; all reads/tests use the runtime); cannot escape the workspace
(rooted filesystem tools + path validation at three layers: tools,
ChangeSet, verifier); cannot disable security (no API touches permission
levels; mode restrictions even disable *its own* test runner, which reports
`not_executed`); cannot expose credentials (reports/events/memory/run
records all pass through `redact`, secret material is refused at the memory
boundary); cannot claim unperformed work (statuses are derived from executed
steps — see §3, and refusal records are first-class report content).

## 16. Python 3.8 / Windows 7 constraints (layer 14)

* 3.8 grammar everywhere (the repo-wide `test_python38_compat` walks
  `forge/`; a native-specific suite re-checks each file, forbids
  symlink/POSIX-only APIs, and enforces stdlib+forge module-scope imports
  only);
* no new dependencies: the whole engine imports stdlib + forge; pydantic/
  fastapi remain only in the existing optional API surfaces;
* Windows-safe I/O: atomic `os.replace`, `newline="\n"` writes, all paths
  through `pathlib` with posix-relative repository paths;
  drive-letter/backslash inputs are *rejected* at validation boundaries
  rather than interpreted;
* no Win10/11 APIs, no threads at construction (a single background poller
  in the desktop app already exists), bounded subprocess output, and small
  default budgets (1600-token context, 3 retries, 400-file compile sample).

## 17. Start and test

```bash
# on the G560 (or any machine), from the repository checkout:
python -m forge.cli native-ai status
python -m forge.cli native-ai test
python -m forge.cli native-ai plan "fix the add function in calc.py" --root <path>
python -m forge.cli native-ai history --root <path>
python -m forge.cli native-ai "fix the add function in calc.py" --root <path>

# full test coverage for the engine:
python -m pytest tests/test_native_ai_startup.py tests/test_native_ai_planner.py \
  tests/test_native_ai_context.py tests/test_native_ai_reasoning.py \
  tests/test_native_ai_coding.py tests/test_native_ai_verification.py \
  tests/test_native_ai_debugging.py tests/test_native_ai_memory.py \
  tests/test_native_ai_engine.py tests/test_native_ai_training.py \
  tests/test_native_ai_cli.py tests/test_native_ai_desktop.py \
  tests/test_native_ai_windows_compat.py tests/test_native_ai_hardening.py -q

# with a real model later (example: local Ollama already configured):
python -m forge.cli native-ai "add a multiply function to calc.py" \
  --root <path> --approve
```

## 18. Known limitations (stated, not hidden)

1. **No neural inference ships.** Generative steps require attaching a real
   model through the Model Fabric; until then code/repair generation is
   refused by design. The planner's classification is deterministic
   heuristics — genuinely ambiguous phrasing is low-confidence.
2. **No trainer.** Training interfaces record, validate, and refuse; no
   model exists until a real training run produces artifacts (§13).
3. **Model-driven edits still need the A32/A33 stack quality bar** — a bad
   model produces bad proposals; they are validated/gated, not
   semantically corrected by the engine.
4. **No commits** from the native engine; staging/commit stays with
   `GitTool`/the supervisor flow (cockpit/desktop approvals), by design.
5. Context budgets are heuristic (chars/4) and metadata-based, matching the
   existing intelligence layer's estimator, not a real tokenizer.
6. Windows 7 behavior is guaranteed by static/behavioral guards and the
   supported Python 3.8.10 runtime, not by continuous Win7 CI (none exists).
7. Snapshot staleness is reported (age + historical label); the engine is
   per-project single-flight — a second run on the same root overwrites the
   snapshot and can interleave checkpoints, so run one task at a time per
   project (the desktop dispatch queue already serializes tasks).
8. The `run_tests` bound reuses A32's constrained pytest runner only; custom
   test commands stay a supervisor/terminal-approved operation. It inherits
   A32's per-command 30-second timeout: a suite that needs longer reports a
   failed/not-executed test gate honestly — it never silently passes, and
   "make the timeout configurable" is deliberately not a native-engine
   knob (it would be a security-relevant relaxation).
9. Verification of repository health (compile sample, whole-suite tests) is
   bounded for the 2 GB target; a `PASS` means "the executed gates passed",
   with every skipped or scoped-off gate listed by name — not "provably
   correct for all inputs".
