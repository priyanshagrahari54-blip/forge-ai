# Forge AI — Fresh Audit (Phase 0 forensics)

Date: 2026-09-09 · Auditor: autonomous engineering session `arena/01a0863b-forge-ai`
Scope: full repository, all merged/open PRs, branch graph, A01–A80 implementation truth.

Status vocabulary used throughout: `COMPLETE` · `PARTIAL` · `SIMULATED` · `MISSING` · `BROKEN` · `INSECURE`.

---

## 1. Repository state

| Item | Value |
|---|---|
| Clone HEAD (start of audit) | `16007dc` "⚡ Bolt: Optimize ContextPack additions and symbol file checks (#10)" |
| Working branch | `arena/01a0863b-forge-ai` (clean tree at audit start) |
| `main` (local + origin) | `16007dc` |
| Repository | `priyanshagrahari54-blip/forge-ai` (GitHub) |
| Shallow clone | yes — deepened with `git fetch --unshallow` during audit |
| Python | 3.11 (repo requires >=3.11), pytest 9.1.1 in `.venv` |
| Source | 60,881 LOC Python across 225 files (`forge/` + `tests/`) |
| Test baseline (pre-change) | **1403 passed, 2 skipped** (176 s) |
| Docs | 44 stage docs in `docs/A*.md`, README.md, `.forge/project.yaml` |
| CI | `.github/workflows/` present (workflow inspected during audit) |

## 2. Branch graph (as fetched)

```
main  16007dc (bolt #10) ── 16007dc = b821ce1 + bolt diff
  │
  ├─ b821ce1  Merge PR #9  (A35–A49 master build; head of arena/01a080c1-forge-ai at the time = fb664b4)
  │     └─ fb664b4 … first-parent chain A32→A80 (arena/01a0774e, 01a0777f, 01a07aa1, 01a07b06, 01a080c1)
  │
  ├─ origin/arena/01a080c1-forge-ai = 1805f13  (PR #9 branch tip, still advancing)
  │     └─ MERGE of PR #11 (head ba0d486 = 7f1787a + ba0d486)   ← 9 real-provider gaps
  │
  ├─ origin/arena/01a085e8-forge-ai = ba0d486  (PR #11 head)
  └─ other remotes: bolt/*, jules-*, feature/forge-self-development-engine-*, forge-autobuild
```

Open PRs at audit time: **#2** (jules autonomous loop, OPEN), **#8** (bolt TestMapper stem indexing, OPEN).

## 3. PR #11 forensics (the critical finding)

PR **#11 "feat: implement all 9 real provider gaps"** (head `ba0d486`, two commits: `7f1787a`,
`ba0d486`) was **merged on 2026-09-09 into branch `arena/01a080c1-forge-ai` (merge commit
`1805f13`), NOT into `main`**. `main` does **not** contain a single line of PR #11.

