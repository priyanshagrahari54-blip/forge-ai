FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FORGE_AUTH_MODE=production \
    FORGE_SECURE_COOKIES=1 \
    FORGE_DB_PATH=/data/forge/cockpit.db

WORKDIR /app
COPY . /app

RUN python -m pip install --upgrade pip \
    && python -m pip install --no-cache-dir .

RUN mkdir -p /data/forge

EXPOSE 8300
VOLUME ["/data/forge"]

CMD ["python", "forge_web.py"]
