# A73–A80 — Final Security Gate, Benchmark Gate, Commit Gate,
# Memory Gate, Self-Evaluation, Rollout, Loop, and Go/No-Go

The tail of the final acceptance arc. Every gate is derived from
live state or a real action; none is pre-marked passed, and none
can bypass PolicyGate, ChangeSet, or the permission system.

## A73 — Final security gate

`forge/final/security_gate.py` — go/no-go from live audits: policy
structural findings, secret-pattern hits across projects,
constraining posture (a policy that is all-ALLOW fails the gate;
fail-closed passes), bounded sessions. API
`POST /api/v1/final/security`.

## A74 — Final benchmark gate

`forge/final/benchmark_gate.py` — runs the real A60 harness
(code-judged checks on real model responses, never self-graded)
and requires every registered model to have been benchmarked plus
`min_passed` checks (default 1, max 8; invalid → 400). API
`POST /api/v1/final/benchmark`.

## A75 — Final commit gate

`forge/final/commit_gate.py` — read-only git readiness: work tree,
author identity, HEAD. The gate never commits or pushes. API
`POST /api/v1/final/commit`.

## A76 — Final memory gate

`forge/final/memory_gate.py` — durable memory is live: the A37
session-memory table exists and the A58 failure ledger answers.
Read-only. API `POST /api/v1/final/memory`.

## A77 — Final self-evaluation

`forge/final/self_evaluation.py` — grades from recorded facts:
run outcomes, benchmark results, hardening posture, secret hits,
failure-ledger totals. Grades: healthy / attention / degraded /
unproven (no evidence yet — never a claimed capability). API
`POST /api/v1/final/self-evaluation`.

## A78 — Final rollout gate

`forge/final/rollout.py` — acceptance + security + benchmark +
commit + memory gates plus verification of the acceptance smoke
run's evidence. Overall pass requires every gate to pass; no gate
is skipped. API `POST /api/v1/final/rollout`.

## A79 — Final loop

`forge/final/loop.py` — bounded improvement loop (1–5
iterations): each iteration is a full real re-evaluation, and the
loop always stops at the bound. API `POST /api/v1/final/loop`.

## A80 — Final go/no-go

`forge/final/gate.py` — go requires the full rollout to pass AND
at least one genuinely SUCCEEDED run on record in the project
(the end-to-end demonstration). Every requirement is listed with
its evidence. API `POST /api/v1/final/gate`.

## Smoke idempotency (A71 extension)

Re-running acceptance on a project that already has a SUCCEEDED
run cannot produce a genuine change (the pipeline correctly
refuses empty change sets), so the smoke re-verifies for real:
every recorded file must still exist and the live test suite must
still pass. A broken suite fails the smoke honestly.

## Testing

`tests/test_a73_final_security_gate.py` (5) and
`tests/test_a74_a80_final_gates.py` (9): clean-plane pass,
planted-secret failure, all-ALLOW posture failure, code-judged
benchmark thresholds, honest refusal without any successful run,
bounded loop behavior, API flows.

A72–A80 result: **14 new tests; full suite 1403 passed, 2
skipped** (A72 baseline: 1389 passed, 2 skipped).
