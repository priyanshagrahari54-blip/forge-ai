# Forge AI — production readiness report

Date: 2026-09-20 · Branch: `arena/01a0b955-forge-ai` · PR #46

Sections 1-4 were verified on 2026-09-19; the multimodal, outbound-channel
and CI sections were added on 2026-09-20 and are marked with the run that
produced them.

Everything below was observed in this checkout or against the live service.
Nothing is claimed from a summary; states are the ones the runtime itself
reports. Reproduce with the commands in the last section.

## 1. Verifications (observed)

| Check | Result |
| --- | --- |
| `python -m pytest -q` | **3296 passed, 6 skipped, 0 failed** (665 s) — baseline before this work was 112 failures |
| `node --test tests/web/*.test.cjs` | **11 passed, 0 failed** (4 of them failed at `HEAD`) |
| `scripts/verify_production_readiness.py` | 1000 registered specialists / 40 roles (25 each), 40/40 representatives routed through a real `ModelFabric`, worker identity + capabilities survive restart with `live_count = 0` before a heartbeat |
| Runtime capability states | LIVE 5 · READY 1 · SIMULATED 2 · BLOCKED 1 · ARCHITECTURE 6 (details below) |
| Live service `/api/v1/health` | `{"status":"ok","auth_mode":"production","worker":true,"projects":1}` — **service is up** |
| Live service `/city.html` | 200 (already deployed) |
| Live service `/voice-playback.js` | 404 — **this commit is not deployed yet** |
| Real model execution | **900/1000 specialists produced real model output** (0 exceptions, 0 empty, 51,047 tokens, 515 s); the other 100 need capabilities no configured model provides. Evidence: `docs/evidence/fleet-real-run-2026-09-19.json` |
| Provider quota failover | exhausted-provider classification, same-model endpoint rotation, cooldown skip and recovery: **19 tests**, incl. real HTTP 429/500 servers |
| Multi-server routing | numbered env / JSON endpoint lists parsed and deduped; same model pools, different models get their own providers and tiers (**8 tests**) |
| Fine-tuning | a **real LoRA fine-tune** ran against the served GGUF: 758 examples, 24 steps, **loss 3.52 → 0.50** (held-out 0.87), 460,800 trainable params, 47 s, `adapter.gguf` + `adapter_model.safetensors` + loss curve written. Evidence: `docs/evidence/finetune-lora-2026-09-19.json` |
| Serving the fine-tuned adapter | **verified by the runtime itself**: `llama-server --lora adapter.gguf` started and `GET /lora-adapters` returned `{"id": 0, "path": ".../adapter.gguf", "scale": 1.0}`; Forge then answered a documentation task through that endpoint (`provider=local-openai`, success) |
| CI failure root-caused and fixed | The branch's CI had been red on all three Python versions. Reproduced in a clean venv that matches CI (`pip install -e ".[dev]"`, no torch): **4 failures** — three fine-tune preflight tests (the job demanded the HuggingFace stack even for a *registered* backend that does not use it) and the Python 3.8 compat gate (`Path.is_relative_to`). Both fixed; the same CI-shaped venv now runs **3328 passed, 7 skipped, 0 failed** (764 s) |
| Multimodal specialists (100) | 25 per modality (vision, image generation, speech-to-text, text-to-speech). Registration is evidence-based: **0/100 registered in an unconfigured environment**, each missing modality reported with its exact env var. Backed end to end against real HTTP endpoints (data-URI chat message, multipart WAV upload, `/v1/audio/speech`, `/v1/images/generations`) — **14 tests** |
| Outbound channels | WhatsApp Cloud API, Twilio SMS, Twilio **voice call** (TwiML) and **SMTP email** implemented and wired to the voice intents. Tested against real HTTP servers and a real SMTP dialogue (greeting, EHLO/AUTH, MAIL/RCPT/DATA) — **14 tests**. Unconfigured channels are not registered and name their exact variables |

## 2. Implementations

