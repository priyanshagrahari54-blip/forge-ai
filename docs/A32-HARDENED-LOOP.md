# A32 — Production-Hardened Autonomous Engineering Loop

This document describes the rebuilt A32 autonomous engineering loop: the full
path from a real requirement to a real, safe commit, with every control
point, its configuration, and its honest status.

```text
requirement → supervisor → plan → agents → Model Fabric → coder
→ ChangeSet (validate) → policy gate (authorize) → apply
→ test → debug/repair (bounded) → retest → independent review
→ security → build/lint → benchmark → acceptance → checkpoint
→ commit (accepted files only)
```

Failure at any point: structured error → checkpoint rollback of candidate
files only → report. Unrelated user files are never touched.

## Proposal vs execution

A32 separates *proposing* work from *performing* it:

- **Proposal (no write approval needed):** inspect the repository, build
  intelligence, plan, select agents, route and call the model, validate the
  ChangeSet structurally, preview policy decisions (`dry_run`), run the
  constrained test suite, and run read-only verification.
- **Execution (policy authorization required):** every file write, every
  repair write, and the final commit. Each passes the PolicyGate with the
  operation, path, tool, risk, and requested capability visible, and each
  decision is recorded in the run's event log.

| Mode | Reads/tests | Low-risk writes | High-risk/sensitive writes | Commit |
| --- | --- | --- | --- | --- |
| `safe` | yes | DENY | DENY | DENY |
| `assisted` (default) | yes | with approval | with approval | with approval |
| `autonomous` | yes | low-risk auto | with approval | with approval |
| `locked` | reads only | DENY | DENY | DENY |

`approved=True` satisfies `REQUIRE_APPROVAL`; it can never override `DENY`,
and a missing approval is never treated as approval.

## Control points

### 1. Controlled ChangeSet engine (`forge/tools/change_applier.py`)

- Structural `CodeChange` entries: `create` / `modify`, plus `delete` behind
  an explicit operator gate (`allow_delete=True` **and** approval).
- Path rules: repository-relative POSIX, no `..`, no `.git`, no `.forge`,
  no backslashes, no absolute paths.
- Content rules: secret/credential/`.env` rejection, 2 MiB bound, Python
  syntax validation.
- Optional old-state guards (`expected_old_hash` / `expected_old_content`):
  a change applies only against the exact file state it was built for.
- `dry_run()` validates a proposal with zero writes; `fingerprint()`
  identifies a proposal deterministically (order-independent).
- Structured `ChangeError` values (`code`, `path`, `message`) alongside the
  legacy string list; every applied path is recorded.

### 2. Permission / policy gate (`forge/security/policy_gate.py`)

Every change is authorized **before** modification with the full request
context: operation, path, tool, risk, and requested capability. Decisions:

- `ALLOW`, `DENY`, `REQUIRE_APPROVAL`.
- Modes: `safe` (read-only), `assisted` (approval-required, default),
  `autonomous` (low-risk writes auto-approved; sensitive operations and
  high-risk writes still need approval), `locked` (no modifications).
- `DENY` (blocked operations, protected paths, safe/locked modifications)
  cannot be overridden by approval and is recorded, never bypassed.

```python
from forge.security.permissions import OperationMode, PermissionManager
from forge.security.policy_gate import PolicyGate

gate = PolicyGate(PermissionManager(mode=OperationMode.AUTONOMOUS))
outcome = gate.evaluate(operation="write_file", path="app.py",
                        tool="write_file", risk="LOW", capability="coding",
                        approved=False)
assert outcome.decision.value == "ALLOW"
```

### 3. Model → coder → ChangeSet (`forge/agents/coder.py`)

- Production path routes through the Model Fabric (`fabric=`); the legacy
  `router=` path is preserved unchanged.
- Structured schema: `summary`, `changes`, `tests_to_run` (`tests` alias),
  `reasoning_summary`, `risk_level`, `risks`. Per-change `risk`, `old_hash`,
  and `old_content` guards thread into the ChangeSet engine and policy gate.
- Invalid JSON, unsafe paths, secrets, bad risk labels, and model-proposed
  deletes reject the response. Caller-supplied change shortcuts do not
  exist: `request.metadata["changes"]` is ignored (proven by test).

