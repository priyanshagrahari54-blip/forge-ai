# A48 — Compute / Colab

Managed local computation: code cells that really execute, with
honest resource accounting and the same permission posture as every
other controlled action.

## What A48 adds

- `forge/compute/engine.py` — `ComputeEngine` executes cells in a
  fresh `python -I -c` subprocess against the project working
  directory. Status (`succeeded`/`failed`/`timeout`) comes from real
  exit codes and real timeouts; output is captured and truncated to
  4000 chars with an explicit `output_truncated` flag. Quotas
  (cells + total seconds + per-cell timeout) are enforced **before**
  execution and refusals leave no side effects. Backend is labeled
  `local-python` and the status payload states that no remote/GPU
  backend exists in this build.
- Control plane `compute_execute()` — gated by TERMINAL/execute
  policy exactly like terminal commands (ALLOW rules must pin the
  concrete executable and exact args — the existing A33 hardening is
  unchanged; REQUIRE_APPROVAL files a task-scoped approval and only
  a redeemed single-use token releases the run; DENY fails closed).
  Every run is audited with real status/timing.
- `compute_status/compute_history` + session-isolated compute
  approvals; API `/api/v1/compute/*` (execute rate-limited, status,
  history, approvals, approve/deny).
- Cockpit Compute view (cell input, result, quota, history) with the
  usual UI contracts.
- `ControlConfig` gains `compute_max_cells` (20),
  `compute_max_seconds` (300), `compute_cell_timeout` (30).

## Security notes

- Compute inherits the terminal permission model; nothing executes
  without a policy decision, and approvals bind to the session/task.
- Timeouts bound runaway cells; quotas bound session usage; output
  is bounded so cells cannot flood memory.

## Testing

`tests/test_a48_compute.py` (10): real success/failure/timeout
execution, quota enforcement before execution, output truncation and
bounded history, gated plane runs with audit, DENY fail-closed,
approval round trip with spent-token refile, API boundaries, cockpit
contracts.

A48 result: **10 new tests; full suite 1263 passed, 2 skipped** (A47
baseline: 1253 passed, 2 skipped).