**Real local model path** (`forge/models/local_openai.py`, `docs/SELF-HOSTED-MODEL.md`)
- A self-hosted OpenAI-compatible endpoint (llama.cpp `llama-server`, vLLM,
  Ollama `/v1`, TGI, LM Studio) can be registered from
  `FORGE_LOCAL_MODEL_URL` / `FORGE_LOCAL_MODEL_NAME` with no credential; it is
  registered as CONFIGURED and only the runtime monitor's real `/models` probe
  moves it to LIVE.
- `scripts/run_fleet_real.py` executes every registered specialist through the
  fabric, refuses to report when no runtime-verified model exists, smoke-tests
  one real generation before starting, and records per-specialist output.
- Specialist requests carry a bounded output budget and a low temperature, and
  the model-facing prompt contains the role and the job only — routing
  metadata stays in the request/result metadata.

**Provider failover and multi-server pools** (`forge/models/endpoints.py`,
`quota.py`, `local_openai.py`, `config.py`, `fabric.py`, `router.py`, `errors.py`)
- One provider can front several of your servers for the same model: a spent
  endpoint is rotated to within the same request, and if every endpoint of that
  model is spent the request fails over to another model.
- Exhaustion is *classified*, not guessed: HTTP 402/429/503 and provider wording
  (`insufficient_quota`, rate limit, credits, capacity) start a cooldown; a 500
  or an unreachable host never does — a broken endpoint must not be mistaken
  for a full one.
- The cooldown is bounded (default 60 s, stated `Retry-After` honoured, capped
  at 900 s) and expires by itself: a spent provider is skipped, never
  blacklisted, and any successful call clears it.
- Endpoints are declared with numbered env vars, a JSON list, or the original
  single-endpoint variables (unchanged). Same model → pooled; different models
  → separate models, ordered by declared `tier` so a stronger server is
  preferred among models that can equally do the job. Capability requirements
  are never relaxed, so power never overrides fitness.
- Visibility: `quota_snapshot()`, `provider_exhausted` /
  `provider_skipped_exhausted` telemetry, and a `failover` section in the
  readiness report.

**Fine-tuning** (`forge/training/`, `scripts/finetune_specialists.py`)
- Dataset: real fleet-run evidence → instruction pairs (blocked specialists
  contribute nothing), validated and deduplicated by the Model Studio's own
  validator; a secret-bearing row is rejected and never trained on.
- `GgufLoRATrainer` trains LoRA against the **GGUF the runtime serves**
  (dequantized with `gguf.quants`), on CPU, with a hand-written llama forward
  pass whose correctness is checked by loss on real text (4.96 vs log(vocab)
  10.80).
- Artifacts: `adapter_model.safetensors`, `adapter.gguf` (llama.cpp adapter
  format) and `training.json` (loss curve, params, exact base file).
- Honest states: `BLOCKED` (with the exact missing requirement) · `READY` ·
  `TRAINING` · `TRAINED` (only with a real artifact the backend marked trained)
  · `FAILED` (verbatim error).

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

**Multimodal models on your own servers** (`forge/models/multimodal_bridge.py`,
`forge/agents/multimodal_fleet.py`, `forge/vision/local.py`,
`forge/voice/local_audio.py`)
- Four self-hosted endpoints are bridged into the fabric as ordinary models:
  `FORGE_VISION_URL` (VL model on llama.cpp/vLLM/Ollama), `FORGE_STT_URL`
  (whisper), `FORGE_TTS_URL` (Piper/Coqui), `FORGE_IMAGE_URL`
  (LocalAI/diffusion bridge). No cloud credential is needed for any of them.
- A model is registered **only** for a modality whose endpoint is configured, so
  a registry entry always has a real backend. The 100 specialists register per
  modality: 25 vision, 25 image-generation, 25 speech-to-text, 25 text-to-speech.
- Media travels as a `data:` URI or a file path, and file paths are read only
  under `FORGE_MULTIMODAL_INPUT_DIR` — a prompt cannot make Forge read an
  arbitrary file. A failing endpoint raises a real error; it is never an empty
  success. `probe_multimodal()` performs the tiny real request that moves a
  modality to `verified`.