The Model Fabric is the canonical production route
(`CoderAgent → Model Fabric → provider`); the `router=` argument is a legacy
compatibility adapter preserved for pre-existing callers, not a second
routing algorithm. Successful coder responses record which route served them
(`routing: fabric | legacy-router`).

### 4. Test / debug / repair loop (`forge/agents/debugger.py`)

- Runs the relevant tests (`tests_to_run` from the coder) or the full suite;
  the acceptance gate always runs the full suite regardless.
- Captures command, exit code, and stdout/stderr per execution; each failure
  yields a structured `FailureReport`, and every retry carries a recorded
  reason. Retries are hard-bounded (max 10).
- Repairs are model output like any other change: they validate through the
  ChangeSet engine and authorize through the policy gate. A rejected repair
  fails honestly with no partial write — a failure is never reported as a
  pass.

### 5. Independent review gate (`forge/security/review.py`)

- Deterministic, severity-typed findings (`INFO`…`CRITICAL`) producing
  `APPROVE` / `REQUEST_CHANGES` / `BLOCK`. `HIGH`/`CRITICAL` always block.
- `ReviewPolicy(max_medium_allowed=0)` (default, strict) makes any `MEDIUM`
  finding request changes; callers may configure a `MEDIUM` budget.
- An optional model-driven reviewer contributes findings through the Model
  Fabric (`review` capability); it never softens deterministic blockers.

```python
from forge.security.review import ReviewGate, ReviewPolicy

gate = ReviewGate(".", policy=ReviewPolicy(max_medium_allowed=2))
```

### 6. Security verification (`forge/security/verification.py`)

Real checks, no hardcoded scores: secret/cloud/database/private-key patterns,
`eval`/`exec`, `os.system`/`os.popen`, `shell=True`, `sh`/`bash -c`, path
traversal, `.env` files, credential data files, private-key material
(`.pem`/`.key`/`.p12`/`.pfx`, `id_rsa`, …), unsafe declared paths, and
protected repository files. Any finding fails the gate and blocks acceptance.

### 7. Acceptance engine (`forge/core/acceptance.py`)

One central `AcceptanceDecision` over tests, build, lint, review, security,
benchmark, permissions, rollback availability, and the changed set. It names
every failed gate (`failed_gates`) and carries measured `metrics` (finding
counts, file counts, execution evidence). One passing component can never
override a failed mandatory gate. An unconfigured lint checker passes only
under the explicit, documented pass-when-unconfigured policy, recorded in
`metrics["lint_executed"]`.

### 8. Checkpoints and rollback (`forge/tools/checkpoint.py`)

- Before a change set applies, the checkpoint captures exact original bytes
  plus restore metadata (`existed`, `sha256`, `size`, `mode`) per path.
- Rollback restores **only** candidate files and deletes only
  declared-as-new candidate files. Never `git reset --hard`, never touches
  unrelated or untracked user work (proven by test).

### 9. Safe Git (`forge/tools/git.py`)

- No `git add .` / `git add -A` on autonomous paths: only the exact accepted
  paths are staged, and the staged set is verified to equal the candidate
  set before commit.
- Staging rejects `.git`, `.forge`, `.env`, credential-like files, and
  private-key material.
- `commit_accepted()` refuses to commit unless the acceptance gate
  succeeded — refusal happens *before* staging.

### 10. Supervisor integration (`forge/core/supervisor.py`)

`Supervisor.run()` is the single central controller for the autonomous loop:
plan → agents → model → controlled code → test/debug → review → security →
benchmark → acceptance → checkpoint → explicit commit or rollback. There is
no parallel duplicate orchestration path for autonomous engineering (the
self-development loop reuses the same coder/debugger/verification
infrastructure for its own candidate workflow).

### 11. Observability (`forge/core/report.py`)

Every run returns a structured `TaskReport`: stages, agents, model/provider,
context fingerprint, files read/changed, commands run, gate results,
checkpoint id, retries, duration — plus:

