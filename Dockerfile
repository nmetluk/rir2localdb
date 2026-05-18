# syntax=docker/dockerfile:1.7
#
# Multi-stage build. Один image, две роли через ENTRYPOINT + CMD:
#   docker run image                       → rir2localdb serve (default)
#   docker run image sync --tier core ...  → oneshot sync
#   docker run image migrate               → alembic upgrade head
#
# См. docs/07-operations.md § «Deployment via Docker».

# ---------------------------------------------------------------------------
# Stage 1 — build
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Build deps только для wheel-сборки asyncpg/psycopg при отсутствии
# manylinux wheels. На 3.12 они есть, но build-essential страхует.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/

# Build + install в /opt/venv. Wheel-install чище editable для image.
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install .

# ---------------------------------------------------------------------------
# Stage 2 — runtime
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    RIR2LOCALDB_LOG_FORMAT=json \
    RIR2LOCALDB_LOG_LEVEL=INFO \
    RIR2LOCALDB_DATA_DIR=/var/lib/rir2localdb/data

# Runtime deps:
# - libpq5 для asyncpg (на самом деле asyncpg использует свой
#   binary protocol без libpq, но pg_isready / psql полезны для
#   debug и в HEALTHCHECK альтернатив).
# - ca-certificates для HTTPS к RIR mirrors.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -r rir2local -g 1001 \
    && useradd -r -u 1001 -g rir2local -d /home/rir2local -m -s /bin/bash rir2local

COPY --from=builder /opt/venv /opt/venv

# Persistent cache mount-point.
RUN mkdir -p /var/lib/rir2localdb/data \
    && chown -R rir2local:rir2local /var/lib/rir2localdb

USER rir2local
WORKDIR /home/rir2local

# Healthcheck — для serve-режима. Для oneshot sync/migrate Docker
# просто игнорирует (контейнер завершается до первого check'а).
# /v1/healthz — liveness (БД не пингует), всегда 200 если процесс жив.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import httpx,sys; \
sys.exit(0 if httpx.get('http://localhost:8000/v1/healthz', timeout=3).status_code == 200 else 1)" \
    || exit 1

EXPOSE 8000

ENTRYPOINT ["rir2localdb"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
