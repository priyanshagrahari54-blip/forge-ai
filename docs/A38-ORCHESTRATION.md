# A38 — Multi-Agent Orchestration

A38 turns Forge's agents from isolated executors into a coordinated
team. One user requirement becomes a validated, dependency-ordered plan
of agent steps that the control plane executes with real budgets,
policy-gated dispatch, structured inter-agent messages, and a full
per-step report with evidence.

```text
REQUIREMENT
 ↓
ORCHESTRATION API (/api/v1/orchestrations)
 ↓
PLANNER (deterministic capability matching, never invents agents)
 ↓
AGENT TEAM (planner · architect · researcher · coder · tester ·
debugger · reviewer · security · performance · documentation · git)
 ↓
DAG EXECUTION (parallel where safe, chain where requested, bounded
workers, per-step attempts/timeouts, cooperative cancellation)
 ↓
POLICY GATE (Resource.AGENT / execute per dispatch; coder/debugger
writes flow through the existing ChangeSet + approval system)
 ↓
REPORT (per-step outcomes, structured messages, evidence, summary)
```

## What A38 adds

| Area | Location | Notes |
|---|---|---|
| Orchestration engine | `forge/core/orchestrator.py` | `MultiAgentOrchestrator`, `OrchestrationPlan/Step`, `StepOutcome`, `OrchestrationReport`, `AgentMessage`, `DispatchQuery` |
| Policy vocabulary | `forge/security/policy.py` | `Resource.AGENT` with `execute`/`message` operations |
| Requirement extractor | `forge/agents/requirements.py` | Added `research`, `architecture`, `performance`, `git` capability keywords |
| Records | `forge/control/orchestrations.py` | SQLite-backed, session-scoped orchestration records (survive restarts) |
| Control plane | `forge/control/control_plane.py` | Submit/list/get/cancel, worker, default team, approval callback + sink, budgets via `ControlConfig` |
| API | `forge/api/routes_orchestrations.py`, `schemas.py` | Submit/list/get/cancel + per-orchestration approvals |
| Cockpit | `forge/cockpit/web/` | Orchestrations view: submit, live status, plan, per-step report, approve/deny |
| Tests | `tests/test_a38_*.py` | 33 tests across 4 suites |

## Planning and execution

- **Planning is deterministic**: `CapabilityAgentPlanner` matches the
  requirement's keywords against the registered team. A requirement
  matching nothing yields an empty plan, and execution reports
  `PLAN_REJECTED` — a fabricated team is never produced.
- **The team is real**: every executor performs its actual job over the
  project root — capability planning, deterministic architecture
  proposals, bounded repository inventory, model-driven coding and
  debugging through the permissioned runtime, bounded real test
  collection, deterministic review findings, a bounded secret-pattern
  security scan, measured import timings (performance), a docs
  inventory, and real git status.
- **Parallel where safe, sequential where required**: independent steps
  run concurrently (bounded by `orchestration_max_workers`, default 3);
  `chain=true` makes each step depend on its predecessor
  (planner → … → git). Dependency failures skip dependents with an
  explicit `SKIPPED` outcome; the report never hides it.
- **Budgets are real**: per-step attempts (`orchestration_max_attempts`,
  default 2), optional per-step timeout (`orchestration_step_timeout`,
  default 120 s), and cooperative cancellation via the A34 run-control
  machinery (pause/cancel semantics).

## Security model

- **Dispatch is permission-gated**: every step evaluates
  `Resource.AGENT / execute` with agent identity `forge-orchestrator`
  (so a human can decide its approvals — the store enforces approver ≠
  agent). `DENY` fails the step closed; `REQUIRE_APPROVAL` files a
  session-bound request and waits for the operator (bounded by the
  approval timeout); stale, spent, or mis-scoped tokens fail closed.
- **Writes stay on the existing path**: coder/debugger change sets flow
  through the A33 ChangeSet engine and approval system exactly like
  supervisor runs — with ASSISTED mode, file-changing steps always wait
  for an operator decision even under an ALLOW policy.
- **Structured communication**: agent results are carried as
  `AgentMessage` records (sender, receiver, task id, type, content,
  evidence, confidence, timestamp) — critical decisions never rely on
  uncontrolled free text.
- **Records are session-scoped** at the API boundary: a session can
  list/get/cancel only its own orchestrations (cross-session ids map to
  NOT_FOUND), and orchestrations survive backend restarts.

## API

| Method | Effect |
|---|---|
| `POST /api/v1/orchestrations` | Submit `{requirement, chain}` (rate-limited) |
| `GET /api/v1/orchestrations` | Session's orchestration records |
| `GET /api/v1/orchestrations/{id}` | Record + plan + full report |
| `POST /api/v1/orchestrations/{id}/cancel` | Cooperative cancel (rate-limited) |
| `GET /api/v1/orchestrations/{id}/approvals` | Pending dispatch/change-set approvals |
| `POST .../approvals/{id}/approve` | Decide + mint token (rate-limited) |
| `POST .../approvals/{id}/deny` | Decide deny (rate-limited) |

## Testing

- `tests/test_a38_orchestrator.py` (14) — deterministic planning, plan
  validation (cycles/unknown agents/duplicate ids), dependency order
  and failure-skip, proven parallel execution, attempt budgets,
  timeouts, DENY fail-closed, approval round trips and stale-token
  rejection, structured messages, events, cancellation.
- `tests/test_a38_plane.py` (9) — full control-plane flows: read-only
  analysis chains, the coder chain that applies a real change after
  approval, DENY blocking writes, plan rejection, cancellation,
  cross-session isolation, restart persistence, audit, the AGENT policy
  vocabulary.
- `tests/test_a38_api.py` (5) — API lifecycle, auth/validation
  boundaries, deny decisions, decision conflicts, cancel.
- `tests/test_a38_ui.py` (5) — cockpit view contracts + live endpoints.

A38 result: **33 new tests; full suite 1138 passed, 2 skipped** (A37
baseline: 1105 passed, 2 skipped).
