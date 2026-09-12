# A81 — Controlled Self-Improvement

Forge analyzes its own performance, identifies weaknesses, proposes
improvements, tests candidate changes in isolation, and evaluates them
against mandatory gates. It is deliberately **not** unrestricted
autonomous self-modification: the loop never writes to the live checkout,
never commits, never merges, and cannot approve its own changes.

```
analyze ─▶ propose ─▶ validate (guardrails) ─▶ isolated candidate
   ▲                                                   │
   │                       apply change ◀──────────────┘
   │                            │
   │        test candidate + baseline ─▶ compare ─▶ reject regression
   │                            │
   │   acceptance: tests · security · architecture · regression ·
   │               improvement · guardrails · POLICY APPROVAL (human)
   │                            │
   └── ledger ◀── parked for operator ──▶ (operator) approve ─▶ apply
                                                    │            │
                                                    └─ rollback ◀┘
```

Module: `forge/self_improvement/`.

## 1. Self-analysis (`analysis.py`, `evidence.py`)

`EvidenceCollector` gathers **observed** facts — every item names its
source and carries the raw measurement — from the systems Forge already
maintains. A missing source yields no evidence, never synthetic evidence.

| Evidence kind         | Sources                                                          |
|-----------------------|------------------------------------------------------------------|
| `failure`             | A34 run store (FAILED / ROLLED_BACK, ≥2 repair retries), A59 ledger singles, self-dev history |
| `test_failure`        | a real `pytest -q -rfE` run (`--run-tests`), parsed node ids      |
| `latency`             | test-suite duration, run p95, A62 metric p95 over threshold       |
| `model_performance`   | Model Fabric router history: per-model calls / failures / latency |
| `routing_mistake`     | fallback events; models routed despite ≥50 % failure rate         |
| `repeated_error`      | A59 failure ledger fingerprints with count > 1                    |
| `agent_failure`       | A51 agent run log entries with `success=false`; ledger `agent`    |
| `resource_bottleneck` | worker saturation, queue backlog, low disk, thread count          |

`SelfAnalyzer.derive_weaknesses` turns evidence into ranked `Weakness`
records (`category`, `severity`, `metric`, measured `value`, `target`,
`evidence_ids`, `affected_files`, `suggested_action`, `risk_notes`).
Affected files are resolved from real imports (`tests/x.py → forge/…`)
and paths mentioned in error text; protected paths are never suggested.
The report is persisted to `.forge/self_improvement/analysis.json`.

## 2. Improvement proposals (`proposals.py`)

Each `ImprovementProposal` must contain: **hypothesis**, **evidence ids**,
**expected benefit** (metric: baseline → target), **risk** + risk notes,
**affected files**, and a **test plan**. `validate_proposal` rejects
proposals that are incomplete, cite unknown evidence, declare no files or
more than 12, have no test plan, or touch guardrail-protected paths.
Weaknesses rejected twice before are suppressed (no retry loops).

## 3. Candidate system (`candidate.py`)

`CandidateRunner` copies the repository (application content only — no
`.git`, `.forge`, venvs, caches) into two temp directories: a **baseline**
and a **candidate**. A *change producer* returns `{path: content}` for the
candidate; the production producer asks the routed model through
`CoderAgent` *inside the candidate copy*. Before anything is written:

- files must be declared in the proposal (undeclared → rejected);
- ≤12 files, ≤200 KB, valid Python syntax;
- guardrails pass on paths **and** content (against the originals);
- paths cannot escape the candidate directory.

Evaluation runs pytest on both copies, `compileall`, the A32 security
verification on the changed files, and architecture checks (no new
dependency cycles, no protected paths, no new low-level imports, no new
direct dependency on security-policy internals). `CandidateComparison`
computes improvement (test failures removed, or a caller-supplied
lower-is-better metric probe) and records every regression: new test
failures, lower pass count, build failure, security findings, architecture
issues, >1.5× slowdown, metric worsening.

## 4. Acceptance (`acceptance.py`)

`SelfImprovementAcceptance.decide` is a conjunction of mandatory gates:
`tests`, `security`, `architecture`, `regression`, `improvement`,
`guardrails`, `policy`. One passing gate never overrides a failing one; a
no-op change fails `improvement`.

`policy` requires a `PolicyApproval` bound to the **candidate id and its
change fingerprint**, issued by a named human actor (`forge`/`self`/`auto`
are refused), unexpired, and the session mode must permit writes (SAFE and
LOCKED never accept). A candidate that passes every technical gate but has
no approval is parked as `awaiting_approval` — it is **not** accepted.

## 5. Improvement ledger (`ledger.py`)

Append-only JSONL at `.forge/self_improvement/ledger.jsonl` with a
sequence number and SHA-256 hash chain (`verify_chain()` detects
tampering). Kinds: `analysis`, `proposal`, `candidate`, `decision`,
`pending`, `applied`, `rejected`, `rollback`, `iteration`, `note`. Payloads
are secret-redacted (pattern + key based). Bounded to 5000 entries.

## 6. Rollback (`rollback.py`)

`RollbackManager.apply` snapshots the exact original bytes (content-
addressed) plus "did not exist" markers under
`.forge/self_improvement/snapshots/<candidate>/` before writing. `rollback`
restores exactly those files, deletes files the candidate created,
preserves all unrelated work, never runs `git reset`, and refuses
corrupt snapshots.