- `GET /api/v1/multimodal` reports registered models and, per modality, the exact
  requirement when it is missing.

**Outbound channels: WhatsApp, SMS, voice call, email**
(`forge/comms/channels.py`, control-plane intent delivery, `GET /api/v1/channels`)
- Channel registry that registers a channel only when it is fully configured; a
  half-configured channel is reported with the variables it needs instead of
  being listed as available.
- Sends are real provider calls returning the provider's own message id and
  status, or the provider's own error. `send_email` prefers SMTP; the
  completion notifier keeps the existing Resend path.
- A missing recipient is reported as `no_recipient`; nothing is sent and no
  address is invented. Sending stays behind the existing permission/approval
  path (delivery happens only for an approved voice intent).

## 3. Live providers

| Provider | State | Evidence |
| --- | --- | --- |
| `local` (deterministic fallback) | registered, `available=True`, not runtime-verified | fabric snapshot |
| `ollama` | registered; endpoint unreachable here → capability state **BLOCKED** | fabric snapshot |
| `openai`, web research | **ARCHITECTURE** — no credential present | capability snapshot |
| `local-openai` → `SmolLM2-135M-Instruct.Q4_1.gguf` on `127.0.0.1:8080` | **LIVE**, `runtime_verified = True` | real probe of `/models` + real generation; see §4 |

No cloud credential exists in this environment, so a real instruct model was
**self-hosted instead**: llama.cpp `llama-server` serving a 135M-parameter
instruct GGUF that Forge routes to through the same fabric path a cloud model
would use (`forge/models/local_openai.py`, configured with
`FORGE_LOCAL_MODEL_URL` / `FORGE_LOCAL_MODEL_NAME`; no credentials involved).
Setup and swap instructions: `docs/SELF-HOSTED-MODEL.md`.

This provider is endpoint-agnostic: pointing it at a larger hosted endpoint (or
setting a cloud credential) changes which model answers, not whether the fleet
can execute.

## 4. Registered agents and executed representatives

- Registered: **1000 logical specialists**, **40 specializations × 25**,
  covering planner, researcher, architect, coder, frontend, backend, database,
  devops, debugger, tester, reviewer, security, performance, refactor,
  documentation, API, data, ML, vision, audio, game, OS, browser,
  computer-use, UX, product, QA, compliance, privacy, localization, math,
  science, finance, legal, prompt, agentic, memory, orchestration,
  integration, release.

### Are all 1,000 working? — the measured answer

| Question | Answer | Evidence |
| --- | --- | --- |
| Registered and unique? | **1000/1000**, 25 per role, 40 roles | `scripts/verify_production_readiness.py` |
| Routable (request is satisfiable)? | **1000/1000** | every specialist's required capabilities are canonical and met by a capability-complete model |
| Executable end-to-end? | **1000/1000** in 0.09 s | all 1,000 executed through a real `ModelFabric` with a recording provider (1,000 provider calls) |
| Executable on *this* deployment's fabric? | **900/1000** | the other **100** require `vision`, `audio`, `browser` or `computer_use` (25 each) and no configured model provides those capabilities — they fail loudly, they are never rerouted to a model that cannot do the job |
| Producing real work right now? | **Yes — 900/1000**, all from a real model | full fleet sweep against the self-hosted instruct model: **900 real outputs, 0 exceptions, 0 empty outputs, 51,047 output tokens, 515 s**, every one routed `provider=local-openai`; artifact `docs/evidence/fleet-real-run-2026-09-19.json` (model SHA-256, per-specialist output, blocked reasons) |
| Selectable by the planner? | **All 10 requirement capabilities staffed** | coding, testing, debugging, review, security, documentation, research, architecture, performance, git each resolve to a real specialist, and core tool-using agents (coder/debugger/reviewer/tester/security) win ties for their own capabilities |
| Selectable when a capability is missing? | **Reported, not hidden** | `AgentPlan.unmet` and the `agents_selected` event carry `unmet_capabilities` |

