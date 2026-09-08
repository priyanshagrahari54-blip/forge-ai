# A60 — Model Benchmarking

An honest benchmark harness: real prompts, code-judged answers,
persistent history.

## What A60 adds

- `forge/benchmark/harness.py` — a bounded suite of three
  deterministic checks (strict-JSON object, arithmetic answer,
  exact marker echo). Every check sends a real prompt through the
  fabric and is judged by code on the actual response — models
  never grade themselves, and failures are reported as failures.
- `BenchmarkStore` (plane database): every benchmark run is
  recorded (model, provider, per-check results, pass counts,
  latency, actor); history survives restarts.
- Control plane `benchmark_run` (validated model list, ≤5 models,
  unknown names refused) and `benchmark_history` (1–100); audited.
  API `POST /api/v1/benchmarks`, `GET /api/v1/benchmarks`.

## Security notes

- Benchmarks are read-only generation probes: bounded prompts,
  no files, no approvals, no state changes; recorded results
  contain no secrets.

## Testing

`tests/test_a60_model_benchmarking.py` (5): all checks pass against
a provider that answers them, the scripted CSV payload fails all
checks honestly, validation + history, persistence across restarts,
API flow.

A60 result: **5 new tests; full suite 1328 passed, 2 skipped** (A59
baseline: 1323 passed, 2 skipped).
