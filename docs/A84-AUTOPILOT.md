# A84 — AutoPilot: hand Forge a complex project, it completes it

Before A84, Forge's engines were complete but unjoined: the Architect
(A83) produced reviewable plans, the PlanStore gated execution behind
approval, and the Supervisor (A32) executed one guarded transaction —
but nothing turned an approved plan into finished software. The
AutoPilot closes that loop.

```
requirement ──> plan ──> approve ──> checkout ──> frontier loop ──> done
 (operator)   (Architect)  (recorded,   (fingerprint     │
                            fingerprint-   gate)          ├─ architect task: verified plan evidence
                            bound)                        ├─ research task: evidence-bound memory entry
                                                        ├─ coding task: FULL Supervisor transaction
                                                        ├─ review task: ReviewGate (+ bounded repair)
                                                        ├─ security task: security gate (+ repair)
                                                        ├─ performance task: measured baseline
                                                        └─ release task: tests+build gate (+ repair)
```

## Usage

```bash
# From a requirement straight to a completed project:
forge auto "Build a todo list CLI tool with JSON storage and tests" \
    --root ./myproject --init-git

# Plan only; approve nothing, execute nothing:
forge auto "..." --dry-run

# Continue an interrupted or blocked run:
forge auto --resume <plan-id> --root ./myproject

# Drive an already-approved plan through the same engine:
forge engineer plan "..." && forge engineer approve <plan-id>
forge engineer execute <plan-id> --auto
```

`forge auto` exits `0` when every task is done, `1` when tasks remain
blocked (it prints the resume command), and `2` on refusal (no model,
no git repository, invalid mode, empty requirement).

## What each task becomes

The Architect's tasks carry roles; the AutoPilot executes each role the
honest way for that role. Nothing runs "the coding pipeline" that is not
coding work, and no verification task is marked done without a recorded
verdict.

| Role          | Execution                                                            | Done when                                        |
|---------------|----------------------------------------------------------------------|--------------------------------------------------|
| architect     | The plan's specifications are checked, not assumed                   | every spec has id + area + a way of checking it  |
| research      | `ResearchEngine` over the repo; result recorded in `ProjectMemory`   | findings **or their honest absence** recorded    |
| coding, documentation, hardware, … | One full Supervisor transaction (model → code → test/debug → review → security → acceptance → checkpoint → commit) | run accepted; files committed |
| review        | Deterministic `ReviewGate` over everything changed since run start; a failing verdict triggers one bounded Supervisor repair, then re-review | verdict `APPROVE`              |
| security      | `VerificationPipeline.security` over changed files; same repair path | zero findings                                     |
| performance   | `BenchmarkRunner` measured `min_runs` times (capped at 3)            | a real baseline exists and passes                 |
| release       | The repository's own tests + build gates; same repair path           | both gates pass                                   |

## Failure, retries, and resume

* A failed task is retried up to `--max-task-retries` extra times; every
  retry receives the previous failure (`PREVIOUS ATTEMPT FAILED …`) so
  the model repairs instead of repeating.
* Inside each task, the Supervisor's bounded test/debug loop still runs
  (`--max-debug-retries`).
* When retries are exhausted the task is marked **blocked** and its
  dependents simply never become ready; the run reports exactly which
  tasks remain and why.
* Progress is persisted after **every** task: task statuses in the plan
  file itself, attempt history under `.forge/autopilot/run-<plan>.json`.
  `forge auto --resume <plan-id>` picks up the frontier where it
  stopped, gives blocked tasks one more pass (an interrupted run's
  blockers are usually exactly what the operator went to fix), and never
  re-runs a completed task.

## The approval contract still holds

* Execution starts only through `PlanStore.checkout_for_execution` —
  the plan that runs is byte-for-byte the plan that was approved.
* On resume, the plan's content must still match the approved
  fingerprint once *execution bookkeeping* (task statuses, plan
  lifecycle status) is normalised away. Any real edit — a smuggled
  specification change, a reworded task — invalidates the run:
  re-approve before executing.
* The AutoPilot itself writes nothing into the target tree. Every byte
  of project content goes through the Supervisor transaction; the
  AutoPilot's own bookkeeping stays under `.forge/`.

## Pre-flight, honestly

`forge auto` refuses before planning when:

* there is no real (non-fallback) code model — it prints the same
  actionable diagnosis as `forge doctor` (`--force` bypasses the gate,
  and the run then fails honestly at the first coding task);
* the target root is not a git repository (Forge commits each accepted
  task; use `--init-git` to initialise one);
* the permission mode cannot write (`safe`/`locked`).

## Limitations (implemented / not implemented)

* Implemented: full plan→approve→execute→persist loop, role-aware
  execution, bounded retries with failure context, resume, approval
  fingerprint guard, CLI (`forge auto`, `forge engineer execute --auto`),
  and a deterministic end-to-end test suite
  (`tests/test_autopilot.py`).
* Not implemented (deliberately): parallel task execution (tasks with
  disjoint dependencies run sequentially — correct first, fast later),
  mid-run interactive approval (use `forge serve` cockpit for that),
  and any model-side replanning (a blocked plan is reported, not
  silently re-scoped).