Two defects were found and fixed while answering this question:

1. **Secondary capabilities were hard requirements.** `tool_use` was required
   by 100 specialists (coder, integration, agentic, orchestration) and could not
   be relaxed by the router, so they failed against models that could do their
   actual job. Only the specialization-defining capability is required now;
   the rest travel as `preferred_capabilities`. Failures dropped 200 → 100.
2. **Three planner capabilities had no specialist at all.** `documentation`,
   `performance` and `git` needs were silently dropped by the planner
   (documentation specialists advertised `writing`, performance specialists
   `optimization`, and no role was `git`), so a “document the deployment steps”
   task planned **zero** agents. Fixed by advertising the capabilities the
   requirement vocabulary uses and mapping `git` to the release-engineering
   role; every requirement capability now selects a specialist.

Representatives: **40/40** (one per specialization) routed through a real
`ModelFabric` with an in-process **SIMULATED** provider — plumbing proven, not
a live remote model call.

### Observed real execution (2026-09-19)

Command (reproducible; the harness refuses to report when no runtime-verified
model exists):

```
FORGE_LOCAL_MODEL_URL=http://127.0.0.1:8080 \
FORGE_LOCAL_MODEL_NAME=SmolLM2-135M-Instruct.Q4_1.gguf \
.venv/bin/python scripts/run_fleet_real.py --workers 2 --max-tokens 64 \
    --temperature 0.2 --out /tmp/fleet-real-run2.json
```

| Measurement | Result |
| --- | --- |
| Specialists executed | **1000/1000** |
| Real model outputs | **900**, `provider=local-openai`, 0 exceptions, 0 empty |
| Output tokens generated | **51,047** |
| Wall time | **515 s** on 2 vCPU |
| Roles covered | 36 of the 40 roles produced output; the other 4 (vision, audio, browser, computer-use) are the blocked ones |
| Correctly refused | **100** = `audio` 25, `browser` 25, `computer_use` 25, `vision` 25 — each with a real error (`no registered model supports capabilities ['audio']`) |
| Output quality | fluent English that addresses the brief; **21.3 %** of answers fall into repetition or a generic “ready to assist” (measured, not hidden) |

Every specialist receives the same brief (a production-readiness scenario) and
answers as its own specialization; two representative outputs per role are
recorded in the evidence artifact.

Two defects found by actually running it, both fixed:

1. **No output budget.** Each of the 1,000 calls let the runtime generate to its
   own ceiling (512 tokens) — the sweep was going to take hours. Requests now
   carry `DEFAULT_MAX_OUTPUT_TOKENS` (512, overridable, floor 16) and a
   deterministic-by-default temperature (0.2; greedy collapsed to one repeated
   phrase on this model).
2. **Routing metadata was addressed to the model.** The rendered constraint
   block (`required capabilities: coding`, `maximum output tokens: 48`,
   `prefer local provider`) plus `Route through the shared ModelFabric` and
   `Preferred model target: …` dominated the prompt, and the model echoed it
   instead of answering — e.g. `orchestration-01-0038` before →
   *“CONSTRAINTS defined capabilities: planning maximum output tokens: 48”*,
   after → *“To create a web service that can handle ten times its current
   traffic, I would take the following steps: …”*. Limits now travel in the
   API's native fields, the block is recorded in the result metadata (not
   silently dropped), and the model-facing prompt is the role and the job.

## 5. Background execution and persistence

- Worker identity and capabilities survive a restart; restored workers are
  **not** live (`live_count = 0`) until a fresh heartbeat.
- Stale running tasks are recovered into `RECOVERY`, leases are re-issued, and
  a lease-losing attempt cannot publish (covered by the suite:
  `test_lease_guard.py`, `test_worker_registry.py`, server restart/reconnect
  tests).
- No artificial polling deadline is used for background work.

## 6. Blocked external dependencies

