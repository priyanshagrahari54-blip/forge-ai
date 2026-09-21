# Forge AI — production readiness report

Date: 2026-09-21 · Branch: `arena/01a0c3d9-forge-ai`

Section 0 is the current state of this branch. Sections 1-7 are the earlier
reports (verified 2026-09-19/20) and are kept as dated evidence; where they
say **1000 specialists / 40 × 25**, that was the fleet size at the time — the
fleet is now **1,040 = 40 × 26** (section 0).

## 0. Current state (2026-09-21) — what changed and what is verified

State vocabulary used everywhere below (a stronger word is never used for a
weaker state): *catalogued* (named in a document) → *registered* (a `Model`
entry exists in this process) → *configured* (env/config names it) →
*discovered* (the provider's inventory listed the id) → *reachable* (the
endpoint answered) → *verified / live* (a real inference call succeeded) →
*executed / successful* (a task or specialist produced output through it).

| Area | State on this branch | How it was verified |
| --- | --- | --- |
| Hosted providers (Anthropic, Gemini, OpenRouter, Groq, OpenAI) | **Implemented and configured-only**: adapters exist and register from `<NAME>_API_KEY` + `<NAME>_MODELS`/`<NAME>_MODEL` (`OPENAI_MODELS` for OpenAI). No credential is present in this environment, so **0 hosted models are live here**. Registered hosted models start `available=False`, `runtime_verified=False` | `tests/test_live_runtime_gate.py`, `tests/test_runtime_monitor_service.py` (real HTTP servers in-process) |
| Runtime verification | Only a successful real inference (probe or production generation) sets `runtime_verified=True` / `available=True`; discovery is inventory evidence only; a failed probe or a vanished id marks the model unavailable. The API lifespan starts `RuntimeMonitorService` with bounded probes (`FORGE_RUNTIME_INFERENCE_PROBES`, 8 per tick, back-off 5 min → 1 h); a returned model identity that differs from the requested id is rejected | same tests; production entry exercised with `forge_web.build_app()` — `ollama/llama3.2` went `available=False` after its probe failed, `/api/v1/runtimes` reported `configured=1 live=0 verified=0 unavailable=1` |
| P0 defects fixed | prefixed registry ids vs bare provider ids (`Model.provider_model_id`); discovery-only unrouting; LIVE → unrouted flapping on stale ticks; test doubles that echo the provider name as the model identity no longer count as a mismatch | reproduced before the fix, regression tests added |
| Specialist fleet | **1,040 registered = 40 specializations × 26 variants** (`frontier-1040`), 5 core agents, up to 100 multimodal specialists that register only with a working backend. A specialist binds **no model**: eligible models are resolved from the live registry per request (`available`, not fallback, supports all required capabilities; `runtime_verified` first; variant *k* rotates the order by *k−1*) | `scripts/verify_production_readiness.py`: `registered_specialists=1040`, `roles=40`, 26 per role; `tests/test_frontier_fleet.py` (eligibility, rotation, availability changes without rebuild) |
| Fleet execution on *this* machine (no text model, no Pillow) | **78/1040 execute** through real local backends (`forge-local-audio` 26, `forge-browser` 26, `forge-web-actions` 26); **936 are answered only by the deterministic fallback and are no longer counted as executed**; **26 blocked** (`vision`: no backend here). With a verified text model the 936 route to it — that is the deployment's job, not a code change | `scripts/verify_production_readiness.py` (the script now reports `answered_by_fallback_only` separately) |
| Local capability providers | Registered only after an in-process probe passes; the verified report is memoised per fabric, so per-run registry rebuilds re-use it (first probe ≈ 0.5 s before, later runs 0 ms). The browser probe's HTTP server now shuts down in 20 ms instead of 500 ms: **control-plane construction 0.55 s → 0.04 s** (measured with `tests/helpers_a34.make_plane`) | timing runs in this checkout |
| Task execution | submit → queued → leased → started → routed → provider → verified → completed, dispatcher bounded to `max_runs_per_project=1` (sequential per project by design) | `tests/test_a34_task_e2e.py`, `tests/test_a34_approvals_e2e.py` |
| Voice | informational intents execute directly; consequential intents (commit, email, WhatsApp, calls) always require approval; ambiguous requests get a clarifying question; the A33 voice policy still gates every intent; API/plane/web default `confirm=false` | `tests/test_a42_*.py`, `tests/test_voice*.py`, `node --test tests/web/handsfree.test.cjs` |
| Login | `POST /api/v1/sessions` needs only `actor`/`profile`; the project resolves to the sole registered project ("forge" in the deployment); no project picker in the cockpit; unknown ids still 404 | `tests/test_a34_api.py`, `tests/test_a34_ui.py` |
| AI City | reads only real backend state (`/tasks`, milestones, SSE event stream); no synthetic progress | `scripts/verify_production_readiness.py` `web` section |
| Dead code removed | the older `forge/agents/fleet.py` / `routing.py` / `model_execution.py` "1000+ slot" fleet (unused by any production path, traced) and the stray `forge-ai-fixes.patch` (already applied) | `grep` trace, full suite |
| Not done / honest gaps | no hosted credential here, so hosted inference is **configured-only** until the Render environment sets a key; same-project task concurrency stays at 1; vision needs the `media` extra (installed by the Dockerfile, not in this checkout) | — |

