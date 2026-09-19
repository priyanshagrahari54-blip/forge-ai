# Forge AI — production readiness report

Date: 2026-09-19 · Branch: `arena/01a0b955-forge-ai` (commit `3ad4a27`) · PR #46

Everything below was observed in this checkout or against the live service.
Nothing is claimed from a summary; states are the ones the runtime itself
reports. Reproduce with the commands in the last section.

## 1. Verifications (observed)

| Check | Result |
| --- | --- |
| `python -m pytest -q` | **3246 passed, 6 skipped, 0 failed** (764 s) — baseline before this work was 112 failures |
| `node --test tests/web/*.test.cjs` | **11 passed, 0 failed** (4 of them failed at `HEAD`) |
| `scripts/verify_production_readiness.py` | 1000 registered specialists / 40 roles (25 each), 40/40 representatives routed through a real `ModelFabric`, worker identity + capabilities survive restart with `live_count = 0` before a heartbeat |
| Runtime capability states | LIVE 5 · READY 1 · SIMULATED 2 · BLOCKED 1 · ARCHITECTURE 6 (details below) |
| Live service `/api/v1/health` | `{"status":"ok","auth_mode":"production","worker":true,"projects":1}` — **service is up** |
| Live service `/city.html` | 200 (already deployed) |
| Live service `/voice-playback.js` | 404 — **this commit is not deployed yet** |

## 2. Implementations

**Runtime truth** (`forge/models/runtime_verification.py`,
`runtime_monitor.py`, `runtime_monitor_service.py`, `configured_runtime*.py`)
- A provider that exposes no model-list probe is no longer treated as an
  outage; an inconclusive probe is absence of evidence and never downgrades a
  model or refreshes `last_checked`.
- The monitor service tolerates duck-typed registries instead of reporting
  its own crash as a model outage.
- Capability snapshots keep the honesty contract (`configured_is_not_live`,
  `live_requires_verified`, `simulation_is_never_live`,
  `unknown_provider_is_never_assumed_ready`) and expose `id`/`state` aliases.

**1,000-specialist fleet** (`forge/agents/frontier_fleet.py`)
- All 40 specializations register before any repeat at 1,000 slots.
- 23 of 40 specializations previously required labels no model can advertise
  (`web`, `database`, `multilingual`, `deployment`, …). `Model` rejects
  unknown capabilities and the router never relaxes capability requirements,
  so those specialists failed closed with *"no registered model supports
  capabilities […]"* — registered but not executable. Only canonical Model
  Fabric capabilities are required now; the declared labels stay on the
  registration and in request metadata.
- Regression: one representative per specialization executes end-to-end
  through a real `ModelFabric`.

**Execution integrity** (`forge/server/executor.py`, `forge/server/workers.py`,
`forge/core/lease_guard.py`, `forge/workers/admission.py`,
`forge/workers/execution_gateway.py`, `forge/compute/worker_registry.py`)
- Worker settlement is the single canonical `task.completed` emitter with
  bounded inference provenance; the supervisor no longer publishes a
  provenance-less duplicate.
- The lease guard checks ownership synchronously before publishing a result.
- Execution receipts expose `ok`; admission accepts a heartbeat TTL; stale
  handling only excludes revoked workers.

**API, voice, cockpit** (`forge/api/app.py`, `forge/capabilities/*`,
`forge/cockpit/web/*`, `forge/conversation/*`, `forge/voice/*`)
- `/capabilities` and `/provider-links` no longer demand a `request` query
  parameter; `served_route_paths(app)` restores route introspection.
- Model-backed conversation answers only with a runtime-verified live model
  and always carries model/provider provenance.
- One shared browser playback module (`voice-playback.js`) is used by both
  the full cockpit and the lightweight home page, with an **Enable Voice**
  gesture that satisfies the browser autoplay policy; the cockpit also primes
  playback on its enable toggle.
- Voice commands ask before executing again (`confirm=true`), matching the
  A42 contract and the UI copy.