- an ordered `events` log (`task_started`, `agents_selected`,
  `model_selected`, `change_proposed`, `permission_decision`,
  `change_applied`, `test_executed`, `test_failed`, `repair_attempted`,
  `review_result`, `security_result`, `benchmark_result`,
  `acceptance_result`, `rollback`, `commit`),
- measured per-phase `timings`,
- real `model_latency_seconds` and token counts when providers report them
  (`None` when the routing history carries no token data — never fabricated),
- full-payload secret redaction: API keys, passwords, tokens, private keys,
  cloud keys, and database URLs are masked, so reports are safe to persist
  or ship.

## Model Fabric compatibility

A32 uses the existing Model Fabric exclusively — no second routing system.
Local models, the deterministic mock provider (tests), and optional remote
providers all work through `ModelRequest`/`ModelResponse` with capability
selection, health, latency/reliability tracking, cost metadata, and
privacy/local policies. The core suite needs no paid API.

## Status

| Area | Status | Evidence |
| --- | --- | --- |
| ChangeSet engine (validate/dry-run/fingerprint/guards/gated delete) | implemented + tested | `tests/test_a32_changeset.py`, `tests/test_a32_change_applier.py` |
| Policy gate (ALLOW/DENY/REQUIRE_APPROVAL, 4 modes) | implemented + tested | `tests/test_a32_policy_gate.py`, `tests/test_a32_permissions_mode.py` |
| Model→coder→ChangeSet (schema, guards, no shortcuts) | implemented + tested | `tests/test_a32_coding_pipeline.py`, `tests/test_a32_coder_schema.py` |
| Test/debug/repair (ChangeSet repairs, reports, targeted runs) | implemented + tested | `tests/test_a32_debug_loop.py` |
| Independent review (+ configurable MEDIUM policy) | implemented + tested | `tests/test_a32_gates.py`, `tests/test_a32_review.py` |
| Security verification (real checks) | implemented + tested | `tests/test_a32_gates.py` |
| Acceptance engine (failed gates, metrics) | implemented + tested | `tests/test_a32_acceptance_safety.py`, `tests/test_a32_acceptance.py` |
| Checkpoint/rollback exactness | implemented + tested | `tests/test_a32_acceptance_safety.py` |
| Safe Git (exact staging, acceptance-enforced commit) | implemented + tested | `tests/test_a32_acceptance_safety.py` |
| Supervisor integration | implemented + tested | `tests/test_a32_e2e.py`, `tests/test_a32_failure_matrix.py` |
| Observability (events, timings, redaction) | implemented + tested | `tests/test_a32_observability.py` |
| Failure matrix E2E | implemented + tested | `tests/test_a32_failure_matrix.py` |
| Approval at the policy boundary (modes, commit gating) | implemented + tested | `tests/test_a32_approval.py` |
| Constrained test execution without write approval | implemented + tested | `forge/runtime/defaults.py` (`run_tests`), `tests/test_a32_approval.py` |
| Rollback/Git staging hardening | implemented + tested | `tests/test_a32_rollback_git.py` |
| CSV+tests+docs E2E through the mock fabric provider | implemented + tested | `tests/test_a32_csv_e2e.py` |
| Live-model (Ollama) autonomous E2E | implemented, opt-in, not run here | `tests/test_ollama_autonomous_e2e.py` (skipped without endpoint) |
| Model-proposed deletes | not implemented by design | rejected; deletes are an explicit operator path |
| Multi-model consensus as acceptance authority | not implemented | available as a library (`forge/models/consensus.py`), not wired into acceptance |

## Deliberate limitations

- Autonomous runs never delete files implicitly; operator-driven deletes go
  through the gated `delete` action with explicit approval.
- The model-driven reviewer runs only when a review-capable model is
  available through the fabric; the deterministic gate always runs.
- Lint/type checks execute only when the target project declares them
  (`ruff`/`mypy` in `pyproject.toml`); otherwise the omission is recorded,
  not fabricated.
- A useful autonomous run needs a capable model provider (local Ollama or a
  configured remote); the dependency-free fallback refuses to invent source
  code. Proprietary models are never fabricated.
- Live-provider verification was not run in this environment (no Ollama
  endpoint); the deterministic suite is the verification evidence.