Full suite on this branch: see the end of this section.


Everything below was observed in this checkout or against the live service.
Nothing is claimed from a summary; states are the ones the runtime itself
reports. Reproduce with the commands in the last section.

## 1. Verifications (observed)

| Check | Result |
| --- | --- |
| `python -m pytest -q` | **3352 passed, 6 skipped, 0 failed** (693 s) in this checkout's dev venv (torch + the fine-tune stack installed). Baseline before this work was 112 failures |
| **Branch CI is green** | Run [35487573208](https://github.com/priyanshagrahari54-blip/forge-ai/actions/runs/35487573208) on `f851f2f`: **all three jobs pass** — Python 3.8 (15m19s), 3.11 (13m13s), 3.13 (13m57s). The three previous runs were red; each of the three jobs was down to exactly one failure and each was a real defect, root-caused and fixed (row below) |
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
| Last three CI failures root-caused (all three jobs were down to one) | **3.8**: `classify_exhaustion` read `retry_after` off a `urllib` `HTTPError`, whose Python 3.8 attribute delegate raises `KeyError('file')` instead of `AttributeError` — a real 429 could not be classified, so an exhausted provider was invisible to failover. Attribute reads now go through `safe_attribute()` (still honours the stated `Retry-After`) and `FailoverPool` guards the same reads. **3.13**: the CRUD test demanded a created task still be `created`/`queued` while the worker is *supposed* to pick it up (observed `started`) — it now accepts any non-terminal state. **3.11**: `recover_stale_running(0.001)` raced the clock (the claim→query gap can be under 1 ms on a fast runner); the heartbeat is backdated instead, as its sibling test already did. Regression tests reproduce the 3.8 delegate on every interpreter. |
| Fine-tune promotion gate (held-out) | A real end-to-end run on the served GGUF, then honest evaluation on a held-out 25% split: base model averaged **0.0000** on 6 held-out coding cases, the trained adapter **0.1308** (delta +0.1308). Both verdicts were produced by the gate: the 0.35 absolute floor **rejected** the adapter, the no-regression gate **promoted** it. A rejected adapter stays `candidate`; training alone never claims promotion. Evidence: `docs/evidence/finetune-promotion-2026-09-20.json` |
| Per-specialisation fine-tuning (role filter) | `build_dataset(roles=[...])` narrows the dataset to one specialist's own answers (case/separator-insensitive) and keeps the unfiltered counts in `report["roles_before_filter"]`; an unknown role yields an **empty** dataset that trips the minimum-records preflight instead of silently training on other roles. `--coverage` audits the fleet evidence first: **36/36 roles are trainable** (758 validated rows, ≥ 8 each). Real runs: security 20 rows **0.2358** vs base 0.0, devops 25 rows **0.1382** vs 0.0, documentation 16 rows **0.1595** vs 0.0, privacy 19 rows **0.2250** vs base 0.0094 — all four promoted by the gate. Evidence: `docs/evidence/finetune-role-coverage-2026-09-20.json` |
| Serving the fine-tuned adapter | **verified by the runtime itself**: `llama-server --lora adapter.gguf` started and `GET /lora-adapters` returned `{"id": 0, "path": ".../adapter.gguf", "scale": 1.0}`; Forge then answered a documentation task through that endpoint (`provider=local-openai`, success) |
| CI failure root-caused and fixed | The branch's CI had been red on all three Python versions. Reproduced in a clean venv that matches CI (`pip install -e ".[dev]"`, no torch): **4 failures** — three fine-tune preflight tests (the job demanded the HuggingFace stack even for a *registered* backend that does not use it) and the Python 3.8 compat gate (`Path.is_relative_to`). Both fixed; the same CI-shaped venv now runs **3328 passed, 7 skipped, 0 failed** (764 s) |
| Multimodal specialists (100) | 25 per modality (vision, image generation, speech-to-text, text-to-speech). Registration is evidence-based: with no endpoint **and** no local backend, **0/100 register** and each missing modality reports its exact env var; with the in-process backends this machine probes as working, **100/100 register and execute** (below). Backed end to end against real HTTP endpoints (data-URI chat message, multipart WAV upload, `/v1/audio/speech`, `/v1/images/generations`) — **14 tests** |
| Local capability backends (2026-09-20) | vision / audio / speech / image / browser / computer-use serve from the machine itself, and a capability is registered **only after its own probe really ran**: 7/7 probes verified here (`vision`, `image_generation`, `audio`, `speech_to_text`, `text_to_speech`, `browser`, `computer_use`). Probes are real work — Pillow rasterises 64×48 pixels, espeak-ng synthesises 152,896 bytes of speech, pocketsphinx decodes it, a loopback HTTP server serves pages the local browser reads and clicks through. `forge/models/local_capabilities.py`, `forge/{vision/pixels,vision/procedural,voice/local_speech,browser/local}.py`; **17 tests** |
| The 200 capability specialists really execute (2026-09-20) | `scripts/run_capability_fleet.py`: **100/100** vision/audio/browser/computer-use and **100/100** multimodal specialists, **0 failures**, on real inputs (a real PNG, real espeak-ng speech, real loopback pages), each through the real `ModelFabric` routing path, each with its own request and its own output. Evidence: `docs/evidence/capability-fleet-2026-09-20.json` |
| **All 1000 specialists execute against this deployment's fabric (2026-09-20)** | `scripts/verify_production_readiness.py` now asks every specialist for work it can answer — a text task for a text capability, and a request carrying real media for vision/audio/browser/computer-use (built by `forge/capabilities/live_inputs.py`, the same inputs the fleet run uses). Result: **1000/1000 execute**, by provider `{local: 900, forge-local-vision: 25, forge-local-audio: 25, forge-browser: 25, forge-web-actions: 25}`, with a real 9,162-byte bitmap, 152,928 bytes of real speech and real pages on loopback in those requests. Spot-checked outputs: `image: 96x64 PNG, brightness 130.51/255, contrast 62.42, palette #17171f 63%`; `audio: 3.467s, 22050 Hz, RMS 3409.0, silence 21%`; `URL/TITLE/LINKS/TEXT` of `/orders`; actions landing on `/search?q=low+stock` |
| The deployment image can do it too (2026-09-20) | A `media` extra (Pillow, espeakng-loader, pocketsphinx — all manylinux wheels, no compiler) is installed by the `Dockerfile` and by the 3.11 CI leg, which is the interpreter the image runs. Verified on **Python 3.11 with the extra installed exactly as the image does it**: 7/7 probes pass and the same script executed **100/100 + 100/100** specialists in 35.3 s. Evidence: `docs/evidence/capability-fleet-deployment-python-2026-09-20.json`. CI now fails if a media package is installed but its capability does not verify |
| Crash-class defect fixed while proving the above | espeak-ng was re-initialised **per request**; `espeak_Initialize` rebuilds the phoneme tables, and after a few hundred calls the compiler printed `Invalid instruction ... for phoneme` and the process died with **SIGSEGV (exit 139)** — seen twice. The engine is now built once per process and synthesis is serialised (`_ENGINE`, `_ENGINE_LOCK`); **four consecutive fleet runs (200 executions each) and three thread-concurrency tests are clean** |
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

**Specialist fleet** (`forge/agents/frontier_fleet.py`; 1,000 slots at the
time of this section — now 1,040 = 40 × 26, see section 0)
- All 40 specializations register before any repeat.
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
| `forge-local-vision` (Pillow) | **LIVE**, `runtime_verified = True`, no credential | `forge-local/vision-pixels` measured a real 64×48 PNG (mean brightness 84.0) |
| `forge-local-imagegen` (Pillow drawing) | **LIVE**, `runtime_verified = True`, no credential | `forge-local/image-procedural` rendered a real 320×200 PNG (1,772 bytes) from a real spec; metadata states it is procedural, not generative (`FORGE_IMAGE_URL` upgrades it) |
| `forge-local-audio` (stdlib) | **LIVE**, `runtime_verified = True`, no credential | measured a real espeak-ng WAV: 3.467 s, RMS 3409.0, peak 29803, silence 21% |
| `forge-local-tts` (espeak-ng via ctypes) | **LIVE**, `runtime_verified = True`, no credential | synthesised 152,896 bytes of 22,050 Hz speech; round-tripped back through the recogniser |
| `forge-local-stt` (pocketsphinx, bundled en-us model) | **LIVE**, `runtime_verified = True`, no credential | decoded that audio to a hypothesis with its confidence reported (hypothesis-grade: `FORGE_STT_URL` gives Whisper-class) |
| `forge-browser` / `forge-web-actions` (stdlib DOM) | **LIVE**, `runtime_verified = True`, no credential | fetched and acted on a real loopback page (`['navigate','click','navigate','type','submit']` → `/search?q=csv+export`); SSRF allowlist refused `169.254.169.254` and non-allowlisted `10.0.5.5` |

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

- Registered (2026-09-19 fleet; now 1,040 = 40 × 26): **1000 logical specialists**, **40 specializations × 25**,
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

**2. Render deployment of this commit** — BLOCKED (only the deploy trigger)
- *Reason*: no Render API key or deploy hook exists in this environment, and this
  session may only push `arena/01a0b955-forge-ai`. The live service is up
  (`/api/v1/health` → `{"status":"ok","auth_mode":"production","worker":true}`
  at 2026-09-20 04:0x UTC) but serves an **older build**: `/voice-playback.js`
  still answers `{"error":{"code":"NOT_FOUND"}}`, and `/api/v1/multimodal` /
  `/api/v1/channels` are not mounted there either.
- *Already implemented*: Dockerfile (now installing the `media` extra, so the
  deployed container has the real vision / speech / image backends), `forge_web`
  entrypoint, CI workflow, `/api/v1/health`, safe static cockpit mount, the new
  routes and the shared voice playback module.
- *Changed since*: PR #46 is now **CONFLICTING** with `main` — other sessions
  have pushed 21 commits to `main` while this branch was being worked on, and a
  conflicting pull request stops producing CI runs entirely (GitHub cannot build
  the merge ref), which is why the newest commits appeared to have no CI. The
  workflow now also runs on pushes to `arena/**`, so this branch gets its own
  3.8 / 3.11 / 3.13 runs (`1d91979`). Reconciling the six conflicted files was
  attempted and abandoned deliberately: they are other sessions' in-flight
  changes (`forge/models/router.py`, `runtime_verification.py`,
  `frontier_fleet.py`, …), and merging them blind would have put unverified code
  into this branch. That reconciliation is a human decision.
- *Exact requirement* (any one): **(a)** merge PR #46 (rebase or resolve the six
  conflicting files) into the branch `forge-ai-server` tracks — if Auto-Deploy is
  on for that branch Render rebuilds by itself; or **(b)** Render dashboard →
  `forge-ai-server` → *Manual Deploy → Deploy latest commit* (or pick the
  branch/commit), or `render deploys create <service-id> --commit <sha>` with a
  Render API key; or **(c)** run this branch on one of your own servers, which
  needs no dashboard at all:
  `git fetch origin arena/01a0b955-forge-ai && git switch --detach FETCH_HEAD`,
  then `python -m pip install ".[media]"` (or `docker build -t forge-ai .`), then
  `FORGE_AUTH_MODE=production python forge_web.py` — the image and the extra both
  include the real vision / speech / image backends, and the voice loop is
  configured to use them (`FORGE_VOICE_{TTS,STT}_PROVIDER=local-inprocess`).
  *Verify*: `GET /api/v1/health` → `status: ok`, `GET /voice-playback.js` → 200,
  `GET /api/v1/multimodal` and `/api/v1/channels` → 401 without a token.
  *Verify*: `GET /voice-playback.js` → 200,
  `GET /api/v1/multimodal` and `/api/v1/channels` → **401** without a token
  (they are read-only and auth-protected), `GET /api/v1/health` → `status: ok`.

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

