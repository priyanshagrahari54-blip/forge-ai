# Running Forge against a self-hosted model

Forge can route real work to a model you host yourself. No cloud credential is
involved, which makes this the way to get genuine model output on a machine
that has no API keys.

## 1. Start an OpenAI-compatible runtime

Any runtime that speaks the OpenAI HTTP shape works: llama.cpp's
`llama-server`, vLLM, Ollama's `/v1` layer, LM Studio, TGI.

```bash
# llama.cpp: build once
git clone --depth 1 https://github.com/ggml-org/llama.cpp
pip install cmake ninja
cd llama.cpp && cmake -B build -G Ninja -DCMAKE_BUILD_TYPE=Release -DLLAMA_CURL=OFF
cmake --build build --target llama-server -j"$(nproc)"

# serve an instruct GGUF
LD_LIBRARY_PATH=build/bin ./build/bin/llama-server \
  --model /path/to/model-instruct.gguf \
  --host 127.0.0.1 --port 8080 --ctx-size 4096 --threads "$(nproc)"
```

Use an **instruct** model: a base model completes text instead of following
instructions. Check the GGUF before relying on it:

```python
from gguf import GGUFReader
fields = GGUFReader("model.gguf").fields
print(fields["general.name"].contents())
print("chat template present:", "tokenizer.chat_template" in fields)
```

## 2. Point Forge at it

```bash
export FORGE_LOCAL_MODEL_URL=http://127.0.0.1:8080
export FORGE_LOCAL_MODEL_NAME=model-instruct.gguf     # must match the file name
export FORGE_LOCAL_MODEL_CAPABILITIES="coding,reasoning,planning,debugging,testing,review,security,research,documentation,structured_output"
export FORGE_LOCAL_MODEL_CONTEXT=4096
```

`ModelFabric.from_defaults()` registers the provider (`local-openai`) and the
model from the environment, so `ControlPlane` picks it up with no code change.

The runtime monitor then lists the endpoint's `/models` (inventory evidence:
does the id exist?) and sends a bounded real inference probe
(`FORGE_RUNTIME_INFERENCE_PROBES`, default on; 8 probes per tick with
back-off). The model moves to `LIVE` only when that exact model answers the
probe — or a production generation succeeds through it. Until then the model
stays `CONFIGURED` (or `UNAVAILABLE` after a failed probe / a vanished id) and
Forge routes elsewhere — configuration and discovery are never treated as
verification.

## 3. What the capabilities mean here

Forge cannot introspect a remote endpoint, so it only requires from a model the
capabilities the operator declared. Declaring `vision` for a text model would
make vision specialists route to it and fail; leave a capability out and those
specialists stay blocked with an explicit "no registered model supports
capabilities [...]" error instead of being served by a model that cannot do the
job.

## 4. Verify

```bash
.venv/bin/python scripts/verify_production_readiness.py       # fabric state
.venv/bin/python scripts/run_fleet_real.py --workers 2        # every specialist
```

`run_fleet_real.py` refuses to run when the model is not runtime-verified; it
records each specialist's real output to JSON so the claim can be checked.

## Sizing note

A 135M-parameter instruct model in Q4 runs on 2 CPU cores at roughly 40-50
tokens/second and is enough to answer, summarise and write short snippets. It
is not a substitute for a frontier model: route demanding work to a larger
endpoint (or a cloud provider) and keep the small model for cheap, local
traffic. Forge's routing policy decides per request.