- AI City is reachable from the main navigation (`#/city`) and renders backend
  state only ("REAL BACKEND EVENTS / NO SYNTHETIC PROGRESS"); its bearer token
  is kept in `sessionStorage`, not `localStorage`.

## 3. Live providers

| Provider | State | Evidence |
| --- | --- | --- |
| `local` (deterministic fallback) | registered, `available=True`, not runtime-verified | fabric snapshot |
| `ollama` | registered; endpoint unreachable here → capability state **BLOCKED** | fabric snapshot |
| `openai`, web research | **ARCHITECTURE** — no credential present | capability snapshot |

Runtime-verified models: **0** in this environment. The fleet therefore routes
through the fabric but cannot call a real remote model here.

## 4. Registered agents and executed representatives

- Registered: **1000 logical specialists**, **40 specializations × 25**,
  covering planner, researcher, architect, coder, frontend, backend, database,
  devops, debugger, tester, reviewer, security, performance, refactor,
  documentation, API, data, ML, vision, audio, game, OS, browser,
  computer-use, UX, product, QA, compliance, privacy, localization, math,
  science, finance, legal, prompt, agentic, memory, orchestration,
  integration, release.
- Executed through real ModelFabric routing: **40/40 representatives** (one per
  specialization), using an in-process **SIMULATED** provider. This proves the
  routing and execution plumbing; it is **not** a live remote model call.

## 5. Background execution and persistence

- Worker identity and capabilities survive a restart; restored workers are
  **not** live (`live_count = 0`) until a fresh heartbeat.
- Stale running tasks are recovered into `RECOVERY`, leases are re-issued, and
  a lease-losing attempt cannot publish (covered by the suite:
  `test_lease_guard.py`, `test_worker_registry.py`, server restart/reconnect
  tests).
- No artificial polling deadline is used for background work.

## 6. Blocked external dependencies

**1. Real model execution** — BLOCKED
- *Reason*: no provider credential or reachable model endpoint exists in this
  environment.
- *Already implemented*: ModelFabric routing, capability matching, runtime
  verification gate, failover chain, provenance on every result.
- *Exact requirement*: set `OPENAI_API_KEY` / `ANTHROPIC_API_KEY`, or a
  reachable `OLLAMA_BASE_URL`, then re-run
  `scripts/verify_production_readiness.py`; the state flips from CONFIGURED to
  LIVE only when a model is runtime-verified.

**2. Render deployment of this commit** — BLOCKED
- *Reason*: no Render API key or deploy hook is available here, and this
  session may only push `arena/01a0b955-forge-ai`. The live service answers
  `/voice-playback.js` with 404, i.e. the deployed build predates commit
  `3ad4a27`.
- *Already implemented*: Dockerfile, `forge_web` entrypoint, CI workflow,
  `/api/v1/health`, safe static cockpit mount.
- *Exact requirement*: merge PR #46 into the branch Render tracks (or trigger
  "Deploy latest commit" for `forge-ai-server` in the Render dashboard), then
  confirm `/voice-playback.js` returns 200 and `/api/v1/health` still reports
  `status: ok`.

**3. WhatsApp / email / phone calls** — MISSING (not implemented)
- *Reason*: no adapter exists anywhere in the codebase (`grep -ri whatsapp`
  finds only the voice intent and its task-description string).
- *Already implemented*: intent recognition (`send_whatsapp`, `send_email`,
  `make_call`) and confirm-before-execute, which creates a normal task
  requirement.
- *Exact requirement*: add a real provider (e.g. WhatsApp Business Cloud API
  or Twilio) behind the existing task/agent path; until then the capability is
  reported MISSING and no surface claims it works.

## 7. Reproduce

```bash
.venv/bin/python -m pytest -q                       # 3246 passed, 6 skipped
node --test tests/web/*.test.cjs                    # 11 passed
.venv/bin/python scripts/verify_production_readiness.py
.venv/bin/python scripts/verify_production_readiness.py --json | jq .
```