- PR #11 base = `fb664b4` = the exact commit `main` absorbed via the PR #9 merge (`b821ce1`).
- Bolt commit `16007dc` (on `main` after PR #9) touches `forge/intelligence/context.py` and
  `context_query.py`, two files PR #11 also edits → real 3-way conflict potential.
- PR #11 delta vs its base: **29 files, +2146/−88**:

| Area | New/changed code |
|---|---|
| A44 real OpenAI connector | `forge/collaboration/openai_connector.py` (+123); `connectors.py` +`openai` |
| A47 real web research | `forge/research/web.py` (+225); `research/engine.py` +96 (ask_web/fetch_url) |
| A48 remote compute | `forge/compute/remote.py` (+219); `engine.py`, API `backend=` param |
| A50 training / fine-tune | `forge/agents/training.py` (+359); `evolution.py` docstring |
| A64 production deploys | `forge/deployment/production.py` (+243) |
| A36 real voice | `forge/voice/openai_stt.py` (+106), `openai_tts.py` (+101) |
| A39 real vision | `forge/vision/real.py` (+136); `pipeline.py` +`openai-vision` |
| A40 computer use w/ real vision | control-plane + vision config wiring |
| Control plane | `control_plane.py` +271/−88 (voice stack, training pipeline, deploy backends, vision) |
| Tests added | **NONE** (only 3 assertion tweaks in existing tests) |
| Docs | A36/A39/A40/A44/A47/A48/A50/A64 updated; `launch_cockpit.py` added |

**PR #11 quality findings (code read, not merged yet at audit start):**

1. `INSECURE` — `forge/compute/remote.py::_execute_ssh`: `ssh -o StrictHostKeyChecking=no`,
   Python code interpolated into `python3 -I -c f"'{code}'"` (quote-breakout on any `'` inside
   user code), creates `tempfile.NamedTemporaryFile(delete=False)` that is never used and never
   deleted (leak). No known_hosts, no host allowlist, no user/port parsing, no audit of target.
2. `INSECURE` — `forge/deployment/production.py::SSHDeployer.deploy`: `rsync -avz --delete`
   against an env-configured host with no allowlist, no approval flag for destructive sync, no
   strict SSH options, no rollback/verification beyond return code.
3. `INSECURE` — `forge/research/web.py::fetch_page_content`: plain `urllib` fetch of arbitrary
   user-supplied URLs → classic **SSRF**; no scheme/host/IP validation, no redirect
   re-validation, no size cap on response (reads 100 KB but only after unbounded headers), no
   content decompression limit.
4. `INSECURE` — `forge/agents/training.py`: `collect_training_data`/`export_jsonl`/
   `start_fine_tuning` upload **raw run outputs to OpenAI without any secret/credential/PII
   scan**; no authorization object; default permits external upload when a key exists
   (violates fail-closed default).
5. `BROKEN` (spec vs code) — promotion/retirement logic (`should_promote` /
   `should_retire`): docstring says "no consecutive failures in last 3 runs" / "retire after …
   5 consecutive failures", but code only checks `last_outcome != "FAILED"` and
   `runs >= 5 and last_outcome == "FAILED"` — it never inspects outcome history (the main
   `AgentEvolution` ledger does not even store history, only counters + `last_outcome`).
6. `INSECURE`-ish/honesty — `openai_connector.ask()` returns failures as **successful text
   payloads** (`"[openai] API returned HTTP 401…"` inside `content`, `simulation=False`), i.e. a
   failed provider request is represented as a model response; no explicit
   SUCCESS/PROVIDER_ERROR/TIMEOUT/RATE_LIMITED/AUTH_ERROR/POLICY_DENIED/UNAVAILABLE state.
   Same pattern in `web.py` (bare `except Exception: return []`), `training.py` (`{"error":…}`
   dicts OK but not classified), vision/real.py and STT/TTS (to be verified in merge review).
7. Colab/Modal/vision/voice providers lack health-status vocabulary
   (AVAILABLE/DEGRADED/RATE_LIMITED/AUTH_FAILED/UNAVAILABLE/MISCONFIGURED).
8. No new tests for any of the 9 areas; docs updated before merge have no post-merge CI proof.

## 4. A01–A80 current status (evidence-based, pre-PR#11-merge)

See `.forge/audit/A01-A80_MATRIX.md` for the full matrix. Summary:

- **A01–A31 (audited core)** — implementation exists and is tested (symbol/dependency context,
  context pack/query, plan pipeline, model contract, supervisor). Oldest layers; README/docs
  describe them; tests `test_repository_*`, `test_context_*`, `test_plan_*`, `test_self_*`.
- **A32/A33 (autonomous core + permission platform)** — `COMPLETE`-intent: Supervisor,
  ChangeSet, PolicyGate, approvals, audit, resources; extensive tests incl. attack tests
  (`test_a33_attack.py`, `test_a32_rollback_git.py`). No `git reset --hard` rollback.
- **A34–A40 (desktop/voice/session/orchestration/vision/computer-use)** — implemented with
  honest `SIMULATED` providers as defaults; real providers MISSING on main (PR #11 provides).
  Computer use exists with deterministic simulator + guards (see `forge/computer/`).
- **A41–A45 (cockpit, voice/general conversation, AI-to-AI, council)** — implemented;
  A44 simulated connector default; council uses model fabric.
- **A46 (model fabric)** — `COMPLETE` on main: provider/registry/router/health/streaming/
  telemetry/credentials; local/Ollama/OpenAI/mock; health degrades after consecutive failures.
- **A47 research** — repository-intelligence engine `COMPLETE` on main; web research MISSING
  (PR #11) and, as written, SSRF-unsafe.
- **A48 compute** — local subprocess execution with TERMINAL gating, quotas — real for local;
  remote backends MISSING on main (PR #11 provides them insecurely).
- **A49–A57 (agent creation/evolution/execution/teams/memory/skills/lifecycle/packaging/
  governance)** — implemented and tested; A50 evolution ledger real but history-less;
  lifecycle transitions gate execution.
- **A58–A60 (self-development, failure learning, benchmarking)** — implemented; A58 loop uses
  real runs + model-selected candidates.
- **A61–A65 (hardening/observability/performance/deployment/backup)** — implemented; A64
  deployment manager local staging + validated manifests; production backends MISSING on main
  (PR #11 provides them with destructive-sync risk).
- **A66–A70 (plugin SDK, command palette, cockpit navigation, autonomy levels, UX)** —
  implemented.
- **A71–A80 (final gates)** — implemented; A80 gate = rollout pass AND ≥1 real SUCCEEDED run.
  Gap: it does **not** distinguish `ARCHITECTURE_COMPLETE` from `PRODUCTION_READY` and does not
  validate provider state; GO can be recorded with every external provider unconfigured
  (simulated-only) — real-capability check missing.

## 5. Real vs simulated capabilities (main, before PR #11 reconciliation)

| Capability | Main (pre-work) |
|---|---|
| Model inference | REAL local Ollama + REAL OpenAI (key-gated), deterministic fallbacks — routed via fabric |
| External AI collaboration (A44) | SIMULATED only |
| Web research / URL fetch (A47) | repository research REAL; web SIMULATED/MISSING |
| Remote compute (A48) | local REAL; Colab/SSH/Modal MISSING |
| Agent training / fine-tuning (A50) | ledger REAL; external training MISSING |
| Production deployment (A64) | local staging REAL; Docker/K8s/SSH/Fly MISSING |
| Voice STT/TTS (A36) | SIMULATED only |
| Vision (A39) | SIMULATED only |
| Computer use (A40) | simulator-driven REAL loop w/ vision SIMULATED |

## 6. Security findings (pre-change scan of main + PR #11)

Severity key: CRITICAL / HIGH / MEDIUM / LOW.

1. `CRITICAL` (PR #11, not on main) — SSRF in `fetch_page_content` (arbitrary URL fetch).
2. `CRITICAL` (PR #11, not on main) — SSH remote compute: host-key checking disabled + code
   quote-injection transport + temp-file leak.
3. `HIGH` (PR #11) — `rsync --delete` w/o destination allowlist / approval.
4. `HIGH` (PR #11) — training data uploaded without secret/PII scan or authorization.
5. `HIGH` (PR #11) — provider failure reported inside success-shaped payloads
   (`simulation=false` but not a real success).
6. `MEDIUM` (main) — voice STT/TTS/vision/web capability endpoints label "simulation" or
   "real" per config but no per-provider health state is surfaced (cockpit cannot show
   RATE_LIMITED/AUTH_FAILED etc.).
7. `MEDIUM` (main) — A50 evolution ledger keeps no outcome history; any future
   promotion/retirement rule based on runs history is unimplementable as-is (PR #11 tries and
   gets it wrong).
8. `LOW` — `docs/` and README claim consistency issues (e.g. A44 docs say simulated-only
   while main connectors expose only simulated; will re-audit after merge).

No secrets, `.env`, or credential files found in the repo (verified by `.gitignore` + scan;
re-verified at the end of the audit).

## 7. Test findings

- Baseline green: 1403 passed / 2 skipped.
- Strength: A32/A33 attack + invariants + rollback + transaction suites are real integration
  tests (in-memory control plane, real policy evaluation, real git staging, real subprocess
  compute). Level-4 real-provider tests are env-gated already for Ollama/OpenAI
  (`test_ollama_live.py`, model-fabric credential tests).
- Weakness (Phase 20): PR #11 ships 0 new tests; vision/voice/collaboration tests assert
  simulator behavior only; no SSRF/SSH/training-data tests anywhere.

## 8. Architecture findings

- Single `ControlPlane` monolith (5,988 LOC) — coherent but large; all subsystems hang off it.
- Model fabric is clean and canonical for A46; A44/A47/A48/A50/A64 connectors added by PR #11
  are independent stacks — they must be wired through the same policy/audit/approval paths the
  control plane already provides (PR #11 routes already gate on policy; verify).
- PR #11 introduces one protocol drift: `connectors.py::AVAILABLE_CONNECTORS`,
  `vision/pipeline.py::AVAILABLE_PROVIDERS`, voice provider names must stay consistent with
  config validation lists in `ControlConfig`.

## 9. Deployment findings

- `.github/workflows/` runs pytest + compileall on push (see workflow).
- Deployment backends on main: local staging only (`forge/deployment/manager.py`, 295 LOC,
  manifest-validated, rollback via checkpoint).
- `launch_cockpit.py` added by PR #11 for a quick local server (kept, audit contents).

## 10. Provider findings

- Model fabric: provider health (healthy/degraded/unhealthy + recheck) is REAL and wired into
  routing (unhealthy providers avoided). This is the model to imitate for the new connectors.
- New external connectors must expose structured health and must never be auto-promoted.

## 11. Recommended fixes (priority order)

1. Reconcile PR #11 into the working branch (3-way merge vs `main`), resolving
   `forge/intelligence/context*.py` conflicts with bolt #10.
2. Implement `forge/security/ssrf.py` (validation chain incl. DNS resolution, redirect
   re-validation, port/scheme policy, size limits) and route ALL url fetching through it.
3. Rewrite SSH compute transport: strict host-key verification via `known_hosts`, explicit
   host allowlist + user + port, code over stdin, no temp-file leak, timeouts, cancellation.
4. Deployment: destination allowlist, `--delete` only with explicit approval, strict ssh opts.
5. `TrainingDataPolicy` (fail-closed secret/credential/PII scan + explicit authorization) on
   every export/upload path; default DENY external training upload.
6. Fix A50 promotion/retirement on real outcome history (extend ledger w/ recent outcomes,
   helpers `consecutive_failures/recent_outcomes/promotion_eligible/retirement_eligible`) with
   boundary unit tests; make docs and code agree.
7. Classify every real-provider response with explicit states
   (SUCCESS/PROVIDER_ERROR/TIMEOUT/RATE_LIMITED/AUTH_ERROR/POLICY_DENIED/UNAVAILABLE) — never a
   failure inside a success payload; structured health per provider
   (AVAILABLE/DEGRADED/RATE_LIMITED/AUTH_FAILED/UNAVAILABLE/MISCONFIGURED).
8. Extend A80 gate: GO/NO_GO + machine-readable reasons + real-provider state validation and
   ARCHITECTURE_COMPLETE vs PRODUCTION_READY classification.
9. Add Level-2/3 tests for every hardened path (SSRF, SSH, deploy, training policy, evolution,
   connectors) while keeping default CI free/deterministic; add Level-4 env-gated tests.
10. Re-run full suite, compileall, static greps, secret scan; then rewrite
    `.forge/audit/A01-A80_MATRIX.md` and produce `.forge/audit/FINAL_DEEP_AUDIT.md`.

---

## 12. Remediation close-out (executed in this session, HEAD `57d4425`)

The ten recommendations of §11 were executed on this branch; status below. Detailed evidence,
verification results, the static-analysis ledger, the dependency census, and the honest final
status block live in `.forge/audit/FINAL_DEEP_AUDIT.md`.

| # | Recommendation | Status | Evidence |
|---|---|---|---|
| 1 | Reconcile PR #11 onto current main (3-way vs bolt #10) | DONE — `f05984d` | merge diff reviewed; full suite green |
| 2 | `forge/security/ssrf.py` + route ALL url fetching through it | DONE — `632b734` | `tests/test_ssrf.py` (4 levels) |
| 3 | Strict SSH compute transport (known_hosts, arg-list, bounded drains, no temp leak) | DONE — `632b734` | `tests/test_a48_compute_security.py` |
| 4 | Deploy destination allowlist + approval-gated `--delete` | DONE — `632b734` | `tests/test_a64_deployment_security.py` |
| 5 | Fail-closed `TrainingDataPolicy`, default DENY external upload | DONE — `c72484c` | `tests/test_a33_data_policy.py` |
| 6 | A50 promotion/retirement on real outcome history | DONE — `c72484c` | `tests/test_a50_agent_evolution.py` |
| 7 | Classified provider states + structured health everywhere real | DONE — `fd384a5` | `tests/test_a44_provider_states.py` |
| 8 | A80 gate: GO/NO_GO + reasons + provider-state validation + capability classification | DONE — `fd384a5` | `tests/test_a80_gate_redesign.py` |
| 9 | Level-2/3 tests for every hardened path; env-gated Level-4 | DONE | suites above; 3 skips are the env-gated lives |
| 10 | Full suite + compileall + static greps + secret scan + rewrite matrix + FINAL_DEEP_AUDIT | DONE — `57d4425` | 1593 passed / 3 skipped (188.8 s); ruff defect classes clean; matrix + this file + `FINAL_DEEP_AUDIT.md` |

Static-analysis defects found and fixed in the close-out (`57d4425`): 8 undefined names
(including three latent runtime NameErrors: vision-DENY `PolicyDeniedError`,
SSH-deploy missing `import os`, SearXNG missing `parse_and_validate` import), 6 dead
locals, 4 closure loop-variable captures, 14 `subprocess.run` calls made explicit
(`check=False` where returncode is deliberately handled). Remaining `ruff` output is
documented style/boundary-guard noise (see FINAL_DEEP_AUDIT §3.1).

Final label (exactly one): **`ARCHITECTURE_COMPLETE`** in this zero-key environment —
`PRODUCTION_READY` per `forge/final/gate.py` as soon as any real provider is configured
and `AVAILABLE`.
