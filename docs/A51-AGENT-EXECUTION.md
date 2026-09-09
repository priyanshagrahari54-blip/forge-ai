# A51 — Agent Execution

Runtime-defined agents really run tasks — through the same executors
and gates as built-in agents.

## What A51 adds

- `forge/agents/runner.py` — `AgentRunner` runs a defined agent
  through a real executor: bound definitions only (unbound specs are
  refused with an explanation), supported roles coding/planning/
  research, bounded requirement, per-agent run log capped at 20.
- Control plane `agent_run()` — the `Resource.AGENT / execute` policy
  gate decides synchronously (ALLOW / DENY fail-closed /
  REQUIRE_APPROVAL round trip with spent-token refiling); execution
  then runs on a worker thread as a **real recorded Run**
  (`agent-run-*` rows in the run store, terminal status + files +
  output recorded), so change-set approvals filed during the run
  resolve through the standard project-scoped approval flow.
- Real executors per role:
  - `coding` → `CoderAgent` over the project with the fabric and the
    standard change-application layer; in ASSISTED semantics the
    change set files a real approval, the operator approves, and the
    same run applies the changes.
  - `planning` → deterministic `TaskRequirementExtractor`
    capabilities.
  - `research` → real bounded file counts over the project root.
- API: `POST /api/v1/agents/{name}/run`, `GET
  /api/v1/agents/{name}/runs`, `GET /api/v1/agents/{name}/runs/{run_id}`
  (pending/finished/failed polling), agent-run approvals endpoints.

## Security notes

- Runs are policy-gated before dispatch and each executor operates
  inside the existing permission/runtime gates (the coding executor
  cannot write without operator approval under ASSISTED).
- Failures are recorded honestly on the run record and audited.

## Testing

`tests/test_a51_agent_execution.py` (7): unbound refusal, real
research counts, real planning extraction, real coding change-set
through the approval flow (files verified on disk), DENY fail-closed,
approval round trip with token replay refiling, API boundaries.

A51 result: **7 new tests; full suite 1283 passed, 2 skipped** (A50
baseline: 1276 passed, 2 skipped).
