# Running Forge server-side (thin clients: G560 and anything else)

Forge's work belongs on the machines you point it at, not on the laptop in
front of you. This page is the deployment shape for exactly that: your servers
hold the models and run the agents, a thin client (an old laptop, a phone, a
browser) only opens the UI.

## What runs where

| Piece | Where it runs | Why |
| --- | --- | --- |
| Cockpit UI (`/`, `/city.html`, voice pages) | **client browser** | static HTML/CSS/JS served by the server; no local runtime |
| API, orchestration, task state | **Forge server** | `forge_web`/FastAPI; the client never talks to a model directly |
| 1,000 specialists, planning, routing, failover | **Forge server** | logical registrations sharing the server's model capacity |
| Model inference (llama.cpp, vLLM, Ollama, TGI) | **your model servers**, one or many | this is the heavy part, and it is configurable per server |
| Training / fine-tuning (LoRA) | **a server with CPU/GPU to spare** | never the thin client; see the fine-tuning section |
| AI City, voice understanding | browser for audio I/O, **server** for decisions | the browser only plays/records |

A G560-class machine therefore needs: a browser, and a network path to the
Forge server. Nothing else. The UI fetches relative `/api/v1/...` paths, so one
URL is the whole configuration.

## Point the client at your server

1. Deploy Forge on a server that stays up (see `Dockerfile`, `render.yaml`,
   `docs/DEPLOYMENT*` in this repository).
2. Open `https://<your-forge-server>/` from the G560. Add a
   **Frontier token** in the UI if the deployment sets `FORGE_AUTH_TOKEN`;
   the token is the only thing stored locally, in `sessionStorage`.
3. Use `#/city` for AI City and the voice page for hands-free; both call the
   server, and playback uses the shared `voice-playback.js` module.

If you want a saved shortcut that opens the right page with the token field
focused, keep it as a browser bookmark — no local install is involved.

## Give the server several model servers

Any OpenAI-compatible endpoint works. Declare as many as you want; Forge pools
endpoints that serve the *same* model (for quota rotation) and treats different
models as different models (so routing can prefer the more powerful one).

**Numbered environment variables** (simplest):

```bash
# one server, one model — the original single-endpoint config still works
FORGE_LOCAL_MODEL_URL=http://box-a:8080
FORGE_LOCAL_MODEL_NAME=SmolLM2-135M-Instruct.Q4_1.gguf

# a second, more powerful server
FORGE_LOCAL_MODEL_URL_2=http://gpu-box:8080
FORGE_LOCAL_MODEL_NAME_2=qwen2.5-coder-7b-instruct-q4_k_m.gguf
FORGE_LOCAL_MODEL_TIER_2=80          # declared power; higher is preferred
FORGE_LOCAL_MODEL_LABEL_2=gpu-box

# the same model on two servers pools for failover
FORGE_LOCAL_MODEL_URL_3=http://box-c:8080
FORGE_LOCAL_MODEL_NAME_3=qwen2.5-coder-7b-instruct-q4_k_m.gguf
```

**One JSON value** (handy in a platform dashboard):

```bash
FORGE_MODEL_ENDPOINTS='[
  {"url": "http://gpu-box:8080",  "model": "qwen2.5-coder-7b-instruct-q4_k_m.gguf", "tier": 80,  "label": "gpu-box",  "capabilities": "coding,reasoning,structured_output"},
  {"url": "http://box-a:8080",    "model": "SmolLM2-135M-Instruct.Q4_1.gguf",        "tier": 10,  "label": "office"},
  {"url": "http://box-c:8080",    "model": "qwen2.5-coder-7b-instruct-q4_k_m.gguf", "tier": 80,  "label": "gpu-box-2"}
]'
```

Also available per endpoint: `_KEY` (bearer token), `_CONTEXT`, `_TIMEOUT`.
See `deploy/model-endpoints.example.json` for a fill-in template.

### What the tiers do

Among models that can do the job, the higher `tier` wins; ties fall back to
the existing scoring (reliability, latency, cost, free/local preference). A
model that cannot do the job is never chosen for being powerful — capability
requirements are never relaxed. Leave `tier` unset and ordering is exactly what
it was before tiers existed.

## What happens when a provider's limit is spent

This is the "ek ka limit khatam, doosra chalao" path, and it is automatic:

1. **Recognised.** HTTP 402/429/503, `insufficient_quota`, "rate limit",
   "exceeded your current quota", "no credits", "capacity/overloaded" are
   classified as *exhausted*. A 500 or an unreachable host is **not**: a broken
   endpoint must not be mistaken for a full one.
2. **Rotated.** Endpoints serving the same model are tried in order within the
   same request; the caller sees one answer, not one failure per server.
3. **Skipped.** The spent provider is remembered for its cooldown (default
   60s, honouring a stated `Retry-After`, capped at 900s) so the *next*
   request goes straight to a provider that can serve.
4. **Failed over.** If no endpoint of that model is available, routing moves to
   the next model in the failover chain — the same request, no caller retry.
5. **Recovered.** Once the cooldown passes, the provider is eligible again, and
   any successful call clears its history. It is a delay, never a blacklist.

Knobs: `FORGE_PROVIDER_COOLDOWN_SECONDS`, `FORGE_QUOTA_MAX_COOLDOWN_SECONDS`.
Visibility: `fabric.quota_snapshot()` and the readiness report's
`failover` section.

## Fine-tune a specialist on your own server

Forge trains adapters against the **exact GGUF your runtime serves** — no model
download, no Hub access, no second copy of the weights. The training data comes
from real execution evidence: the per-specialist answers recorded by
`scripts/run_fleet_real.py`.

```bash
# prerequisites on the training server: torch, gguf, tokenizers
.venv/bin/pip install torch gguf tokenizers

.venv/bin/python scripts/finetune_specialists.py \
    --dataset docs/evidence/fleet-real-run-2026-09-19.json \
    --specialization documentation \
    --steps 24 --out .forge/models/documentation-finetune-report.json
```

What it does, in order: builds and validates the dataset (duplicates collapsed,
secret-bearing rows rejected), checks the training stack/trainer/disk, trains
LoRA adapters (attention `q`/`v` projections, base weights frozen), and writes:

```
.forge/models/<specialization>-<epoch>/adapter_model.safetensors   # portable
.forge/models/<specialization>-<epoch>/adapter.gguf                # llama.cpp
.forge/models/<specialization>-<epoch>/training.json               # loss curve
```

Serve the fine-tuned specialist:

```bash
llama-server --model <base>.gguf --lora .forge/models/<run>/adapter.gguf \
             --host 127.0.0.1 --port 8080 --ctx-size 4096
```

Measured on this repository's machine (2 vCPU, no GPU), 758 real examples,
24 steps, rank 8: **loss 3.52 → 0.50, held-out 0.87, 460,800 trainable
parameters, 47 s**. The `training.json` next to each adapter is the evidence.

Honest limits:

* an adapter that is fine-tuned needs to *stay* honest — the promotion gate
  (`ModelStudio.promote` + benchmarks) exists for that, and this pipeline
  reports loss, not benchmark scores;
* CPU training is fine for 135M–1.5B models and small step counts; a bigger
  model wants a GPU server, which is a hardware dependency, not a code change;
* if a machine cannot train (no stack, no base model, too little disk), the
  job reports `BLOCKED` with the exact requirement instead of pretending.