**1. Cloud model credential on the deployed service** — BLOCKED (locally
resolved by self-hosting a real model)
- *Reason*: no provider credential exists in this environment. This is no
  longer a blocker for real execution — a real instruct model was self-hosted
  and Forge routes to it (`local-openai`, LIVE, `runtime_verified = True`) — but
  the **deployed** Render service has no model endpoint of its own, so it still
  answers from the deterministic stub until one is configured there.
- *Already implemented*: ModelFabric routing, capability matching, runtime
  verification gate, failover chain, provenance on every result, and the
  endpoint-agnostic `local-openai` provider.
- *Exact requirement*: set `FORGE_LOCAL_MODEL_URL` + `FORGE_LOCAL_MODEL_NAME`
  to a reachable endpoint from the service (a self-hosted runtime on the same
  network, or a managed OpenAI-compatible endpoint) **or** set
  `OPENAI_API_KEY` / `ANTHROPIC_API_KEY`; then re-run
  `scripts/verify_production_readiness.py` and
  `scripts/run_fleet_real.py`. The state flips from CONFIGURED to LIVE only
  when a model is runtime-verified.

**1b. Vision / audio / browser / computer-use specialists (the 100 of
1000 that a text model cannot serve)** — BLOCKED on the *endpoint* (see
item 4 for the four implemented bridges; browser/computer-use remain
adapter work)
- *Reason*: the live self-hosted model is a **text** instruct model; it declares
  coding, debugging, documentation, planning, reasoning, research, review,
  security, structured_output and testing. No configured model declares
  `vision`, `audio`, `browser` or `computer_use`, so those 100 specialists stop
  with `no registered model supports capabilities ['vision']` (25 each) — they
  are never served by a model that cannot do the job.
- *Already implemented*: those 25+25+25+25 specialists are registered, routed
  and executable the moment a model advertising the capability is configured;
  they currently fail with an explicit capability error instead of being
  rerouted to a model that cannot do the job.
- *Exact requirement*: register a multimodal/vision model (e.g. a vision Ollama
  model such as `llava` / `qwen2.5vl` through `OLLAMA_BASE_URL`, or a hosted
  vision model), plus browser/computer-use backends for those two
  specializations; re-run the readiness script and the blocked counts drop.

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

**2b. Model quality: only a 135M-parameter model is reachable from this
environment** — BLOCKED (quality, not plumbing)
- *Reason*: the sandbox reaches PyPI and GitHub only; HuggingFace,
  `media.githubusercontent.com` and the Ollama registry are unreachable
  (verified: HTTP 000), and every larger instruct GGUF found in a GitHub repo is
  a Git-LFS pointer (`version https://git-lfs.github.com/spec/v1`) whose content
  lives on the blocked media host. PyPI bundles exactly one instruct GGUF
  (`llm-smollm2`, 135M — `llm-qwen`, `llm-gemma`, `llm-phi`, `llm-smollm2-360m`
  do not exist).
- *Already implemented*: the endpoint-agnostic `local-openai` provider, the
  env-configured model slot, real runtime verification, and the fleet harness —
  the 900 routable specialists answer through whichever model is configured.
  Measured quality: fluent and on-brief, **21.3 %** degenerate (repetition or a
  generic "ready to assist"), 51,047 tokens across 900 answers.
- *Exact requirement*: either allowlist a model host (`huggingface.co`,
  `media.githubusercontent.com`/LFS) so a 0.5B-1.5B instruct GGUF can be
  fetched, or set a cloud model credential (`OPENAI_API_KEY` /
  `ANTHROPIC_API_KEY`) on this service. Then set `FORGE_LOCAL_MODEL_URL/NAME`
  (or the cloud config) and re-run `scripts/run_fleet_real.py`; no code change
  is needed.

**3. Outbound channels need the operator's credentials** — BLOCKED (only the
credentials)
- *Reason*: the adapters are implemented and tested, but this environment has no
  WhatsApp/Twilio/SMTP account, so nothing can be sent from here and no send is
  claimed. In an unconfigured deployment `GET /api/v1/channels` reports every
  channel with `configured: false` and the variables it needs.
