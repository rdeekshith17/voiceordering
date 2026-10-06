# VoiceOrderAI production image
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

WORKDIR /app

# Install dependencies first (better layer caching). Deps are read from
# pyproject.toml with the stdlib so the list stays in one place.
COPY pyproject.toml ./
RUN pip install --upgrade pip && python - <<'EOF'
import subprocess, sys, tomllib
deps = tomllib.load(open("pyproject.toml", "rb"))["project"]["dependencies"]
subprocess.run([sys.executable, "-m", "pip", "install", *deps], check=True)
EOF

# Copy the application
COPY voiceorder ./voiceorder
COPY deploy/seed.py ./deploy/seed.py

# Writable runtime dirs (mounted as a volume in production)
RUN mkdir -p /app/data/backups /app/.tts_cache \
    && useradd --create-home --shell /usr/sbin/nologin app \
    && chown -R app:app /app
USER app

EXPOSE 8000

# Seed the tenant shell on first boot (idempotent), then start the API.
# $PORT with default 8000 so PaaS port injection works
CMD ["sh", "-c", "python deploy/seed.py && exec uvicorn voiceorder.api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