**4. Multimodal endpoints on your servers** — PARTLY LIFTED (local backends
serve the four modalities; the endpoints remain the quality upgrade)
- *Reason*: there is no vision, speech or diffusion **server** reachable from
  this environment. That no longer stops the modalities: real in-process
  backends now serve all four (Pillow measurement and rendering, espeak-ng
  synthesis, pocketsphinx transcription), so `multimodal specialists` reports
  **100/100 registered, state READY** with the local model named per modality,
  and the capability fleet executed 100/100 of them. What an endpoint adds is
  quality: semantic vision instead of pixel statistics, Whisper-class
  transcription instead of a hypothesis, neural voices, and a diffusion model
  instead of procedural drawing. Each modality's report says exactly which
  variable buys that.
- *Already implemented*: the four bridges, capability-gated registration, the
  100 specialists, `probe_multimodal()`, the local backends behind them, the API
  surface and 14 tests against real HTTP endpoints.
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
.venv/bin/python -m pytest -q                       # 3352 passed, 6 skipped (dev venv)
node --test tests/web/*.test.cjs                    # 11 passed
.venv/bin/python scripts/verify_production_readiness.py
.venv/bin/python scripts/verify_production_readiness.py --json | jq .

# provider failover + multi-server routing (real HTTP servers in the tests)
.venv/bin/python -m pytest tests/test_provider_failover_quota.py -q   # 19 passed

# fine-tuning: dataset -> preflight -> train -> held-out gate (needs torch + gguf + tokenizers)
.venv/bin/python -m pytest tests/test_finetune_pipeline.py -q         # 11 passed
.venv/bin/python -m pytest tests/test_finetune_promotion.py -q        # 10 passed
.venv/bin/python -m pytest tests/test_gguf_lora_trainer.py -q         # 6 passed

# what can the recorded evidence actually train? (one adapter per role)
.venv/bin/python scripts/finetune_specialists.py --coverage           # 36/36 roles READY

# train one specialisation on its own answers, then let the gate decide:
# a rejected adapter stays a candidate artifact, it is never silently promoted
.venv/bin/python scripts/finetune_specialists.py \
    --dataset docs/evidence/fleet-real-run-2026-09-19.json \
    --specialization security --roles security --steps 30 \
    --eval-cases 6 --eval-tokens 24

# real model execution (requires the self-hosted runtime, see
# docs/SELF-HOSTED-MODEL.md; refuses to report without a verified model)
FORGE_LOCAL_MODEL_URL=http://127.0.0.1:8080 \
FORGE_LOCAL_MODEL_NAME=SmolLM2-135M-Instruct.Q4_1.gguf \
.venv/bin/python scripts/run_fleet_real.py --workers 2 --max-tokens 64 \
    --temperature 0.2 --out /tmp/fleet-real-run.json
```