## 7. Bounded iteration

`HARD_MAX_ITERATIONS = 10` caps every run regardless of caller input
(`--iterations 999` → 10). A run also stops at the first candidate that is
accepted or parked for approval, when no admissible proposals exist, or
when proposals are exhausted.

## 8. What Forge must never do (`guardrails.py`)

Checked on the proposal, on the produced change set, and again at apply
time. Constants only — nothing is configurable — and the guardrail module,
acceptance, ledger and rollback are themselves protected.

| Invariant                                             | Enforcement |
|-------------------------------------------------------|-------------|
| never remove its own security controls                | removal of `PolicyGate`, `PermissionManager`, `Guardrails`, `VerificationPipeline`, … from a file is a violation |
| never increase its own permissions                    | `PermissionLevel.* → SAFE`, sensitive ops mapped to SAFE, `OperationMode.AUTONOMOUS` / `mode="autonomous"` |
| never disable policy                                  | `policy = None`, `gate = None`, `disable_/skip_/bypass_(policy|gate|security|…)` |
| never bypass approvals                                | `approved = True`, `auto_approve = True`, `require_*=False`, self-minted `PolicyApproval(` |
| never modify credentials                              | credential-like filenames (`.env`, `*credentials*`, `id_rsa`, `.pem`, `token`, …) refused **even with authorization**; secret patterns in content refused |
| never modify protected files without authorization    | `.git/`, `.forge/`, `.github/`, `pyproject.toml`, `forge/security/`, approvals/sessions/deps, change applier, checkpoint, acceptance, `forge/final/`, `forge/autonomy/`, credentials module, and the A81 guardrail/acceptance/ledger/rollback modules — unless a `ProtectedFileAuthorization` naming the exact path and a human `authorized_by` is supplied |

Additional safety: apply refuses if a live file changed since the
candidate was evaluated (the tested change must be the applied change);
approvals are single-use per session in the API; nothing is ever committed
(`committed: false` is recorded in the ledger).

## 9. CLI

```bash
forge self-analyze [--run-tests] [--json]
forge self-improve [--iterations N] [--run-tests] [--targets tests/x.py …] [--json]
forge self-improve --approve CAND-… --as <you> [--reason "…"]
forge self-improve --approve CAND-… --as <you> --apply CAND-…   # apply to working tree
forge self-improve --rollback CAND-…
forge self-improve --discard CAND-…
forge self-status [--json]
```

`--apply` only works together with `--approve` for the same candidate in
the same command (approvals are fingerprint-bound and single-use). The
apply result explicitly says **NOT committed**.

## 10. Dashboard and API

Cockpit view **Self-Improvement** (`#/selfimprove`): stats, actions
(Analyze / Evaluate one candidate), candidates awaiting approval with
per-gate badges and Approve → Apply / Discard, proposals (hypothesis,
evidence, benefit, risk, files, test plan), candidates, accepted
improvements (with Roll back), rejected improvements (failed gates and
reasons), weaknesses, evidence, and the guardrail invariants.

API (`/api/v1/self-improvement…`, session-authenticated, CSRF-checked,
rate-limited, audited under `self_improvement`):

| Method | Path | Notes |
|--------|------|-------|
| GET    | `/self-improvement` | dashboard payload |
| POST   | `/self-improvement/analyze` | `{run_tests}` read-only, allowed in safe mode |
| POST   | `/self-improvement/run` | `{iterations ≤10, run_tests, targets}`; 403 in safe/locked |
| POST   | `/self-improvement/candidates/{id}/approve` | `{reason, change_fingerprint}`; approver = session actor |
| POST   | `/self-improvement/candidates/{id}/apply` | 409 `APPROVAL_REQUIRED` without a prior approve in this session |
| POST   | `/self-improvement/candidates/{id}/rollback` | |
| DELETE | `/self-improvement/candidates/{id}` | discard pending |

## Relationship to A26–A30 / A58

The earlier `forge.self_development` package (analysis of TODOs, cycles,
test mapping; executor that committed accepted candidates) remains for
compatibility, but the `forge self-*` CLI now drives A81. A81 differs in
three ways: evidence is runtime performance rather than static markers,
candidates are built in isolated copies rather than the live tree, and
acceptance requires an explicit human policy approval and never commits.

## Testing

`tests/test_a81_self_improvement.py` (28) — evidence sources, analysis
honesty, proposal fields/validation/suppression, candidate isolation,
regression detection, undeclared-file / syntax rejection, all acceptance
gates incl. approval binding and modes, ledger chain + redaction, rollback
exactness + corrupt-snapshot refusal, full loop park → approve → apply →
rollback, drift refusal, hard iteration bound, and one test per "never"
invariant.

`tests/test_a81_self_improvement_api.py` (8) — auth, dashboard shape,
full API flow (apply refused before approval, wrong fingerprint refused,
never committed, single-use approvals, rollback), safe profile denial,
validation/404s, discard, live evidence sources, cockpit view hooks, and
the production model producer routing through the Fabric inside the
isolated candidate.

A81 result: **36 new tests; full suite 1859 passed, 3 skipped** (baseline
before A81: 1823 passed, 3 skipped).
