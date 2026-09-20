FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FORGE_AUTH_MODE=production \
    FORGE_SECURE_COOKIES=1 \
    FORGE_DB_PATH=/data/forge/cockpit.db \
    # Speak and listen with the in-process engine instead of the simulated
    # codec. Both stay honest if a probe fails: the provider reports the
    # missing package and refuses the request rather than answering with
    # synthetic speech. Set either to `simulated` to go back.
    FORGE_VOICE_TTS_PROVIDER=local-inprocess \
    FORGE_VOICE_STT_PROVIDER=local-inprocess

WORKDIR /app
COPY . /app

# `.[media]` adds the in-process vision / speech / image backends (Pillow,
# espeak-ng, pocketsphinx). They are what makes the vision, audio, speech,
# image-generation, browser and computer-use specialists runnable here without
# an external endpoint; each one still has to pass its own runtime probe before
# the fabric registers it.
RUN python -m pip install --upgrade pip \
    && python -m pip install --no-cache-dir ".[media]"

RUN mkdir -p /data/forge

EXPOSE 8300
VOLUME ["/data/forge"]

CMD ["python", "forge_web.py"]
