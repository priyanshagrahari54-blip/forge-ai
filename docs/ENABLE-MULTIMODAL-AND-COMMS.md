# Enabling vision, speech, image generation and outbound channels

Everything below is already implemented and tested in this repository. What is
missing in a given deployment is only an *endpoint* or a *credential* — and the
exact one is always named. Nothing here is claimed as working until a real
endpoint answers; Forge registers a capability only when it is configured.

Check the live state at any time:

```bash
python scripts/verify_production_readiness.py     # row: multimodal specialists
# or, on a running server (authenticated):
curl -H "Authorization: Bearer $FORGE_AUTH_TOKEN" \
     https://forge-ai-server.onrender.com/api/v1/multimodal
curl -H "Authorization: Bearer $FORGE_AUTH_TOKEN" \
     https://forge-ai-server.onrender.com/api/v1/channels
```

---

## 1. Multimodal models (100 specialists: 25 per modality)

A specialist is registered only when a model in the fabric advertises its
capability. With no endpoint configured the report reads
`0/100 registered, state MISSING` and lists the four variables below — that is
the honest "not configured", not a failure.

| Modality | 25 specialists read/write | Capability | Endpoint |
| --- | --- | --- | --- |
| Vision | OCR, UI inspection, chart reading, defect checks, … | `vision` | `FORGE_VISION_URL` |
| Image generation | icons, mockups, posters, textures, … | `image_generation` | `FORGE_IMAGE_URL` |
| Speech to text | Hindi/English/Hinglish ASR, diarization, … | `speech_to_text` | `FORGE_STT_URL` |
| Text to speech | Hindi/English/Hinglish TTS, prosody, … | `text_to_speech` | `FORGE_TTS_URL` |

### 1a. Vision (a vision-language model on your server)

```bash
# on the GPU/server box
llama-server -m qwen2.5-vl-7b-instruct-q4_k_m.gguf --host 0.0.0.0 --port 8080
# Forge side
FORGE_VISION_URL=http://gpu-box:8080
FORGE_VISION_MODEL=qwen2.5-vl-7b-instruct-q4_k_m.gguf
FORGE_VISION_KEY=                      # only if your endpoint needs one
```

Any OpenAI-compatible multimodal endpoint works (llama.cpp `llama-server`,
vLLM, Ollama's `/v1` layer). Verify with a real image:

```python
from forge.models.multimodal_bridge import probe_multimodal
print(probe_multimodal())     # vision -> verified | unavailable (+ reason)
```

### 1b. Speech to text

```bash
# whisper.cpp server, faster-whisper, or vLLM transcription endpoint
whisper-server -m ggml-large-v3.bin --host 0.0.0.0 --port 9000
FORGE_STT_URL=http://gpu-box:9000
FORGE_STT_MODEL=large-v3
FORGE_STT_LANGUAGE=                # empty = auto-detect (Hindi/English/Hinglish)
```

The voice loop picks it up with `FORGE_VOICE_STT_PROVIDER=local-whisper`.

### 1c. Text to speech

```bash
# Piper, Coqui TTS, or any /v1/audio/speech server
FORGE_TTS_URL=http://gpu-box:9100
FORGE_TTS_MODEL=tts-1              # whatever your server calls it
FORGE_TTS_VOICE=alloy
FORGE_TTS_FORMAT=pcm               # pcm (16 kHz mono, pipeline-ready) | wav
```

Enable in the voice loop with `FORGE_VOICE_TTS_PROVIDER=local-tts`. The cockpit
plays audio through the single shared playback module used by both
`handsfree.js` and `forge-home.html`.

### 1d. Image generation

```bash
# LocalAI, a ComfyUI/SD-WebUI OpenAI bridge, or any local diffusion server
FORGE_IMAGE_URL=http://gpu-box:9200
FORGE_IMAGE_MODEL=sd-xl              # your server's model id
FORGE_IMAGE_KEY=                     # optional
FORGE_IMAGE_OUTPUT_DIR=/data/forge-images   # default: <root>/forge-images
```

When the API answers with a URL instead of inline base64, Forge fetches it only
from the configured endpoint's own host.

### 1e. Where the media inputs may come from

Image/audio inputs travel as a `data:` URI in the request, or as a file path.
File paths are read **only** under `FORGE_MULTIMODAL_INPUT_DIR`
(colon-separated; defaults to the process working directory), so a prompt
cannot make Forge read an arbitrary file on the host.

---

## 2. Outbound channels (WhatsApp, SMS, voice call, email)

Channels are registered only when fully configured. A half-configured channel
is *not* registered: its send would always fail, and listing it as available
would be a lie. The `/api/v1/channels` endpoint reports both states, and each
unconfigured channel names the variables it needs.

### 2a. WhatsApp (Meta Cloud API)

```bash
FORGE_WHATSAPP_TOKEN=EAAG...          # your token
FORGE_WHATSAPP_PHONE_ID=1234567890    # the sender phone-number id
FORGE_WHATSAPP_TO=919876543210        # default recipient (optional)
FORGE_WHATSAPP_API_VERSION=v21.0      # optional
```

### 2b. SMS and phone calls (Twilio)

```bash
FORGE_TWILIO_ACCOUNT_SID=ACxxxx
FORGE_TWILIO_AUTH_TOKEN=xxxx
FORGE_TWILIO_FROM=+15550001111
```

`make_call` places a real call and speaks the message using TwiML `<Say>`
(`FORGE_TWILIO_BASE_URL` exists for testing against a local stand-in).

### 2c. Email (any SMTP server)

```bash
FORGE_SMTP_HOST=mail.example.com
FORGE_SMTP_PORT=587                  # 465 implies implicit TLS
FORGE_SMTP_FROM=forge@example.com
FORGE_SMTP_USER=forge                # optional (AUTH)
FORGE_SMTP_PASSWORD=xxxx             # optional
FORGE_SMTP_STARTTLS=1                # 0 for a local relay
```

### 2d. How a message is sent

Say or type the intent (`send_whatsapp`, `send_email`, `send_sms`, `make_call`)
— it goes through the normal permission/approval path, and is then delivered by
`ControlPlane._deliver_voice_message`. A send reports the provider's own
message id and status, or the provider's own error; a missing recipient is
reported as `no_recipient` and nothing is sent.

---

## 3. Verifying a deployment end to end

```bash
# 1. endpoints answer (real, tiny requests; nothing is marked verified unpaid)
python -c "from forge.models.multimodal_bridge import probe_multimodal; print(probe_multimodal())"

# 2. per-modality registration and the exact requirement when missing
python scripts/verify_production_readiness.py

# 3. API surface (authenticated, read-only)
curl -H "Authorization: Bearer $FORGE_AUTH_TOKEN" .../api/v1/multimodal
curl -H "Authorization: Bearer $FORGE_AUTH_TOKEN" .../api/v1/channels

# 4. the behaviours themselves
python -m pytest tests/test_multimodal_specialists.py tests/test_comms_channels.py \
                 tests/test_local_audio_vision_providers.py -q
```

The tests drive real HTTP servers standing in for each endpoint, so they prove
the request shapes (multipart WAV upload, data-URI chat message, TwiML call
document, SMTP dialogue) and the honesty rules, not just the plumbing.
