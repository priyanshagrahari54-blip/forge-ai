FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FORGE_AUTH_MODE=production \
    FORGE_SECURE_COOKIES=1 \
    FORGE_DB_PATH=/data/forge/cockpit.db

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
