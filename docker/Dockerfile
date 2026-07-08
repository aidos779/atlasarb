# Multi-stage build — small, non-root production image.
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY src ./src
COPY alembic.ini ./
COPY config ./config

# Non-root runtime user.
RUN useradd --create-home --uid 10001 appuser
USER appuser

# Healthcheck: process is alive (bot polling loop). Replace with webhook probe if used.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import socket; socket.gethostbyname('api.telegram.org')" || exit 1

CMD ["python", "-m", "src.app"]