- *Already implemented*: WhatsApp Cloud API, Twilio SMS, Twilio voice call
  (TwiML), SMTP email; intent wiring for `send_whatsapp`, `send_sms`,
  `make_call`, `send_email`; real provider-response reporting; 14 tests driving
  real HTTP servers and a real SMTP dialogue.
- *Exact requirement*: set `FORGE_WHATSAPP_TOKEN` + `FORGE_WHATSAPP_PHONE_ID`,
  or `FORGE_TWILIO_ACCOUNT_SID` + `FORGE_TWILIO_AUTH_TOKEN` + `FORGE_TWILIO_FROM`,
  or `FORGE_SMTP_HOST` + `FORGE_SMTP_FROM` (plus AUTH if required) on the
  service; then `GET /api/v1/channels` lists them as configured and a voice
  intent sends for real.

**4. Multimodal endpoints on your servers** — BLOCKED (only the endpoints)
- *Reason*: there is no vision, speech or diffusion server reachable from this
  environment, so 0/100 multimodal specialists are registered here. Nothing is
  claimed: the readiness report prints `0/100, state MISSING` with the four
  variables.
- *Already implemented*: the four bridges, capability-gated registration, the
  100 specialists, `probe_multimodal()`, the API surface and 14 tests against
  real HTTP endpoints.
- *Exact requirement*: run the endpoint(s) on your GPU/server box and set
  `FORGE_VISION_URL` (VL GGUF on llama.cpp/vLLM/Ollama), `FORGE_STT_URL`
  (whisper), `FORGE_TTS_URL` (Piper/Coqui), `FORGE_IMAGE_URL` (LocalAI/SD
  bridge); `python scripts/verify_production_readiness.py` then shows the
  modality READY with 25/25 specialists. Steps:
  `docs/ENABLE-MULTIMODAL-AND-COMMS.md`.

## 6b. Deployment shape: servers do the work, thin clients just connect

- The cockpit serves static HTML/CSS/JS and all logic runs server-side; the
  browser calls relative `/api/v1/...` paths only. A G560-class client needs a
  browser and a network path, nothing else (`docs/SERVER-SIDE-EXECUTION.md`).
- Model servers are declared per deployment (`FORGE_LOCAL_MODEL_URL[_N]`,
  `FORGE_MODEL_ENDPOINTS`, `deploy/model-endpoints.example.json`); the readiness
  report's `failover` section prints how many are configured, their tiers, the
  cooldown policy and anything currently exhausted.

## 7. Reproduce

```bash
.venv/bin/python -m pytest -q                       # 3296 passed, 6 skipped
node --test tests/web/*.test.cjs                    # 11 passed
.venv/bin/python scripts/verify_production_readiness.py
.venv/bin/python scripts/verify_production_readiness.py --json | jq .

# provider failover + multi-server routing (real HTTP servers in the tests)
.venv/bin/python -m pytest tests/test_provider_failover_quota.py -q   # 19 passed

# fine-tuning: dataset -> preflight -> train (needs torch + gguf + tokenizers)
.venv/bin/python -m pytest tests/test_finetune_pipeline.py -q         # 8 passed
.venv/bin/python -m pytest tests/test_gguf_lora_trainer.py -q         # 6 passed
.venv/bin/python scripts/finetune_specialists.py \
    --dataset docs/evidence/fleet-real-run-2026-09-19.json \
    --specialization documentation --steps 24

# real model execution (requires the self-hosted runtime, see
# docs/SELF-HOSTED-MODEL.md; refuses to report without a verified model)
FORGE_LOCAL_MODEL_URL=http://127.0.0.1:8080 \
FORGE_LOCAL_MODEL_NAME=SmolLM2-135M-Instruct.Q4_1.gguf \
.venv/bin/python scripts/run_fleet_real.py --workers 2 --max-tokens 64 \
    --temperature 0.2 --out /tmp/fleet-real-run.json
```
