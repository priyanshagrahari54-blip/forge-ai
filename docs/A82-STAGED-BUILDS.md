# A82 — Staged Builds: project sections, ordered stages, verified execution

Staged builds give Forge the workflow this project needed: **different
sections for different projects, each with its own stages**, where the
operator pastes **all stage prompts plus the roadmap and blueprint up
front**, and Forge works through them **one stage at a time** — stage 1
first, completed **for real**, and only then stage 2.

## Mental model

```
Build project "Shop API"          Build project "Blog"          ...
┌──────────────────────┐          ┌──────────────────────┐
│ roadmap              │          │ roadmap              │
│ blueprint            │          │ blueprint            │
│  1. Auth      ✓ done │          │  1. Posts    running │
│  2. Cart      locked │          │  2. Comments locked  │
│  3. Checkout  locked │          │  ...                 │
└──────────────────────┘          └──────────────────────┘
```

- A **build project** is a section: name + description + roadmap +
  blueprint + an ordered list of stages. Sections live inside one
  control-plane workspace (the session's project) and are isolated per
  project.
- A **stage** is one ordered step: title + prompt + status + linked runs
  + verification evidence.
- Execution is **strictly sequential**: stage N+1 can never start until
  stage N is *verified complete*. There is no skip, no reorder, no
  "mark complete" button.

## The one-at-a-time prompt

When stage N runs, the service assembles exactly one requirement:

```
STAGED BUILD — stage N of M: <title>
=== ROADMAP ===            (whole plan — context, truncated honestly to fit)
=== BLUEPRINT ===          (architecture/contracts — truncated honestly to fit)
=== CURRENT STAGE N ===    (this stage's full prompt — never truncated)
=== COMPLETED STAGES ===   (short summaries of verified earlier stages)
=== RULES ===              (real implementation, tests, no fake completion)
```

Later stages are **never included**, so the model cannot race ahead.
The assembly is budgeted to the control plane's 8000-char requirement
limit; the current stage prompt is always complete, roadmap/blueprint
share the remainder with explicit `[… truncated N chars …]` markers,
and a snapshot (char counts, truncation flags, sha256) is stored on the
stage for evidence.

## Real completion, never faked

Each stage runs through the **existing, unmodified** control-plane
pipeline — the real Supervisor transaction:

```
requirement → plan → model → code (ChangeSet + policy gate) → tests →
debug/repair → review → security → acceptance → checkpoint → commit
```

A stage becomes `completed` **only** when its linked run verifies:

1. run status is `SUCCEEDED` (the worker sets this solely from the
   Supervisor's `accepted` flag), **and**
2. the stored report's `acceptance.accepted` is `True`, **and**
3. `acceptance.failed_gates` is empty.

Anything else — failed/cancelled runs, a `SUCCEEDED` run with missing
or refused acceptance — becomes `failed` with the reason recorded, and
the next stage stays locked. Consequences:

- No API or UI control completes a stage by hand; completion is
  derived from run records on every read (sync).
- Completed stages are immutable (no edit/delete/rerun) and never
  re-synced, so late mutations cannot rewrite history.
- Only one active run per build project; a second `run-next` while a
  stage runs is a `409 Conflict`.
- Failed stages can be edited and retried (`run-next` retries the
  first incomplete stage); every attempt is recorded (`attempts`,
  `runs[]`).
- Evidence snapshots (acceptance, trimmed test/review/security/build
  results, files changed, checkpoint, model, duration, docs snapshot)
  are stored on completion and shown in the cockpit.

## Backend layout

| Path | Responsibility |
|---|---|
| `forge/staged/models.py` | `BuildProject`, `BuildStage`, `StageStatus`, `assemble_stage_requirement` |
| `forge/staged/store.py` | `StagedStore`: `staged_projects` / `build_stages` tables in the control-plane SQLite DB (survives restarts) |
| `forge/staged/service.py` | `StagedBuilds`: validation, sequential gating, run submission, sync-from-runs, strict `verify_run_accepted` |
| `forge/api/routes_staged.py` | HTTP API (schemas in `forge/api/schemas.py`, `Staged*`) |
| `forge/cockpit/web/` | `Builds` view: sections list, roadmap/blueprint editors, stage timeline, run controls, evidence |

`StagedBuilds` uses only public control-plane operations
(`submit_task`, `runs.get`), so all existing guarantees (queue,
workers, approvals, audit, checkpoints, exact-file commits) apply
unchanged.

## API reference (`/api/v1`)

| Method & path | Purpose |
|---|---|
| `GET /builds` | List sections with progress counts |
| `POST /builds` | Create a section (`name`, `description`, `roadmap`, `blueprint`) |
| `GET /builds/{id}` | Full board: docs + ordered stages + progress + `current_position` + `active_run_id` |
| `PATCH /builds/{id}` | Update name/description/roadmap/blueprint (docs apply to future stages) |
| `DELETE /builds/{id}` | Delete a section (refused while any stage runs) |
| `POST /builds/{id}/stages` | Append stages (`stages: [{title, prompt}, …]`, up to 50) |
| `PATCH /builds/{id}/stages/{n}` | Edit a pending/failed stage |
| `DELETE /builds/{id}/stages/{n}` | Delete a pending stage (positions renumber) |
| `POST /builds/{id}/run-next` | Run the first incomplete stage (retries failures) |
| `POST /builds/{id}/stages/{n}/run` | Run stage N (gate-checked: earlier stages must be verified) |
| `GET /builds/{id}/stages/{n}/evidence` | Stage evidence + linked-run summary |

Errors reuse the standard envelope: `404 NOT_FOUND` for unknown or
foreign ids (existence never leaks cross-project), `409 TASK_CONFLICT`
for gating violations (`stage is still running`, `not verified
complete yet`, `already verified complete`), `400 INVALID_REQUEST` for
validation failures. Run endpoints share the `task_create` rate-limit
group.

## Limits

`name ≤ 120`, `description ≤ 2000`, `roadmap/blueprint ≤ 20000` each,
`title ≤ 160`, `prompt 1…4000`, `≤ 50` stages per build,
`≤ 50` builds per workspace project.

## Cockpit

`Builds` (nav → Workspace) shows every section on the left and the
selected section on the right: verified-progress bar, `Run next stage`
(plus per-stage Run/Retry on the current stage only), roadmap &
blueprint editors, an add-stage form, and the ordered stage timeline
with locks (`Locked — waiting for stage N…`), live-run links, failure
reasons, and verified-evidence cards linking back to the accepted run.
The board polls every 4 s but never re-renders while you are typing.

## Tests

`tests/test_staged_builds.py` (26 tests): assembly budgets and
one-at-a-time isolation, store round-trip/renumber, validation and
project isolation, all gating transitions, the full verification
matrix (including `SUCCEEDED`-without-evidence → `failed`), evidence
trimming, the HTTP API, and an end-to-end test that runs **two real
Supervisor transactions** (scripted model, real code/tests/gates path)
and asserts stage 2 stays locked until stage 1 verifies.
